"""In-process HTTP request metrics (phase 26).

Dependency-free, bounded counters so /api/doctor can answer "what is slow and
what is failing" without an external metrics stack. Requests are keyed by Flask
endpoint (never by raw path), so a scanner hitting random URLs cannot grow the
map: anything without an endpoint is folded into one bucket, and the oldest key
is evicted once MAX_KEYS entries exist.
"""
import threading
import time
from collections import OrderedDict

MAX_KEYS = 200
MAX_KEY_LENGTH = 80
SLOW_REQUEST_MS = 1000.0

_lock = threading.Lock()
_counters = OrderedDict()
_total_requests = 0
_started_at = time.time()


def _bucket_key(endpoint, method):
    name = str(endpoint or "unmatched")[:MAX_KEY_LENGTH]
    return "%s %s" % (str(method or "GET").upper(), name)


def observe(endpoint, method, status, duration_ms):
    """Record one finished request. Never raises."""
    global _total_requests
    try:
        status = int(status or 0)
        duration_ms = max(0.0, float(duration_ms or 0.0))
    except (TypeError, ValueError):
        status, duration_ms = 0, 0.0
    key = _bucket_key(endpoint, method)
    with _lock:
        _total_requests += 1
        entry = _counters.get(key)
        if entry is None:
            entry = {
                "requests": 0, "errors": 0, "client_errors": 0,
                "total_ms": 0.0, "max_ms": 0.0, "slow": 0,
                "last_status": status, "last_seen": time.time(),
            }
            _counters[key] = entry
            while len(_counters) > MAX_KEYS:
                _counters.popitem(last=False)
        entry["requests"] += 1
        entry["total_ms"] += duration_ms
        entry["max_ms"] = max(entry["max_ms"], duration_ms)
        if duration_ms >= SLOW_REQUEST_MS:
            entry["slow"] += 1
        if status >= 500:
            entry["errors"] += 1
        elif status >= 400:
            entry["client_errors"] += 1
        entry["last_status"] = status
        entry["last_seen"] = time.time()
        _counters.move_to_end(key)


def _public(entry):
    requests = entry["requests"] or 1
    return {
        "requests": entry["requests"],
        "errors": entry["errors"],
        "client_errors": entry["client_errors"],
        "mean_ms": round(entry["total_ms"] / requests, 2),
        "max_ms": round(entry["max_ms"], 2),
        "slow_requests": entry["slow"],
        "last_status": entry["last_status"],
        "last_seen": entry["last_seen"],
    }


def snapshot(limit=15):
    """Aggregated view for the doctor endpoint."""
    with _lock:
        items = [(key, dict(entry)) for key, entry in _counters.items()]
        total_requests = _total_requests
    errors = sum(entry["errors"] for _, entry in items)
    classes = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0}
    for _, entry in items:
        status = entry["last_status"]
        if 200 <= status < 300:
            classes["2xx"] += 1
        elif 300 <= status < 400:
            classes["3xx"] += 1
        elif 400 <= status < 500:
            classes["4xx"] += 1
        elif status >= 500:
            classes["5xx"] += 1
        else:
            classes["other"] += 1
    table = {key: _public(entry) for key, entry in items}
    slowest = sorted(table.items(), key=lambda item: item[1]["max_ms"], reverse=True)[:limit]
    busiest = sorted(table.items(), key=lambda item: item[1]["requests"], reverse=True)[:limit]
    return {
        "uptime_seconds": round(time.time() - _started_at, 1),
        "total_requests": total_requests,
        "tracked_endpoints": len(table),
        "error_requests": errors,
        "error_rate": round(errors / total_requests, 4) if total_requests else 0.0,
        "slow_threshold_ms": SLOW_REQUEST_MS,
        "slow_requests": sum(entry["slow_requests"] for entry in table.values()),
        "slowest": [{"endpoint": key, **value} for key, value in slowest],
        "busiest": [{"endpoint": key, **value} for key, value in busiest],
    }


def reset():
    """Drop all counters (tests and diagnostics)."""
    global _total_requests, _started_at
    with _lock:
        _counters.clear()
        _total_requests = 0
        _started_at = time.time()
