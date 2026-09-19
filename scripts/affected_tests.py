"""Tiered validation: run only the tests a change can actually affect.

The full suite takes ~15 minutes and most of it has nothing to do with the file you just
edited. This script maps changed files to the modules that can fail because of them, and
runs them in three tiers so ordinary development never pays for the whole suite:

  Tier 1 (default, target <= 30 s)  unit tests for the changed modules, syntax/import of
                                    the changed files, targeted regression. No real
                                    sleeps, no real Redis, no benchmarks.
  Tier 2 (pre-commit, target <= 2m) Tier 1 plus the integration suites the change can
                                    touch (renew consistency, scheduler, snapshot/fake
                                    Redis, SSE, UI audit).
  Tier 3 (release / CI / nightly)   the full suite, the real-Redis multi-process harness
                                    and the capacity/scale benchmarks. NEVER run this in
                                    an ordinary iteration.

Fail-fast: modules run sequentially in high-signal order and the first failure stops the
run, so a broken change is debugged at the cheapest tier that catches it.

Usage:
    .venv\\Scripts\\python.exe scripts\\affected_tests.py                  # Tier 1, git diff
    .venv\\Scripts\\python.exe scripts\\affected_tests.py --tier 2
    .venv\\Scripts\\python.exe scripts\\affected_tests.py --changed panel/jobs/schedulers.py
    .venv\\Scripts\\python.exe scripts\\affected_tests.py --list
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: path prefix -> (tier-1 modules, tier-2 extra modules). Longest prefix wins, so a more
#: specific mapping can refine a broad one.
MAPPING = (
    ('panel/core/refresh_policy.py', (
        # test_refresh_policy is the OLD policy suite and was missing from the first
        # version of this map: a rewrite of the fetcher loop left one of its tests driving
        # a loop that no longer returns, and Tier 1/2 never saw it. It hung the CI
        # integration job for 45 minutes. Keep it here.
        ['tests.test_server_polling', 'tests.test_refresh_policy'],
        ['tests.test_watch_propagation_crossprocess', 'tests.test_client_mutation_result'],
    )),
    ('panel/core/redis_client.py', (
        ['tests.test_per_server_redis'],
        ['tests.test_watch_propagation_crossprocess', 'tests.test_snapshot_delta',
         'tests.test_refresh_lock_scoping'],
    )),
    ('panel/core/panel_limits.py', (
        ['tests.test_panel_limits', 'tests.test_server_polling'],
        ['tests.test_mutation_scale'],
    )),
    ('panel/core/snapshot_delta.py', (
        ['tests.test_snapshot_delta'],
        ['tests.test_sse_updates', 'tests.test_client_events'],
    )),
    ('panel/core/memory_report.py', (
        # Attribution is the module the whole memory pass is read through: an edit here
        # can silently change what Settings -> Overview reports, so its own suite is
        # Tier 1 and never "unmapped" (the map had no entry for it).
        ['tests.test_memory_report'],
        ['tests.test_measure_snapshot_footprint', 'tests.test_memory_routes'],
    )),
    ('panel/jobs/schedulers.py', (
        # test_refresh_lock_scoping inspects this module's source (the fetch path must not
        # take the snapshot lock) and was missing from the first version of this map: a
        # rewrite of background_data_fetcher passed Tier 1/2 and only failed in CI.
        ['tests.test_server_polling', 'tests.test_refresh_lock_scoping',
         'tests.test_telemetry_pipeline_integration'],
        ['tests.test_refresh_reconcile', 'tests.test_sse_updates',
         'tests.test_usage_intelligence_observability', 'tests.test_refresh_policy'],
    )),
    ('panel/jobs/refresh.py', (
        ['tests.test_config_vs_telemetry', 'tests.test_server_polling'],
        ['tests.test_regression_matrix', 'tests.test_renew_consistency',
         'tests.test_client_mutation_result', 'tests.test_refresh_reconcile'],
    )),
    ('panel/routes/clients.py', (
        ['tests.test_renew_consistency'],
        ['tests.test_renew_enable', 'tests.test_regression_matrix',
         'tests.test_client_rotate', 'tests.test_config_vs_telemetry'],
    )),
    ('panel/routes/dashboard.py', (
        ['tests.test_sse_updates'],
        ['tests.test_client_events', 'tests.test_server_polling',
         'tests.test_subscription_scope', 'tests.test_refresh_policy'],
    )),
    ('panel/routes/doctor.py', (
        ['tests.test_server_polling'],
        ['tests.test_observability'],
    )),
    ('panel/routes/system.py', (
        # The memory endpoint lives here (and the updater); its contract is asserted by
        # test_memory_routes, which had no mapping to reach it.
        ['tests.test_memory_routes'],
        ['tests.test_system_update', 'tests.test_security_headers'],
    )),
    ('panel/routes/pages.py', (
        ['tests.test_ui_design_system'],
        ['tests.test_subscription_scope', 'tests.test_static_deploy_contract'],
    )),
    ('panel/adapters/xui.py', (
        ['tests.test_3xui_compat'],
        ['tests.test_regression_matrix', 'tests.test_renew_enable'],
    )),
    ('panel/services/client_state.py', (
        ['tests.test_client_mutation_result'],
        ['tests.test_regression_matrix', 'tests.test_renew_consistency'],
    )),
    ('templates/', (
        ['tests.test_ui_design_system'],
        ['tests.test_subscription_scope', 'tests.test_static_deploy_contract',
         'tests.test_subscription_visual_harness'],
    )),
    ('static/', (
        ['tests.test_ui_design_system'],
        ['tests.test_static_caching', 'tests.test_static_deploy_contract'],
    )),
    ('docs/', (
        ['tests.test_docs_index'],
        ['tests.test_ci_guards'],
    )),
    ('scripts/measure_snapshot_footprint.py', (
        # Longest prefix wins, so the measurement script gets its own counters test
        # instead of falling through to the generic scripts/ entry.
        ['tests.test_measure_snapshot_footprint'],
        ['tests.test_memory_report'],
    )),
    ('scripts/memory_attribution.py', (
        ['tests.test_memory_attribution'],
        ['tests.test_memory_report'],
    )),
    ('scripts/', (
        ['tests.test_ci_guards'],
        ['tests.test_benchmark_harness'],
    )),
    ('app.py', (
        ['tests.test_server_polling'],
        ['tests.test_regression_matrix', 'tests.test_security_headers'],
    )),
)

#: Always part of Tier 2: they are the cross-cutting guards that a change in any of the
#: paths above can break.
TIER2_ALWAYS = (
    'tests.test_ui_design_system',
    'tests.test_ci_guards',
)

TIER3 = (
    ('full unittest suite', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests']),
    ('real-Redis multi-process harness',
     [sys.executable, 'scripts/integration_redis_multiprocess.py']),
    ('HOT capacity (virtual time)', [sys.executable, 'scripts/benchmark_hot_capacity.py']),
    ('scheduling wall-clock benchmark (quick)',
     [sys.executable, 'scripts/benchmark_per_server_scheduling.py', '--quick']),
)


def changed_files(explicit=None):
    if explicit:
        return [path.replace('\\', '/') for path in explicit]
    try:
        out = subprocess.run(['git', 'diff', '--name-only', 'HEAD'],
                             cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        names = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        out2 = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard'],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        names += [line.strip() for line in out2.stdout.splitlines() if line.strip()]
        return [name.replace('\\', '/') for name in names]
    except Exception:
        return []


def modules_for(files, tier):
    tier1, tier2 = [], []
    for path in files:
        best = None
        for prefix, (t1, t2) in MAPPING:
            if path.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, t1, t2)
        if best is None:
            continue
        for module in best[1]:
            if module not in tier1:
                tier1.append(module)
        for module in best[2]:
            if module not in tier2:
                tier2.append(module)
    if tier == 1:
        return tier1
    ordered = list(tier1)
    for module in list(tier2) + list(TIER2_ALWAYS):
        if module not in ordered:
            ordered.append(module)
    return ordered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tier', type=int, default=1, choices=(1, 2, 3))
    parser.add_argument('--changed', nargs='*', default=None,
                        help='files to map (default: git diff + untracked)')
    parser.add_argument('--list', action='store_true', help='print the plan and exit')
    parser.add_argument('--budget-seconds', type=float, default=None,
                        help='stop before starting a module that would exceed it')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    files = changed_files(args.changed)
    budget = args.budget_seconds
    if budget is None:
        budget = {1: 30.0, 2: 120.0, 3: 0.0}[args.tier]
    print('changed files (%d): %s' % (len(files), ', '.join(files) or '(none)'))

    if args.tier == 3:
        print('TIER 3 invokes the full suite and the real-backend harnesses:')
        for label, command in TIER3:
            print('  - %s: %s' % (label, ' '.join(command)))
        if args.list:
            return 0
        print('refusing to run Tier 3 from this script; run the commands above explicitly '
              '(release/CI only)')
        return 0

    modules = modules_for(files, args.tier)
    if not modules:
        print('no mapped test modules for these files; nothing to run at Tier %d' % args.tier)
        return 0
    print('Tier %d plan (%d modules, budget %ss):' % (args.tier, len(modules), budget))
    for module in modules:
        print('  - %s' % module)
    if args.list:
        return 0

    env = dict(os.environ)
    env.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    started = time.perf_counter()
    spent = 0.0
    for module in modules:
        if budget and spent > budget:
            print('\nbudget of %ss exhausted; stopping before %s' % (budget, module))
            break
        module_started = time.perf_counter()
        result = subprocess.run([sys.executable, '-m', 'unittest', module],
                                cwd=REPO_ROOT, env=env, capture_output=True, text=True)
        elapsed = time.perf_counter() - module_started
        spent = time.perf_counter() - started
        tail = [line for line in result.stdout.splitlines()
                if line.startswith(('Ran ', 'OK', 'FAILED', 'ERROR:', 'FAIL:'))]
        status = 'ok' if result.returncode == 0 else 'FAIL'
        print('%-58s %-4s %5.1fs  %s' % (module, status, elapsed,
                                         ' | '.join(tail[-2:]) if tail else ''))
        if result.returncode != 0:
            # Fail fast: the first red module is the one to debug, at the cheapest tier.
            print('\n--- %s failed; stopping (fail-fast) ---' % module)
            print(result.stdout[-4000:])
            print(result.stderr[-2000:])
            return 1
    print('\nTier %d green in %.1fs' % (args.tier, time.perf_counter() - started))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
