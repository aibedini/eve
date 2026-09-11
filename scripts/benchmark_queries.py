"""Per-scenario SQL statement profiler (phase 20).

The baseline harness records how MANY statements each request executes. This tool
records WHICH statements repeat, which is what identifies an N+1: a normalised
statement executed once per row instead of once per request.

It reuses the baseline harness for the environment, dataset and scenarios, so the
two tools measure exactly the same request paths.

Usage:
    python scripts/benchmark_queries.py --quick
    python scripts/benchmark_queries.py --json docs/performance/query-profile.json
"""
import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import benchmark_baseline as harness  # noqa: E402

STATEMENT_PREVIEW = 300


def _normalize(statement):
    return " ".join(str(statement or "").split())[:STATEMENT_PREVIEW]


def install_recorder(db):
    """Record every executed statement (normalised) for the next request."""
    from sqlalchemy import event
    state = {"statements": [], "total": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        state["total"] += 1
        state["statements"].append(_normalize(statement))

    event.listen(db.engine, "before_cursor_execute", _before)
    return state


def profile(scenarios, state, top=5, warmup=1):
    results = {}
    for name, description, call in scenarios:
        for _ in range(max(0, warmup)):
            call()
        state["statements"] = []
        state["total"] = 0
        response = call()
        counts = Counter(state["statements"])
        results[name] = {
            "description": description,
            "status": getattr(response, "status_code", None),
            "sql_statements": state["total"],
            "repeated": [
                {"count": count, "statement": statement}
                for statement, count in counts.most_common(top) if count > 1
            ],
        }
    return results


def format_lines(profile_result):
    lines = []
    for name, row in profile_result.items():
        lines.append("%s: %d statements" % (name, row["sql_statements"]))
        for item in row["repeated"]:
            lines.append("    x%-3d %s" % (item["count"], item["statement"][:120]))
    return lines


def run(sizes, seed_value=1234, top=5):
    db_path = os.path.join(tempfile.gettempdir(), "eve-query-profile-%d.db" % os.getpid())
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    url = harness.configure_environment(db_path)
    flask_app, ns = harness.load_app()
    ids = harness.seed(sizes, seed_value)
    harness.build_snapshot(sizes, ids, seed_value)
    with flask_app.app_context():
        state = install_recorder(ns["db"])
    scenarios = harness.build_scenarios(sizes, ids)
    results = profile(scenarios, state, top=top)
    if not os.environ.get("EVE_BENCH_DATABASE_URL"):
        try:
            os.remove(db_path)
        except OSError:
            pass
    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "app_version": ns["version"],
        "git_sha": harness._git_sha(),
        "sizes": dict(sizes),
        "database_url_scheme": url.split(":", 1)[0],
        "scenarios": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Eve SQL statement profiler")
    parser.add_argument("--quick", action="store_true", help="tiny dataset")
    parser.add_argument("--json", default=None, help="write the profile as JSON")
    parser.add_argument("--top", type=int, default=5, help="repeated statements per scenario")
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args(argv)
    sizes = dict(harness.QUICK_SIZES if args.quick else harness.DEFAULT_SIZES)
    report = run(sizes, seed_value=args.seed, top=args.top)
    print(*format_lines(report["scenarios"]), sep=os.linesep)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print("profile written to %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
