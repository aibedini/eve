"""Repeatable load test for Eve's hot paths (phase 27).

The baseline harness measures one request at a time; this tool measures the app
under concurrent arrival. It drives real WSGI requests in-process (deterministic,
no network) or a running panel over keep-alive HTTP, at a fixed arrival rate for
a fixed duration, and reports per-scenario p50/p95/p99 latency, achieved RPS, the
error rate and the status mix.

The effective concurrency settings (panel fetch slots, refresh workers, database
pool) are recorded with the result, so a capacity number can be traced to the
configuration that produced it.

Usage:
    python scripts/loadtest.py --quick
    python scripts/loadtest.py --rate 50 --duration 15 --json docs/performance/loadtest.json
    python scripts/loadtest.py --url http://127.0.0.1:5000 --rate 20 --duration 30
"""
import argparse
import http.client
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

QUICK = {"rate": 20, "duration": 3, "workers": 4}
DEFAULT = {"rate": 50, "duration": 10, "workers": 8}


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[min(len(ordered) - 1, max(0, index))]


class HttpClient:
    """One keep-alive HTTP connection for a load worker."""

    def __init__(self, base_url, cookie=None):
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.secure = parsed.scheme == "https"
        self.cookie = cookie
        self._connection = None

    def _connect(self):
        if self.secure:
            return http.client.HTTPSConnection(self.host, self.port, timeout=10)
        return http.client.HTTPConnection(self.host, self.port, timeout=10)

    def get(self, path):
        for attempt in (0, 1):
            try:
                if self._connection is None:
                    self._connection = self._connect()
                headers = {"Accept": "application/json, text/html"}
                if self.cookie:
                    headers["Cookie"] = self.cookie
                self._connection.request("GET", path, headers=headers)
                response = self._connection.getresponse()
                response.read()
                return response
            except Exception:
                try:
                    if self._connection is not None:
                        self._connection.close()
                except Exception:
                    pass
                self._connection = None
                if attempt:
                    raise
        return None


class AppTarget:
    """In-process WSGI target, seeded like the baseline harness."""

    def __init__(self, quick=False):
        import benchmark_baseline as harness
        sizes = dict(harness.QUICK_SIZES if quick else harness.DEFAULT_SIZES)
        db_path = os.path.join(tempfile.gettempdir(),
                               "eve-loadtest-%d.db" % os.getpid())
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except OSError:
                pass
        harness.configure_environment(db_path)
        self.harness = harness
        self.flask_app, self.ns = harness.load_app()
        self.ids = harness.seed(sizes, 1234)
        harness.build_snapshot(sizes, self.ids, 1234)
        self.sizes = sizes
        self.db_path = db_path
        self.revision = None
        with self.flask_app.test_client() as probe:
            with probe.session_transaction() as sess:
                sess["admin_id"] = self.ids["admin"]
                sess["role"] = "admin"
                sess["is_superadmin"] = False
        self.anon = harness._client_for(self.flask_app, self.ids["root"],
                                        "superadmin", True)
        payload = self.anon.get("/api/refresh").get_json() or {}
        self.revision = (payload.get("sync") or {}).get("revision")

    def clients(self):
        harness = self.harness
        return {
            # A genuinely anonymous client for the public paths; the root client
            # would follow the login redirect instead of rendering the page.
            "anonymous": self.flask_app.test_client(),
            "admin": harness._client_for(self.flask_app, self.ids["admin"],
                                         "admin", False),
        }

    def close(self):
        if not os.environ.get("EVE_BENCH_DATABASE_URL"):
            try:
                os.remove(self.db_path)
            except OSError:
                pass


def app_scenarios(target):
    since = target.revision if target.revision is not None else 0
    # /api/doctor is deliberately absent: it probes the TLS endpoints of every
    # configured server, which is an outbound network operation, not a hot path.
    return [
        ("html_login", "public login page render",
         lambda clients: clients["anonymous"].get("/login")),
        ("api_refresh_delta", "poll a known snapshot revision",
         lambda clients: clients["admin"].get("/api/refresh?since=%s&enqueue=0" % since)),
        ("static_style", "large cached stylesheet",
         lambda clients: clients["anonymous"].get("/static/style.css")),
        ("api_permissions", "authenticated JSON call",
         lambda clients: clients["admin"].get("/api/me/permissions")),
    ]


def http_scenarios(target, cookie=None):
    scenarios = [
        ("html_login", "public login page render",
         lambda clients: clients["anon"].get("/login")),
        ("static_style", "large cached stylesheet",
         lambda clients: clients["anon"].get("/static/style.css")),
    ]
    if cookie:
        scenarios.append((
            "api_refresh_delta", "poll the shared snapshot",
            lambda clients: clients["admin"].get("/api/refresh?mode=cache&enqueue=0")))
    return scenarios


