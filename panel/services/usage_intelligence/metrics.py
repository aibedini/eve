"""Repository layer: the only place the analytics reads usage history (RFP section 65).

Telemetry queries are bounded, indexed and few (RFP section 35): one pass over the
account's ``UsageDaily`` rows for the widest window the model needs, one read of the latest
``UsageCounterState``, and the live counter the caller already has from the snapshot (the
recommendation itself never calls a panel). Everything else is pure arithmetic over those
rows, so the analytic layers stay unit testable and the query budget stays provable.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from panel.models import UsageCounterState, UsageDaily

from panel.services.usage_intelligence.schemas import (
    FRESHNESS_ACCEPTABLE_SECONDS,
    FRESHNESS_FRESH_SECONDS,
    WindowMetrics,
)

BYTES_PER_GB = float(1024 ** 3)
MAX_DAILY_ROWS = 400


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


@dataclass(frozen=True)
class UsageEvidence:
    """Everything telemetry can say about one account, loaded once per analysis."""
    rows: tuple = ()
    state_total: int | None = None
    state_observed: datetime | None = None
    live_total: int | None = None
    live_observed: datetime | None = None
    counter_source: str = 'none'

    @property
    def total_bytes(self):
        """The newest counter we can see (live wins over stored)."""
        if self.live_total is not None:
            return self.live_total
        return self.state_total

    @property
    def observed_at(self):
        return self.live_observed or self.state_observed


def load_state(server_id, sub_id):
    """The stored counter row for one account, or None."""
    try:
        return UsageCounterState.query.filter_by(
            server_id=int(server_id), sub_id=str(sub_id)).first()
    except Exception:
        return None


def load_daily_rows(server_id, sub_id, *, since=None, until=None, limit=MAX_DAILY_ROWS):
    """UsageDaily rows for one account inside a half-open observed-time window.

    Filtered per account (the unique ``(server_id, sub_id, usage_date)`` key) and bounded by
    ``limit``: never a full-table scan, never the whole history.
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


def load_evidence(server_id, sub_id, *, since=None, live_usage=None,
                  limit=MAX_DAILY_ROWS, state=None) -> UsageEvidence:
    """One query for the daily rows, one for the counter, plus the caller's live reading."""
    sid = _as_int(server_id)
    account = str(sub_id or '').strip()
    rows = load_daily_rows(sid, account, since=since, limit=limit) if account else []
    if state is None:
        state = load_state(sid, account)
    state_total = max(0, _as_int(getattr(state, 'total_bytes', 0))) if state else None
    state_observed = getattr(state, 'observed_at', None) if state else None

    live_total = None
    live_observed = None
    if isinstance(live_usage, dict) and live_usage:
        try:
            live_total = max(0, int(live_usage.get('total_bytes') or 0))
        except (TypeError, ValueError):
            live_total = None
        observed = live_usage.get('observed_at')
        live_observed = observed if isinstance(observed, datetime) else None

    if live_total is not None and state_total is not None:
        # A live reading below the stored one means the counter was reset, not that the
        # stored value is newer: the live counter is the canonical observation.
        source = 'live' if live_total >= state_total else 'live_reset'
    elif live_total is not None:
        source = 'live'
    elif state_total is not None:
        source = 'stored'
    else:
        source = 'none'

    return UsageEvidence(
        rows=tuple(rows),
        state_total=state_total,
        state_observed=state_observed,
        live_total=live_total,
        live_observed=live_observed,
        counter_source=source,
    )


def raw_counter(server_id, sub_id, *, live_usage=None, evidence=None):
    """(total_bytes, observed_at, source) for the newest counter we can see."""
    if evidence is None:
        evidence = load_evidence(server_id, sub_id, live_usage=live_usage)
    return evidence.total_bytes, evidence.observed_at, evidence.counter_source


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


def _row_total(row):
    return (max(0, _as_int(getattr(row, 'upload_bytes', 0)))
            + max(0, _as_int(getattr(row, 'download_bytes', 0))))


def rows_in_window(rows, *, since=None, until=None):
    """Slice the loaded rows in memory: the widest query serves every window."""
    selected = []
    for row in rows or ():
        observed = getattr(row, 'last_observed_at', None)
        if since is not None and (not isinstance(observed, datetime) or observed < since):
            continue
        if until is not None and isinstance(observed, datetime) and observed >= until:
            continue
        selected.append(row)
    return selected


def daily_totals(rows, *, since=None, until=None):
    """Per-day usage in GB for the rows in a window (robust bounds + variance)."""
    return [_row_total(row) / BYTES_PER_GB
            for row in rows_in_window(rows, since=since, until=until)]


