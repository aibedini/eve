"""Cycle and window construction (RFP sections 11-14).

``build_current_cycle`` anchors on the latest *verified* business event - never on a
counter movement - and turns it into rates with a precise elapsed time, a floor that keeps
minutes-old evidence from producing an absurd forecast, and a maturity bucket that says how
much the cycle may be trusted. The rolling window and the pre-cycle baseline are built
independently; none of them replaces another (RFP section 2).
"""
from datetime import datetime

from panel.services.usage_intelligence import metrics as usage_metrics
from panel.services.usage_intelligence.events import latest_cycle_boundary
from panel.services.usage_intelligence.schemas import (
    MIN_EFFECTIVE_ELAPSED_DAYS,
    ROLLING_WINDOW_DAYS,
    CycleMetrics,
    WindowMetrics,
    maturity_for,
)

BYTES_PER_GB = float(1024 ** 3)


def build_current_cycle(server_id, sub_id, *, now=None, live_usage=None,
                        boundary=None, daily_rows=None) -> CycleMetrics:
    """Metrics for the cycle that started at the latest verified boundary."""
    moment = now or datetime.utcnow()
    if boundary is None:
        boundary = latest_cycle_boundary(server_id, sub_id)
    if boundary is None:
        return CycleMetrics(available=False, reason='no_verified_renewal')

    started_at = boundary.renewed_at
    if not isinstance(started_at, datetime):
        return CycleMetrics(available=False, reason='boundary_without_timestamp')
    if started_at > moment:
        # A boundary in the future is clock skew, not evidence.
        started_at = moment

    elapsed_days = max(0.0, (moment - started_at).total_seconds() / 86400.0)
    effective_days = max(elapsed_days, MIN_EFFECTIVE_ELAPSED_DAYS)

    usage = usage_metrics.cycle_usage(
        server_id, sub_id, boundary, live_usage=live_usage, daily_rows=daily_rows)
    usage_bytes = max(0, int(usage.get('usage_bytes') or 0))

    rate = (usage_bytes / BYTES_PER_GB) / effective_days if effective_days > 0 else 0.0
    return CycleMetrics(
        available=True,
        started_at=started_at,
        event_type=str(getattr(boundary, 'event_type', '') or ''),
        traffic_reset=bool(getattr(boundary, 'traffic_reset', False)),
        elapsed_days=elapsed_days,
        effective_elapsed_days=effective_days,
        usage_bytes=usage_bytes,
        usage_source=str(usage.get('usage_source') or 'none'),
        daily_sum_bytes=max(0, int(usage.get('daily_sum_bytes') or 0)),
        average_daily_gb=float(rate),
        projected_31d_gb=float(rate) * ROLLING_WINDOW_DAYS,
        maturity=maturity_for(elapsed_days),
        granted_volume_bytes=getattr(boundary, 'granted_volume_bytes', None),
        new_volume_limit_bytes=getattr(boundary, 'new_volume_limit_bytes', None),
        previous_volume_limit_bytes=getattr(boundary, 'previous_volume_limit_bytes', None),
    )


def build_rolling_window(server_id, sub_id, *, now=None, window_days=ROLLING_WINDOW_DAYS,
                         live_usage=None) -> WindowMetrics:
    """The rolling window: kept as evidence, no longer the only basis (section 13)."""
    return usage_metrics.load_rolling_window(
        server_id, sub_id, now=now, window_days=window_days, live_usage=live_usage)


def build_historical_baseline(server_id, sub_id, *, cycle_start, window_days=ROLLING_WINDOW_DAYS
                              ) -> WindowMetrics:
    """The 31 days before the cycle started, when there is a cycle to compare with."""
    if cycle_start is None:
        return WindowMetrics(available=False, label='baseline')
    return usage_metrics.load_baseline_window(
        server_id, sub_id, cycle_start=cycle_start, window_days=window_days)