def run_load(scenarios, rate, duration, workers, clients_factory):
    results = defaultdict(list)
    status_counts = Counter()
    lock = threading.Lock()
    stop_at = time.perf_counter() + max(0.5, float(duration))
    per_worker_interval = 1.0 / max(0.1, float(rate) / max(1, workers))

    def worker(index):
        clients = clients_factory()
        position = index
        next_at = time.perf_counter()
        while time.perf_counter() < stop_at:
            name, _description, call = scenarios[position % len(scenarios)]
            position += 1
            started = time.perf_counter()
            status = 0
            try:
                response = call(clients)
                status = int(getattr(response, "status_code", 0) or 0)
            except Exception:
                status = 0
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            with lock:
                results[name].append(elapsed_ms)
                status_counts[(name, status)] += 1
            next_at += per_worker_interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.perf_counter()

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(index,), daemon=True)
               for index in range(max(1, workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(30.0, duration * 5 + 30))
    wall = max(0.001, time.perf_counter() - started)

    summary = {}
    total = 0
    errors = 0
    rejected = 0
    for name, _description, _call in scenarios:
        samples = results.get(name) or []
        if not samples:
            summary[name] = {"requests": 0, "rps": 0.0, "p50_ms": None,
                             "p95_ms": None, "p99_ms": None, "max_ms": None,
                             "errors": 0, "rejected": 0, "status_counts": {}}
            continue
        counts = {str(status): count for (scenario, status), count
                  in status_counts.items() if scenario == name}
        scenario_errors = sum(count for (scenario, status), count
                              in status_counts.items()
                              if scenario == name and (status == 0 or status >= 500))
        scenario_rejected = sum(count for (scenario, status), count
                                in status_counts.items()
                                if scenario == name and 400 <= status < 500)
        total += len(samples)
        errors += scenario_errors
        rejected += scenario_rejected
        summary[name] = {
            "requests": len(samples),
            "rps": round(len(samples) / wall, 1),
            "p50_ms": round(_percentile(samples, 50), 2),
            "p95_ms": round(_percentile(samples, 95), 2),
            "p99_ms": round(_percentile(samples, 99), 2),
            "mean_ms": round(statistics.fmean(samples), 2),
            "max_ms": round(max(samples), 2),
            "errors": scenario_errors,
            "rejected": scenario_rejected,
            "status_counts": counts,
        }
    return {
        "wall_seconds": round(wall, 2),
        "total_requests": total,
        "achieved_rps": round(total / wall, 1),
        "errors": errors,
        "error_rate": round(errors / total, 4) if total else 0.0,
        "rejected": rejected,
        "rejected_rate": round(rejected / total, 4) if total else 0.0,
        "scenarios": summary,
    }


def _concurrency_settings():
    from panel.core import panel_limits
    from panel.core.db_pool import audit as db_audit
    settings = {
        "process_role": os.environ.get("EVE_PROCESS_ROLE", "combined"),
        "panel_concurrency": None,
        "refresh_workers": None,
        "fetch_wait_seconds": None,
    }
    for key, accessor in (("panel_concurrency", "concurrency_limit"),
                          ("refresh_workers", "refresh_worker_limit"),
                          ("fetch_wait_seconds", "fetch_wait_seconds")):
        try:
            settings[key] = getattr(panel_limits, accessor)()
        except Exception:
            pass
    try:
        settings["db_pool"] = {
            key: value for key, value in db_audit().items()
            if key in ("dialect", "pool_size", "max_overflow", "expected_max_connections")
        }
    except Exception:
        settings["db_pool"] = {}
    return settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Eve hot-path load test")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--rate", type=float, default=None, help="requests per second")
    parser.add_argument("--duration", type=float, default=None, help="seconds")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--url", default=None, help="load a running panel instead")
    parser.add_argument("--cookie", default=None, help="session cookie for authenticated paths")
    parser.add_argument("--json", default=None)
    parser.add_argument("--disable-limits", dest="disable_limits", action="store_true",
                        help="app target only: turn the rate limiter off to measure "
                             "capacity instead of the limit policy")
    args = parser.parse_args(argv)

    sizes = dict(QUICK if args.quick else DEFAULT)
    rate = args.rate if args.rate is not None else sizes["rate"]
    duration = args.duration if args.duration is not None else sizes["duration"]
    workers = args.workers if args.workers is not None else sizes["workers"]

    target = AppTarget(quick=args.quick)
    try:
        import app as app_module
        version = getattr(app_module, "APP_VERSION", "unknown")
    except Exception:
        version = "unknown"
    if args.disable_limits and not args.url:
        # Measure the application's capacity, not the rate-limit policy. The
        # extension caches its enabled flag at init_app time, so both the config
        # and the live attribute are cleared.
        target.flask_app.config["RATELIMIT_ENABLED"] = False
        try:
            from panel.extensions import limiter
            limiter.enabled = False
        except Exception:
            pass
    if args.url:
        clients_factory = lambda: {"anon": HttpClient(args.url),
                                   "admin": HttpClient(args.url, cookie=args.cookie)}
        scenarios = http_scenarios(args.url, args.cookie)
        target_name = args.url
    else:
        clients_factory = target.clients
        scenarios = app_scenarios(target)
        target_name = "in-process wsgi"

    result = run_load(scenarios, rate, duration, workers, clients_factory)
    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "app_version": version,
        "git_sha": target.harness._git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "target": target_name,
        "target_rate": rate,
        "duration_seconds": duration,
        "workers": workers,
        "dataset": dict(target.sizes),
        "concurrency": _concurrency_settings(),
        **result,
    }
    target.close()

    print("target=%s rate=%s/s duration=%ss workers=%d" % (
        target_name, rate, duration, workers))
    header = "%-18s %8s %8s %9s %9s %9s %7s %8s" % (
        "scenario", "requests", "rps", "p50_ms", "p95_ms", "p99_ms", "errors",
        "rejected")
    print(header)
    print("-" * len(header))
    for name, row in report["scenarios"].items():
        print("%-18s %8d %8.1f %9s %9s %9s %7d %8d" % (
            name, row["requests"], row["rps"], row["p50_ms"], row["p95_ms"],
            row["p99_ms"], row["errors"], row["rejected"]))
    print("total=%d achieved_rps=%.1f errors=%d (%.2f%%) rejected=%d (%.2f%%)" % (
        report["total_requests"], report["achieved_rps"], report["errors"],
        report["error_rate"] * 100, report["rejected"], report["rejected_rate"] * 100))

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write(os.linesep)
        print("report written to %s" % args.json)
    return 0 if report["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
