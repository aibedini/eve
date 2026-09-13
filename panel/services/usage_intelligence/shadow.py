"""Shadow mode: compute v5 beside v4, compare, and change nothing yet.

RFP sections 55-58. The rollout needs evidence before it needs activation, so the comparison
is recorded as structured, PII-free counters plus one aggregate line per interval:

* how often the package would change,
* how far the forecast moved (percent),
* how often the capacity-limited flag differs,
* how many strong trends and early exhaustions were seen.

The logs deliberately carry only ids and numbers: no email, no phone, no subscription token
(RFP sections 41-42). Activation and removal are separate steps.
"""
import threading
from datetime import datetime

from panel.core.logging_config import get_resilient_logger

logger = get_resilient_logger('eve.usage')

_lock = threading.Lock()
_counters = {
    'comparisons': 0,
    'package_changed': 0,
    'capacity_limited_delta': 0,
    'strong_trend': 0,
    'early_exhaustion': 0,
    'v5_unavailable': 0,
    'forecast_delta_sum': 0.0,
    'forecast_delta_max': 0.0,
}


def reset_shadow_metrics() -> None:
    with _lock:
        for key in _counters:
            _counters[key] = 0.0 if key.endswith('_sum') or key.endswith('_max') else 0


def shadow_metrics() -> dict:
    with _lock:
        snapshot = dict(_counters)
    comparisons = snapshot['comparisons'] or 0
    return {
        'comparisons': comparisons,
        'package_changed_percent': round(
            100.0 * snapshot['package_changed'] / comparisons, 2) if comparisons else 0.0,
        'forecast_delta_percent_mean': round(
            snapshot['forecast_delta_sum'] / comparisons, 2) if comparisons else 0.0,
        'forecast_delta_percent_max': round(snapshot['forecast_delta_max'], 2),
        'capacity_limited_delta': snapshot['capacity_limited_delta'],
        'strong_trend_count': snapshot['strong_trend'],
        'early_exhaustion_count': snapshot['early_exhaustion'],
        'v5_unavailable': snapshot['v5_unavailable'],
    }


def _forecast_delta_percent(v4, v5):
    try:
        old = float((v4 or {}).get('projected_31d_gb') or 0.0)
        new = float(((v5 or {}).get('forecast') or {}).get('projected_31d_gb') or 0.0)
    except (TypeError, ValueError):
        return None
    if old <= 0:
        return None if new <= 0 else 100.0
    return abs(new - old) / old * 100.0


def record_shadow_comparison(server_id, sub_id, v4, v5, *, account=None) -> dict:
    """Count and log one v4/v5 comparison; never raises into the recommendation path."""
    v5_missing = v5 is None
    delta = _forecast_delta_percent(v4, v5)
    package_changed = bool(
        not v5_missing
        and (v4 or {}).get('package_id') != ((v5 or {}).get('recommendation') or {}).get('package_id'))
    capacity_delta = bool(
        not v5_missing
        and bool((v4 or {}).get('capacity_limited'))
        != bool(((v5 or {}).get('recommendation') or {}).get('capacity_limited')))
    trend_state = ((v5 or {}).get('trend') or {}).get('state')
    early_exhaustion = bool(((v5 or {}).get('signals') or {}).get('early_exhaustion'))

    with _lock:
        _counters['comparisons'] += 1
        if v5_missing:
            _counters['v5_unavailable'] += 1
        if package_changed:
            _counters['package_changed'] += 1
        if capacity_delta:
            _counters['capacity_limited_delta'] += 1
        if trend_state == 'strong_increase':
            _counters['strong_trend'] += 1
        if early_exhaustion:
            _counters['early_exhaustion'] += 1
        if delta is not None:
            _counters['forecast_delta_sum'] += delta
            _counters['forecast_delta_max'] = max(_counters['forecast_delta_max'], delta)

    try:
        logger.info(
            'usage-fit shadow server_id=%s account=%s v4_forecast=%s v5_forecast=%s '
            'v4_package=%s v5_package=%s package_changed=%s forecast_delta_pct=%s '
            'trend=%s early_exhaustion=%s capacity_limited_delta=%s',
            server_id, account or 'redacted',
            (v4 or {}).get('projected_31d_gb'),
            ((v5 or {}).get('forecast') or {}).get('projected_31d_gb'),
            (v4 or {}).get('package_id'),
            ((v5 or {}).get('recommendation') or {}).get('package_id'),
            package_changed,
            (None if delta is None else round(delta, 2)),
            trend_state, early_exhaustion, capacity_delta,
        )
    except Exception:
        pass
    try:
        from panel.services.usage_intelligence.observability import note_shadow_comparison
        note_shadow_comparison()
    except Exception:
        pass

    return {
        'at': datetime.utcnow().isoformat(),
        'server_id': int(server_id) if server_id is not None else None,
        'v5_available': not v5_missing,
        'package_changed': package_changed,
        'forecast_delta_percent': (None if delta is None else round(delta, 2)),
        'trend_state': trend_state,
        'early_exhaustion': early_exhaustion,
        'capacity_limited_delta': capacity_delta,
    }
