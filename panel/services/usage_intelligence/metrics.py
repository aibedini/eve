"""Repository layer: the only place the analytics reads usage history (RFP section 65).

Telemetry queries are bounded and indexed:

* one row per account per Tehran day in ``UsageDaily`` (already keyed by
  ``(server_id, sub_id, usage_date)``), filtered by the row's observed timestamps so a
  window never scans the whole table;
* the latest counter from ``UsageCounterState``, or the live counter the caller supplies
  from the snapshot (the recommendation itself never calls a panel).

Nothing here decides anything: it returns numbers and their provenance, so the analytic
functions stay pure and unit testable.
"""
from datetime import datetime, timedelta

from panel.models import UsageCounterState, UsageDaily

from panel.services.usage_intelligence.schemas import (
    FRESHNESS_ACCEPTABLE_SECONDS,
    FRESHNESS_FRESH_SECONDS,
    CycleMetrics,
    WindowMetrics,
)

BYTES_PER_GB = float(1024 ** 3)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def counter_delta(current, baseline):
    """Reset-safe subtraction: a counter that moved backwards restarted at zero."""
    present = max(0, _as_int(current))
    earlier = max(0, _as_int(baseline))
    return present - earlier if present >= earlier else present


def raw_counter(server_id, sub_id, *, live_usage=None):
    """(total_bytes, observed_at, source) for the newest counter we can see.

    ``live_usage`` comes from the in-process snapshot the caller already has; the stored
    ``UsageCounterState`` is the fallback. Both are telemetry: neither may be interpreted
    as a commercial event.
    """
    live_total = None
    live_observed = None
    if isinstance(live_usage, dict) and live_usage:
        try:
            live_total = max(0, int(live_usage.get('total_bytes') or 0))
        except (TypeError, ValueError):
            live_total = None
        observed = live_usage.get('observed_at')
        live_observed = observed if isinstance(observed, datetime) else None

    state = None
    try:
        state = UsageCounterState.query.filter_by(
            server_id=int(server_id), sub_id=str(sub_id)).first()
    except Exception:
        state = None

    state_total = max(0, _as_int(getattr(state, 'total_bytes', 0))) if state else None
    state_observed = getattr(state, 'observed_at', None) if state else None

    if live_total is not None and state_total is not None:
        # The live counter can rotate between inbounds; the higher reading is canonical,
        # but a fresh live reading that is lower means the counter was reset.
        if live_total >= state_total:
            return live_total, live_observed or state_observed, 'live'
        return live_total, live_observed or state_observed, 'live_reset'
    if live_total is not None:
        return live_total, live_observed, 'live'
    if state_total is not None:
        return state_total, state_observed, 'stored'
    return None, None, 'none'


def freshness(observed_at, *, now=None):
    """(state, age_seconds): fresh / acceptable / stale (RFP section 20)."""
    if not isinstance(observed_at, datetime):
        return 'unknown', None
    moment = now or datetime.utcnow()
    age = max(0.0, (moment - observed_at).total_seconds())
    if age <= FRESHNESS_FRESH_SECONDS:
        return 'fresh', age
    if age <= FRESHNESS_ACCEPTABLE_SECONDS:
        return 'acceptable', age
    return 'stale', age


def load_daily_rows(server_id, sub_id, *, since=None, until=None, limit=400):
    """UsageDaily rows for one account inside a half-open time window.

    Filters on the row's own observation timestamps (indexed with the unique
    ``(server_id, sub_id, usage_date)`` constraint for the account), never on a full-table
    scan, and never loads more than ``limit`` rows.
    """
    try:
        query = UsageDaily.query.filter_by(server_id=int(server_id), sub_id=str(sub_id))
        if since is not None:
            query = query.filter(UsageDaily.last_observed_at >= since)
        if until is not None:
            query = query.filter(UsageDaily.last_observed_at < until)
        return (query.order_by(UsageDaily.usage_date.asc()).limit(limit).all())
    except Exception:
        return []


def _evidence(rows):
    total = 0
    samples = 0
    dates = set()
    for row in rows:
        total += max(0, _as_int(getattr(row, 'upload_bytes', 0))) + \
            max(0, _as_int(getattr(row, 'download_bytes', 0)))
        samples += max(0, _as_int(getattr(row, 'sample_count', 0)))
        if getattr(row, 'usage_date', None) is not None:
            dates.add(row.usage_date)
    return total, samples, dates


def _basis_days(dates, *, cap):
    if not dates:
        return 0.0
    ordered = sorted(dates)
    span = float((ordered[-1] - ordered[0]).days + 1)
    return min(float(cap), max(1.0, span))


