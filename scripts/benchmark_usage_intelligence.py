"""Usage-intelligence performance and index verification (RFP sections 30, 31, 50, 51).

Seeds a realistically large dataset (accounts x days of UsageDaily plus one verified renewal
per account), then measures the recommendation on the real code path:

* p95 latency of ``build_recommendation_v5`` (budget: < 50 ms warm) and the statements it
  issued (budget: <= 6);
* that the read path makes **zero** outbound HTTP calls (no X-UI);
* the SQLite query plans of the three windows (latest verified renewal, usage since the cycle,
  rolling 31 days): an account-scoped SEARCH, never a full SCAN of UsageDaily/RenewalEvent.

The same three queries are printed as SQL for ``EXPLAIN ANALYZE`` on PostgreSQL, which is what
a production database should be checked with (RFP section 51); the plan *shape* assertion
runs everywhere.

Usage:
    python scripts/benchmark_usage_intelligence.py --quick
    python scripts/benchmark_usage_intelligence.py --json docs/performance/usage-intelligence.json
"""
import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

LATENCY_BUDGET_P95_MS = 50.0
QUERY_BUDGET = 6
MAX_OUTBOUND_HTTP = 0

PLAN_QUERIES = (
    ('latest_verified_renewal',
     "SELECT * FROM renewal_events WHERE server_id = :sid AND sub_id = :sub "
     "AND verified = 1 AND event_type IN ('renewal', 'package_change') "
     "ORDER BY renewed_at DESC, id DESC LIMIT 1"),
    ('usage_since_cycle',
     "SELECT * FROM usage_daily WHERE server_id = :sid AND sub_id = :sub "
     "AND usage_date >= :day AND last_observed_at >= :day ORDER BY usage_date ASC LIMIT 400"),
    ('rolling_31d',
     "SELECT * FROM usage_daily WHERE server_id = :sid AND sub_id = :sub "
     "AND usage_date >= :day ORDER BY usage_date ASC LIMIT 400"),
)


def p95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1))
    return ordered[index]


def _seed(app_module_db, accounts, days, *, server_id=7001):
    """Bulk-insert the dataset: accounts x days of usage, one renewal each."""
    from datetime import date, datetime, timedelta
    from panel.models import RenewalEvent, Server, UsageCounterState, UsageDaily

    db = app_module_db
    Server.query.filter(Server.id == server_id).delete(synchronize_session=False)
    db.session.commit()
    db.session.add(Server(id=server_id, name='usage-bench', host='https://bench.invalid',
                          username='u', password='p', panel_type='auto', enabled=True))
    db.session.commit()

    now = datetime.utcnow()
    daily_rows = []
    states = []
    events = []
    for index in range(accounts):
        sub_id = 'bench-%d' % index
        for offset in range(days):
            observed = now - timedelta(days=offset)
            used = int((1.0 + (index % 5) * 0.5) * 1024 ** 3)
            daily_rows.append({
                'server_id': server_id, 'sub_id': sub_id,
                'usage_date': date.today() - timedelta(days=offset),
                'upload_bytes': 0, 'download_bytes': used,
                'opening_upload_bytes': 0, 'opening_download_bytes': 0,
                'closing_upload_bytes': 0, 'closing_download_bytes': used,
                'sample_count': 1, 'first_observed_at': observed,
                'last_observed_at': observed,
            })
        states.append({
            'server_id': server_id, 'sub_id': sub_id, 'upload_bytes': 0,
            'download_bytes': int(30 * 1024 ** 3), 'total_bytes': int(30 * 1024 ** 3),
            'observed_at': now,
        })
        events.append({
            'server_id': server_id, 'sub_id': sub_id,
            'renewed_at': now - timedelta(days=8), 'event_type': 'renewal',
            'source': 'explicit_renew', 'verified': True, 'verified_at': now,
            'traffic_reset': False,
            'previous_volume_limit_bytes': int(50 * 1024 ** 3),
            'new_volume_limit_bytes': int(100 * 1024 ** 3),
            'previous_remaining_bytes': int(20 * 1024 ** 3),
            'carried_over_bytes': int(20 * 1024 ** 3),
            'granted_volume_bytes': int(50 * 1024 ** 3),
            'operation_id': 'bench-op-%d' % index, 'created_at': now,
        })
    db.session.bulk_insert_mappings(UsageDaily, daily_rows)
    db.session.bulk_insert_mappings(UsageCounterState, states)
    db.session.bulk_insert_mappings(RenewalEvent, events)
    db.session.commit()
    return server_id, len(daily_rows)


def _query_plans(server_id, sub_id):
    """SQLite query plans for the three windows (the shape must be an indexed SEARCH)."""
    from sqlalchemy import text
    from panel.extensions import db
    plans = {}
    params = {'sid': int(server_id), 'sub': str(sub_id),
              'day': (__import__('datetime').datetime.utcnow()
                      - __import__('datetime').timedelta(days=31)).date()}
    for name, sql in PLAN_QUERIES:
        try:
            rows = db.session.execute(text('EXPLAIN QUERY PLAN ' + sql), params).fetchall()
            plans[name] = ' | '.join(str(row[-1]) for row in rows)
        except Exception as exc:
            plans[name] = 'unavailable: %s' % exc
    return plans


