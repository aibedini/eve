"""Frontend delivery measurement: page weight, assets and cache headers (phase 21).

The dashboard renders a large HTML document and pulls a fixed set of local assets.
This tool records, for each page, the HTML bytes plus every local static asset it
references (bytes and cache headers), and the gzip potential of the body. It uses
the baseline harness for the environment, dataset and dashboard snapshot, so the
numbers belong to the same dataset as the other performance artifacts.

Usage:
    python scripts/benchmark_frontend.py --quick
    python scripts/benchmark_frontend.py --json docs/performance/frontend-baseline.json
"""
import argparse
import gzip
import json
import os
import re
import sys
import tempfile
from datetime import datetime

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import benchmark_baseline as harness  # noqa: E402

# Keep the query string: since phase 21 the templates emit ?v=<mtime+size>, and
# the measurement must reflect the URL the browser really requests.
_STATIC_REF = re.compile(r'(?:src|href)="([^"]*?/static/[^"#]+)"')


def _static_refs(html):
    refs = []
    for match in _STATIC_REF.finditer(html):
        ref = match.group(1)
        if ref not in refs:
            refs.append(ref)
    return refs


def _asset_row(client, ref):
    response = client.get(ref)
    body = response.get_data()
    return {
        "path": ref,
        "status": response.status_code,
        "bytes": len(body),
        "cache_control": response.headers.get("Cache-Control"),
        "etag": bool(response.headers.get("ETag")),
        "versioned": "?" in ref,
    }


def _page_row(client, name, path):
    response = client.get(path)
    body = response.get_data()
    content_type = (response.headers.get("Content-Type") or "").split(";")[0]
    row = {
        "path": path,
        "status": response.status_code,
        "content_type": content_type,
        "html_bytes": len(body),
        "gzip_bytes": len(gzip.compress(body, 6)) if body else 0,
        "cache_control": response.headers.get("Cache-Control"),
    }
    if content_type == "text/html":
        refs = _static_refs(body.decode("utf-8", "replace"))
        assets = [_asset_row(client, ref) for ref in refs]
        row["assets"] = assets
        row["asset_count"] = len(assets)
        row["asset_bytes"] = sum(item["bytes"] for item in assets)
        row["total_bytes"] = row["html_bytes"] + row["asset_bytes"]
        row["immutable_assets"] = sum(
            1 for item in assets if "immutable" in (item["cache_control"] or ""))
        row["unversioned_assets"] = sum(1 for item in assets if not item["versioned"])
    return name, row


def measure(pages):
    results = {}
    for client, name, path in pages:
        key, row = _page_row(client, name, path)
        results[key] = row
    return results


def run(sizes, seed_value=1234):
    db_path = os.path.join(tempfile.gettempdir(), "eve-frontend-bench-%d.db" % os.getpid())
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    harness.configure_environment(db_path)
    flask_app, ns = harness.load_app()
    ids = harness.seed(sizes, seed_value)
    harness.build_snapshot(sizes, ids, seed_value)
    admin_client = harness._client_for(flask_app, ids["admin"], "admin", False)
    anonymous = flask_app.test_client()
    pages = [
        (anonymous, "html_login", "/login"),
        (admin_client, "html_dashboard", "/"),
        (admin_client, "api_refresh", "/api/refresh"),
    ]
    report = measure(pages)
    report["meta"] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "app_version": ns["version"],
        "git_sha": harness._git_sha(),
        "sizes": dict(sizes),
        "gzip_available_in_process": _has_compressor(),
    }
    if not os.environ.get("EVE_BENCH_DATABASE_URL"):
        try:
            os.remove(db_path)
        except OSError:
            pass
    return report


def _has_compressor():
    try:
        import flask_compress  # noqa: F401
        return True
    except Exception:
        return False


def format_lines(report):
    lines = []
    for name, row in report.items():
        if name == "meta":
            continue
        if row["content_type"] == "text/html":
            lines.append("%-16s %8d B html  %8d B assets  %8d B total  %d assets" % (
                name, row["html_bytes"], row["asset_bytes"], row["total_bytes"],
                row["asset_count"]))
            for item in row["assets"]:
                lines.append("    %-46s %8d B  %s" % (
                    item["path"][:46], item["bytes"], item["cache_control"]))
        else:
            lines.append("%-16s %8d B  %s  gzip %d B" % (
                name, row["html_bytes"], row["content_type"], row["gzip_bytes"]))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Frontend delivery measurement")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", default=None)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args(argv)
    sizes = dict(harness.QUICK_SIZES if args.quick else harness.DEFAULT_SIZES)
    report = run(sizes, seed_value=args.seed)
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