def load_window(server_id, sub_id, *, since, until=None, window_days=31,
                live_usage=None, label='') -> WindowMetrics:
    """A bounded usage window with its own basis, rate and 31-day projection.

    ``live_usage`` closes the blind spot between the latest stored counter and now: only
    the traffic observed *since* that stored value is added, so the same bytes are never
    counted twice.
    """
    rows = load_daily_rows(server_id, sub_id, since=since, until=until)
    total, samples, dates = _evidence(rows)
    basis = _basis_days(dates, cap=window_days)

    live_total, live_observed, _source = raw_counter(server_id, sub_id, live_usage=live_usage)
    state_total = None
    try:
        state = UsageCounterState.query.filter_by(
            server_id=int(server_id), sub_id=str(sub_id)).first()
        state_total = max(0, _as_int(getattr(state, 'total_bytes', 0))) if state else None
    except Exception:
        state_total = None

    if live_total is not None and total > 0:
        increment = counter_delta(live_total, state_total if state_total is not None else 0) \
            if state_total is not None else 0
        total += max(0, increment)
    elif total == 0 and live_total:
        # No daily rollup yet: the live counter is the only evidence available.
        total = live_total
        basis = basis or min(float(window_days), 1.0)

    if basis <= 0:
        basis = 0.0
    rate = (total / BYTES_PER_GB) / basis if basis > 0 else 0.0
    return WindowMetrics(
        available=bool(total > 0 and basis > 0),
        label=label,
        window_days=window_days,
        usage_bytes=int(total),
        basis_days=float(basis),
        observed_dates=len(dates),
        samples=int(samples),
        average_daily_gb=float(rate),
        projected_31d_gb=float(rate) * window_days,
    )


def load_rolling_window(server_id, sub_id, *, now=None, window_days=31, live_usage=None):
    """The rolling window (RFP section 13): still computed, no longer sufficient alone."""
    moment = now or datetime.utcnow()
    return load_window(
        server_id, sub_id,
        since=moment - timedelta(days=window_days),
        window_days=window_days,
        live_usage=live_usage,
        label='rolling',
    )


def load_baseline_window(server_id, sub_id, *, cycle_start, window_days=31,
                         live_usage=None):
    """The window immediately before the cycle: behaviour-change evidence (section 14)."""
    if not isinstance(cycle_start, datetime):
        return WindowMetrics(available=False, label='baseline')
    start = cycle_start - timedelta(days=window_days)
    # Live counters are deliberately excluded: they describe *now*, not the past window.
    return load_window(
        server_id, sub_id,
        since=start, until=cycle_start,
        window_days=window_days,
        live_usage=None,
        label='baseline',
    )


def cycle_usage(server_id, sub_id, boundary, *, live_usage=None, daily_rows=None):
    """Usage inside the current cycle, with its provenance.

    The counter anchor is exact and is preferred:

    * ``traffic_reset`` renewals restart the counter, so the counter *is* the cycle usage;
    * otherwise the counter is cumulative, so the cycle usage is the counter minus what it
      already read at the boundary - which is ``previous_volume_limit_bytes -
      previous_remaining_bytes`` for a limited account, both of which the business event
      stores.

    Without an anchor (an unlimited account, or a pre-v2 event) the daily rows observed
    since the boundary are summed instead; that fallback is accurate to the day the cycle
    started, which the returned ``usage_source`` says out loud. Both numbers are returned
    so the confidence layer can see a disagreement.
    """
    rows = daily_rows if daily_rows is not None else load_daily_rows(
        server_id, sub_id, since=getattr(boundary, 'renewed_at', None))
    daily_total, samples, dates = _evidence(rows)

    live_total, live_observed, _source = raw_counter(server_id, sub_id, live_usage=live_usage)
    boundary_type = str(getattr(boundary, 'event_type', '') or '')
    traffic_reset = bool(getattr(boundary, 'traffic_reset', False))

    baseline = None
    previous_limit = _as_int(getattr(boundary, 'previous_volume_limit_bytes', None), None)
    previous_remaining = _as_int(getattr(boundary, 'previous_remaining_bytes', None), None)
    if traffic_reset:
        baseline = 0
    elif previous_limit and previous_remaining is not None:
        baseline = max(0, previous_limit - previous_remaining)

    if live_total is not None and baseline is not None:
        return {
            'usage_bytes': counter_delta(live_total, baseline),
            'usage_source': 'counter_delta',
            'daily_sum_bytes': int(daily_total),
            'samples': int(samples),
            'dates': len(dates),
            'observed_at': live_observed,
            'boundary_event_type': boundary_type,
        }
    if daily_total > 0:
        return {
            'usage_bytes': int(daily_total),
            'usage_source': 'daily_sum',
            'daily_sum_bytes': int(daily_total),
            'samples': int(samples),
            'dates': len(dates),
            'observed_at': live_observed,
            'boundary_event_type': boundary_type,
        }
    if live_total:
        return {
            'usage_bytes': int(live_total),
            'usage_source': 'live_counter',
            'daily_sum_bytes': int(daily_total),
            'samples': int(samples),
            'dates': len(dates),
            'observed_at': live_observed,
            'boundary_event_type': boundary_type,
        }
    return {
        'usage_bytes': 0,
        'usage_source': 'none',
        'daily_sum_bytes': 0,
        'samples': 0,
        'dates': 0,
        'observed_at': live_observed,
        'boundary_event_type': boundary_type,
    }


def empty_cycle(reason: str) -> CycleMetrics:
    return CycleMetrics(available=False, reason=reason)