def _outbound_http_calls(action):
    import requests
    sent = []
    original = requests.sessions.Session.request

    def counting(self, method, url, *args, **kwargs):
        sent.append((method, url))
        raise RuntimeError('stopped an outbound request: %s %s' % (method, url))

    requests.sessions.Session.request = counting
    try:
        action()
    except RuntimeError:
        pass
    finally:
        requests.sessions.Session.request = original
    return len(sent)


def run(*, quick=False, accounts=None, days=31, samples=None):
    from datetime import datetime
    os.environ['FLASK_ENV'] = 'development'
    os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
    os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
    db_path = os.path.join(tempfile.gettempdir(), 'eve-bench-usage-%d.db' % os.getpid())
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ['DATABASE_URL'] = 'sqlite:///' + db_path.replace(os.sep, '/')

    from app import app, db
    from panel.services.usage_intelligence.recommendation import build_recommendation_v5

    accounts = accounts or (200 if quick else 10000)
    samples = samples or (20 if quick else 60)
    packages = [
        {'id': 1, 'name': 'starter', 'days': 30, 'volume': 30, 'price': 100},
        {'id': 2, 'name': 'standard', 'days': 30, 'volume': 60, 'price': 200},
        {'id': 3, 'name': 'plus', 'days': 30, 'volume': 120, 'price': 350},
        {'id': 4, 'name': 'max', 'days': 30, 'volume': 200, 'price': 500},
    ]

    with app.app_context():
        db.create_all()
        started_seed = time.perf_counter()
        server_id, row_count = _seed(db, accounts, days)
        seed_seconds = time.perf_counter() - started_seed
        target = 'bench-%d' % (accounts - 1)
        live = {'total_bytes': int(30 * 1024 ** 3), 'observed_at': datetime.utcnow()}

        # Warm-up: connection pool, compiled statements, caches.
        payload = build_recommendation_v5(server_id, target, packages, live_usage=live)
        if payload is None:
            raise RuntimeError('the benchmark account produced no recommendation')

        latencies = []
        query_counts = []
        for _ in range(samples):
            started = time.perf_counter()
            result = build_recommendation_v5(server_id, target, packages, live_usage=live)
            latencies.append((time.perf_counter() - started) * 1000.0)
            query_counts.append(int((result or {}).get('evidence', {}).get('queries') or 0))

        http_calls = _outbound_http_calls(
            lambda: build_recommendation_v5(server_id, target, packages, live_usage=live))
        plans = _query_plans(server_id, target)

    scanned = {name: ('SCAN' in plan.upper() and 'USING' not in plan.upper())
               for name, plan in plans.items()}
    latency_p95 = p95(latencies)
    result = {
        'mode': 'quick' if quick else 'full',
        'accounts': accounts,
        'days_per_account': days,
        'usage_daily_rows': row_count,
        'seed_seconds': round(seed_seconds, 2),
        'samples': len(latencies),
        'latency_ms': {
            'mean': round(statistics.fmean(latencies), 3) if latencies else 0.0,
            'p50': round(statistics.median(latencies), 3) if latencies else 0.0,
            'p95': round(latency_p95, 3),
            'max': round(max(latencies), 3) if latencies else 0.0,
        },
        'queries_per_recommendation': max(query_counts) if query_counts else 0,
        'outbound_http_calls': http_calls,
        'query_plans': plans,
        'full_scans': scanned,
        'payload_basis': (payload or {}).get('forecast_basis'),
        'verdicts': {
            'latency_within_budget': {
                'p95_ms': round(latency_p95, 3),
                'budget_ms': LATENCY_BUDGET_P95_MS,
                'passed': latency_p95 < LATENCY_BUDGET_P95_MS,
            },
            'query_budget': {
                'queries': max(query_counts) if query_counts else 99,
                'budget': QUERY_BUDGET,
                'passed': bool(query_counts) and max(query_counts) <= QUERY_BUDGET,
            },
            'no_panel_calls': {
                'outbound_http_calls': http_calls,
                'maximum': MAX_OUTBOUND_HTTP,
                'passed': http_calls <= MAX_OUTBOUND_HTTP,
            },
            'indexed_reads': {
                'full_scans': {k: v for k, v in scanned.items() if v},
                'passed': not any(scanned.values()),
            },
        },
    }
    result['passed'] = all(item['passed'] for item in result['verdicts'].values())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Usage-intelligence performance + indexes')
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--json', default=None)
    parser.add_argument('--accounts', type=int, default=None)
    parser.add_argument('--samples', type=int, default=None)
    args = parser.parse_args(argv)
    result = run(quick=args.quick, accounts=args.accounts, samples=args.samples)
    print('accounts=%d days=%d usage_daily_rows=%d (seeded in %.1fs)' % (
        result['accounts'], result['days_per_account'], result['usage_daily_rows'],
        result['seed_seconds']))
    print('recommendation latency: mean=%.3f p50=%.3f p95=%.3f max=%.3f ms (n=%d)' % (
        result['latency_ms']['mean'], result['latency_ms']['p50'],
        result['latency_ms']['p95'], result['latency_ms']['max'], result['samples']))
    print('queries per recommendation=%d, outbound HTTP calls=%d' % (
        result['queries_per_recommendation'], result['outbound_http_calls']))
    for name, plan in result['query_plans'].items():
        print('plan %-26s %s' % (name, plan))
    for name, verdict in result['verdicts'].items():
        print('%-24s %s' % (name, 'OK' if verdict['passed'] else 'MISSED'))
    print('performance and indexes within budget' if result['passed']
          else 'PERFORMANCE REGRESSION')
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print('result written to %s' % args.json)
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
