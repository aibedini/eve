"""Bounded list responses: what one GET used to return vs one page (phase 22).

The BNQO link inventory is the largest operator-facing list in the panel. Before
phase 22 GET /api/bnqo/links materialised and serialised every row (and lazy-loaded
both agents per row); now it returns one bounded page plus total/has_more, and a
client can walk the pages with offset/limit.

This tool seeds an isolated database with a deterministic link inventory and
measures both shapes on the same data: the unbounded response (the previous
behaviour, reproduced from the replaced query) and one bounded page.

Usage:
    python scripts/benchmark_pagination.py --quick
    python scripts/benchmark_pagination.py --json docs/performance/api-pagination.json
"""
import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import benchmark_baseline as harness  # noqa: E402

QUICK = {"links": 120, "repeat": 3}
DEFAULT = {"links": 1000, "repeat": 5}


def _seed(db, models, count):
    BnqoAgent = models["BnqoAgent"]
    BnqoLink = models["BnqoLink"]
    db.drop_all()
    db.create_all()
    agents = [
        BnqoAgent(name="bench-agent-a", role="iran", token="t" * 32,
                  pubkey="p" * 32, address="10.0.0.1", port=9000),
        BnqoAgent(name="bench-agent-b", role="outside", token="u" * 32,
                  pubkey="q" * 32, address="10.0.0.2", port=9000),
    ]
    db.session.add_all(agents)
    db.session.commit()
    db.session.bulk_save_objects([
        BnqoLink(name="bench-link-%d" % index, agent_a_id=agents[0].id,
                 agent_b_id=agents[1].id, enabled=True, status="up")
        for index in range(count)
    ])
    db.session.commit()
    return agents[0].id, agents[1].id


def _timed(call, repeat):
    samples = []
    value = None
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        value = call()
        samples.append((time.perf_counter() - started) * 1000.0)
    return value, statistics.fmean(samples)


def _statements(db, call):
    from sqlalchemy import event
    counter = {"count": 0}
    def _before(conn, cursor, statement, parameters, context, executemany):
        counter["count"] += 1
    event.listen(db.engine, "before_cursor_execute", _before)
    try:
        call()
    finally:
        event.remove(db.engine, "before_cursor_execute", _before)
    return counter["count"]


def run(sizes):
    db_path = os.path.join(tempfile.gettempdir(), "eve-pagination-bench-%d.db" % os.getpid())
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    harness.configure_environment(db_path)
    flask_app, ns = harness.load_app()
    from panel.models import BnqoAgent, BnqoLink
    from panel.routes.common import DEFAULT_PAGE_SIZE, paginate_query
    from sqlalchemy.orm import joinedload

    with flask_app.app_context():
        _seed(ns["db"], {"BnqoAgent": BnqoAgent, "BnqoLink": BnqoLink}, sizes["links"])
        repeat = sizes["repeat"]

        def unbounded():
            rows = BnqoLink.query.order_by(BnqoLink.id.asc()).all()
            return json.dumps([row.to_dict() for row in rows], ensure_ascii=False)

        def one_page():
            rows, meta = paginate_query(BnqoLink.query.order_by(BnqoLink.id.asc()).options(
                joinedload(BnqoLink.agent_a), joinedload(BnqoLink.agent_b)))
            return json.dumps({"links": [row.to_dict() for row in rows], **meta},
                              ensure_ascii=False)

        with flask_app.test_request_context("/api/bnqo/links"):
            unbounded_body, unbounded_ms = _timed(unbounded, repeat)
            unbounded_statements = _statements(ns["db"], unbounded)
            page_body, page_ms = _timed(one_page, repeat)
            page_statements = _statements(ns["db"], one_page)
            page_meta_dict = json.loads(page_body)
            page_limit = page_meta_dict["limit"]

            # Walking every page must reach the same rows the unbounded call did.
            walked = []
            offset = 0
            requests_made = 0
            while True:
                request_path = "/api/bnqo/links?limit=%d&offset=%d" % (page_limit, offset)
                with flask_app.test_request_context(request_path):
                    rows, meta = paginate_query(
                        BnqoLink.query.order_by(BnqoLink.id.asc()).options(
                            joinedload(BnqoLink.agent_a), joinedload(BnqoLink.agent_b)))
                requests_made += 1
                walked.extend(row.id for row in rows)
                if not meta["has_more"]:
                    break
                offset = meta["next_offset"]
                if requests_made > 1000:
                    break

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "app_version": ns["version"],
        "git_sha": harness._git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "links": sizes["links"],
        "repeat": sizes["repeat"],
        "unbounded": {
            "rows": sizes["links"],
            "bytes": len(unbounded_body.encode("utf-8")),
            "mean_ms": round(unbounded_ms, 1),
            "statements": unbounded_statements,
        },
        "bounded_page": {
            "rows": len(page_meta_dict["links"]),
            "bytes": len(page_body.encode("utf-8")),
            "mean_ms": round(page_ms, 1),
            "statements": page_statements,
            "limit": page_limit,
            "has_more": page_meta_dict["has_more"],
            "total": page_meta_dict["total"],
        },
        "full_walk": {
            "requests": requests_made,
            "rows": len(walked),
            "unique_rows": len(set(walked)),
        },
    }
    report["row_reduction"] = round(
        report["unbounded"]["rows"] / max(1, report["bounded_page"]["rows"]), 1)
    report["byte_reduction"] = round(
        report["unbounded"]["bytes"] / max(1, report["bounded_page"]["bytes"]), 1)
    if not os.environ.get("EVE_BENCH_DATABASE_URL"):
        try:
            os.remove(db_path)
        except OSError:
            pass
    return report


def format_lines(report):
    return [
        "links=%d  repeat=%d" % (report["links"], report["repeat"]),
        "unbounded : %d rows  %d bytes  %.1f ms  %d statements" % (
            report["unbounded"]["rows"], report["unbounded"]["bytes"],
            report["unbounded"]["mean_ms"], report["unbounded"]["statements"]),
        "one page  : %d rows  %d bytes  %.1f ms  %d statements" % (
            report["bounded_page"]["rows"], report["bounded_page"]["bytes"],
            report["bounded_page"]["mean_ms"], report["bounded_page"]["statements"]),
        "reduction : %.1fx rows  %.1fx bytes" % (
            report["row_reduction"], report["byte_reduction"]),
        "full walk : %d requests, %d rows (%d unique)" % (
            report["full_walk"]["requests"], report["full_walk"]["rows"],
            report["full_walk"]["unique_rows"]),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded list response measurement")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", default=None)
    args = parser.parse_args(argv)
    sizes = dict(QUICK if args.quick else DEFAULT)
    report = run(sizes)
    print(*format_lines(report), sep=os.linesep)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print("report written to %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())