"""Observability for the recommendation path (RFP sections 43-44).

Dependency-free, bounded, in-process counters in the same style as
``panel/core/http_metrics.py`` (the project deliberately does not require a metrics stack),
plus one structured log line per recommendation on the ``eve.usage`` channel.

Privacy is a property of this module, not of its callers (RFP sections 41-42): an account is
only ever identified by a stable, non-reversible reference, and the log line carries ids and
numbers - never an email, a phone number, a UUID or a subscription token.
"""
import hashlib
import threading
import time
from collections import OrderedDict, deque

from panel.core.logging_config import get_resilient_logger

logger = get_resilient_logger('eve.usage')

MAX_KEYS = 100
MAX_LATENCY_SAMPLES = 500

# RFP section 43 trend buckets.
TREND_BUCKETS = {
    'strong_decrease': 'decrease',
    'decreasing': 'decrease',
    'stable': 'stable',
    'increasing': 'increase',
    'strong_increase': 'strong_increase',
    'unknown': 'unknown',
}

_lock = threading.Lock()
_started_at = time.time()
_counters = {
    'total': 0,
    'errors': 0,
    'early_exhaustion': 0,
    'stale_telemetry': 0,
    'capacity_limited': 0,
    'cycle_available': 0,
    'shadow_comparisons': 0,
}
_basis = OrderedDict()
_trends = OrderedDict()
_latency = deque(maxlen=MAX_LATENCY_SAMPLES)
_latency_total_ms = 0.0
_latency_max_ms = 0.0


def reset() -> None:
    global _latency_total_ms, _latency_max_ms, _started_at
    with _lock:
        for key in _counters:
            _counters[key] = 0
        _basis.clear()
        _trends.clear()
        _latency.clear()
        _latency_total_ms = 0.0
        _latency_max_ms = 0.0
        _started_at = time.time()


def _bump(table, key):
    if not key:
        return
    name = str(key)[:40]
    entry = table.get(name)
    if entry is None:
        table[name] = 1
        while len(table) > MAX_KEYS:
            table.popitem(last=False)
    else:
        table[name] = entry + 1
        table.move_to_end(name)


def redact_ref(value) -> str:
    """A stable, non-reversible account reference for logs (no email/phone/token)."""
    raw = str(value or '').strip()
    if not raw:
        return 'redacted'
    digest = hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:12]
    return 'acct-' + digest


def _p95(samples):
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * len(ordered))) - 1))
    return float(ordered[index])


def observe(*, latency_ms=None, payload=None, account=None, trend_state=None, basis=None,
            early_exhaustion=None, stale_telemetry=None, capacity_limited=None,
            error=None, server_id=None) -> dict:
    """Record one recommendation (or one failure) and log it without PII."""
    payload = payload or {}
    cycle = payload.get('current_cycle') or {}
    rolling = payload.get('rolling_31d') or {}
    trend = payload.get('trend') or {}
    forecast = payload.get('forecast') or {}
    signals = payload.get('signals') or {}
    confidence = payload.get('confidence') or {}
    recommendation = payload.get('recommendation') or {}

    trend_state = trend_state or trend.get('state') or 'unknown'
    basis = basis or payload.get('forecast_basis')
    if early_exhaustion is None:
        early_exhaustion = bool(signals.get('early_exhaustion'))
    if stale_telemetry is None:
        stale_telemetry = bool(signals.get('telemetry_stale'))
    if capacity_limited is None:
        capacity_limited = bool(recommendation.get('capacity_limited'))

    try:
        latency = max(0.0, float(latency_ms)) if latency_ms is not None else None
    except (TypeError, ValueError):
        latency = None

    global _latency_total_ms, _latency_max_ms
    with _lock:
        _counters['total'] += 1
        if error:
            _counters['errors'] += 1
        if early_exhaustion:
            _counters['early_exhaustion'] += 1
        if stale_telemetry:
            _counters['stale_telemetry'] += 1
        if capacity_limited:
            _counters['capacity_limited'] += 1
        if cycle.get('available'):
            _counters['cycle_available'] += 1
        _bump(_basis, basis)
        _bump(_trends, TREND_BUCKETS.get(trend_state, 'unknown'))
        if latency is not None:
            _latency.append(latency)
            _latency_total_ms += latency
            _latency_max_ms = max(_latency_max_ms, latency)

    entry = {
        'model': payload.get('model_version') or None,
        'server_id': server_id,
        'account': redact_ref(account),
        'cycle_days': cycle.get('elapsed_days'),
        'cycle_rate': cycle.get('average_daily_gb'),
        'rolling_rate': rolling.get('average_daily_gb'),
        'trend_ratio': trend.get('ratio'),
        'forecast': forecast.get('projected_31d_gb'),
        'basis': basis,
        'recommended_package': recommendation.get('package_id'),
        'confidence': confidence.get('data') or payload.get('confidence_label'),
        'behavior_stability': confidence.get('behavior_stability'),
        'early_exhaustion': bool(early_exhaustion),
        'capacity_limited': bool(capacity_limited),
        'latency_ms': (round(latency, 2) if latency is not None else None),
        'error': (str(error)[:200] if error else None),
    }
    try:
        if error:
            logger.warning(
                'usage-fit model=%s server_id=%s account=%s error=%s latency_ms=%s',
                entry['model'] or 'usage-fit-v5', entry['server_id'], entry['account'],
                entry['error'], entry['latency_ms'])
        else:
            logger.info(
                'usage-fit model=%s server_id=%s account=%s cycle_days=%s cycle_rate=%s '
                'rolling_rate=%s trend_ratio=%s trend=%s basis=%s forecast=%s '
                'recommended_package=%s confidence=%s behavior_stability=%s '
                'early_exhaustion=%s capacity_limited=%s latency_ms=%s',
                entry['model'] or 'usage-fit-v5', entry['server_id'], entry['account'],
                entry['cycle_days'], entry['cycle_rate'], entry['rolling_rate'],
                entry['trend_ratio'], trend_state, entry['basis'], entry['forecast'],
                entry['recommended_package'], entry['confidence'],
                entry['behavior_stability'], entry['early_exhaustion'],
                entry['capacity_limited'], entry['latency_ms'])
    except Exception:
        pass
    return entry


def note_shadow_comparison() -> None:
    with _lock:
        _counters['shadow_comparisons'] += 1


def snapshot() -> dict:
    """Aggregated counters for /api/doctor (never per-account)."""
    with _lock:
        counters = dict(_counters)
        basis = dict(_basis)
        trends = dict(_trends)
        samples = list(_latency)
        total_ms = _latency_total_ms
        max_ms = _latency_max_ms
    count = len(samples)
    return {
        'uptime_seconds': round(time.time() - _started_at, 1),
        'recommendation_total': counters['total'],
        'recommendation_errors_total': counters['errors'],
        'cycle_available_total': counters['cycle_available'],
        'early_exhaustion_total': counters['early_exhaustion'],
        'stale_telemetry_total': counters['stale_telemetry'],
        'capacity_limited_total': counters['capacity_limited'],
        'shadow_comparisons_total': counters['shadow_comparisons'],
        'basis': basis,
        'trend': trends,
        'latency': {
            'samples': count,
            'mean_ms': round(total_ms / count, 2) if count else 0.0,
            'p95_ms': round(_p95(samples), 2),
            'max_ms': round(max_ms, 2),
        },
    }