def window_metrics(rows, evidence, *, since=None, until=None, window_days=31,
                   live_usage=None, label='', include_live=True) -> WindowMetrics:
    """A bounded window's rate and 31-day projection, from already-loaded evidence."""
    selected = rows_in_window(rows, since=since, until=until)
    total = 0
    samples = 0
    dates = set()
    for row in selected:
        total += _row_total(row)
        samples += max(0, _as_int(getattr(row, 'sample_count', 0)))
        if getattr(row, 'usage_date', None) is not None:
            dates.add(row.usage_date)

    basis = 0.0
    if dates:
        ordered = sorted(dates)
        basis = min(float(window_days), max(1.0, float((ordered[-1] - ordered[0]).days + 1)))

    if include_live and evidence is not None and evidence.total_bytes is not None:
        if total > 0:
            # Close the blind spot between the stored counter and now, without counting
            # the same bytes twice.
            increment = (counter_delta(evidence.total_bytes, evidence.state_total or 0)
                         if evidence.state_total is not None else 0)
            total += max(0, increment)
        elif evidence.total_bytes:
            # No rollup yet: the live counter is the only evidence there is.
            total = int(evidence.total_bytes)
            basis = basis or min(float(window_days), 1.0)

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


def load_window(server_id, sub_id, *, since, until=None, window_days=31,
                live_usage=None, label='', evidence=None) -> WindowMetrics:
    """A bounded usage window, loading evidence when the caller has none."""
    if evidence is None:
        evidence = load_evidence(server_id, sub_id, since=since, live_usage=live_usage)
    return window_metrics(evidence.rows, evidence, since=since, until=until,
                          window_days=window_days, live_usage=live_usage, label=label)


def load_rolling_window(server_id, sub_id, *, now=None, window_days=31, live_usage=None,
                        evidence=None):
    """The rolling window (RFP section 13): still computed, no longer sufficient alone."""
    moment = now or datetime.utcnow()
    since = moment - timedelta(days=window_days)
    if evidence is None:
        evidence = load_evidence(server_id, sub_id, since=since, live_usage=live_usage)
    return window_metrics(evidence.rows, evidence, since=since, window_days=window_days,
                          live_usage=live_usage, label='rolling')


def load_baseline_window(server_id, sub_id, *, cycle_start, window_days=31,
                         live_usage=None, evidence=None):
    """The window immediately before the cycle: behaviour-change evidence (section 14)."""
    if not isinstance(cycle_start, datetime):
        return WindowMetrics(available=False, label='baseline')
    start = cycle_start - timedelta(days=window_days)
    if evidence is None:
        # Live counters describe *now*, so they are never used for the past window.
        evidence = load_evidence(server_id, sub_id, since=start, live_usage=None)
    return window_metrics(evidence.rows, evidence, since=start, until=cycle_start,
                          window_days=window_days, label='baseline', include_live=False)


def cycle_usage(server_id, sub_id, boundary, *, live_usage=None, daily_rows=None,
                evidence=None):
    """Usage inside the current cycle, with its provenance.

    The counter anchor is exact and preferred:

    * ``traffic_reset`` renewals restart the counter, so the counter *is* the cycle usage;
    * otherwise the counter is cumulative, so the cycle usage is the counter minus what it
      already read at the boundary - ``previous_volume_limit_bytes -
      previous_remaining_bytes``, both stored on the business event.

    Without an anchor (an unlimited account, or a pre-v2 event) the daily rows observed
    since the boundary are summed instead; that fallback is accurate to the day the cycle
    started, which the returned ``usage_source`` says out loud. Both numbers are returned so
    the confidence layer can see a disagreement.
    """
    boundary_at = getattr(boundary, 'renewed_at', None)
    if evidence is None:
        evidence = load_evidence(server_id, sub_id, since=boundary_at, live_usage=live_usage)
    rows = (daily_rows if daily_rows is not None
            else rows_in_window(evidence.rows, since=boundary_at))
    daily_total = sum(_row_total(row) for row in rows)
    samples = sum(max(0, _as_int(getattr(row, 'sample_count', 0))) for row in rows)
    dates = {getattr(row, 'usage_date', None) for row in rows}
    dates.discard(None)

    boundary_type = str(getattr(boundary, 'event_type', '') or '')
    traffic_reset = bool(getattr(boundary, 'traffic_reset', False))
    current_total = evidence.total_bytes

    baseline = None
    previous_limit = _as_int(getattr(boundary, 'previous_volume_limit_bytes', None), None)
    previous_remaining = _as_int(getattr(boundary, 'previous_remaining_bytes', None), None)
    if traffic_reset:
        baseline = 0
    elif previous_limit and previous_remaining is not None:
        baseline = max(0, previous_limit - previous_remaining)

    if current_total is not None and baseline is not None:
        usage_bytes, source = counter_delta(current_total, baseline), 'counter_delta'
    elif daily_total > 0:
        usage_bytes, source = int(daily_total), 'daily_sum'
    elif current_total:
        usage_bytes, source = int(current_total), 'live_counter'
    else:
        usage_bytes, source = 0, 'none'

    return {
        'usage_bytes': int(usage_bytes),
        'usage_source': source,
        'daily_sum_bytes': int(daily_total),
        'samples': int(samples),
        'dates': len(dates),
        'observed_at': evidence.observed_at,
        'boundary_event_type': boundary_type,
    }
