"""One bounded pass that gathers every window the model needs (RFP sections 35-37).

The recommendation must not fan out into per-account queries, must not scan the global
snapshot and must never call a panel. This module assembles the cycle, the rolling window,
the pre-cycle baseline, the exhaustion inputs and the telemetry freshness in one place,
measures how many statements it took, and hands the pure analytic layers a single object.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import event

from panel.extensions import db
from panel.services.usage_intelligence import cycles as cycle_analytics
from panel.services.usage_intelligence import metrics as usage_metrics
from panel.services.usage_intelligence.events import latest_cycle_boundary
from panel.services.usage_intelligence.schemas import (
    ROLLING_WINDOW_DAYS,
    CycleMetrics,
    Signals,
    WindowMetrics,
)

# RFP section 35: a recommendation may issue at most this many statements.
QUERY_BUDGET = 6


@contextmanager
def count_queries():
    """Count SQL statements executed while the block runs (observability + tests)."""
    counter = {'count': 0}

    def _handler(*_args, **_kwargs):
        counter['count'] += 1

    try:
        event.listen(db.engine, 'before_cursor_execute', _handler)
        listening = True
    except Exception:
        listening = False
    try:
        yield counter
    finally:
        if listening:
            try:
                event.remove(db.engine, 'before_cursor_execute', _handler)
            except Exception:
                pass


@dataclass(frozen=True)
class UsageContext:
    server_id: int
    sub_id: str
    generated_at: datetime
    boundary: object = None
    cycle: CycleMetrics = field(default_factory=CycleMetrics)
    rolling: WindowMetrics = field(default_factory=WindowMetrics)
    baseline: WindowMetrics = field(default_factory=WindowMetrics)
    signals: Signals = field(default_factory=Signals)
    queries: int = 0

    @property
    def has_cycle(self) -> bool:
        return bool(self.cycle.available)


def estimate_expected_duration_days(boundary, *, fallback=ROLLING_WINDOW_DAYS):
    """How long the customer's last package was supposed to last (RFP section 16).

    The business event stores the new expiry and the previous one, which is the most
    direct evidence; ``days`` is the rounded purchase length. Returns None when neither is
    usable, so the caller can skip exhaustion reasoning instead of inventing a horizon.
    """
    if boundary is None:
        return None
    new_expiry = getattr(boundary, 'new_expiry_at', None)
    previous_expiry = getattr(boundary, 'previous_expiry_at', None)
    renewed_at = getattr(boundary, 'renewed_at', None)
    try:
        if isinstance(new_expiry, datetime) and new_expiry > (renewed_at or new_expiry):
            days = (new_expiry - renewed_at).total_seconds() / 86400.0
            if days > 0:
                return float(days)
        if isinstance(new_expiry, datetime) and isinstance(previous_expiry, datetime) \
                and new_expiry > previous_expiry:
            days = (new_expiry - previous_expiry).total_seconds() / 86400.0
            if days > 0:
                return float(days)
    except (TypeError, ValueError):
        pass
    days = getattr(boundary, 'days', None)
    try:
        if days and int(days) > 0:
            return float(int(days))
    except (TypeError, ValueError):
        pass
    return float(fallback) if fallback else None


def _exhaustion_signals(cycle: CycleMetrics, boundary, *, expected_days,
                        limit_bytes) -> dict:
    """Has the quota run out well before its expected duration? (RFP sections 16, 29)."""
    signals = {'early_exhaustion': False, 'severity': 'normal', 'ratio': None}
    if not cycle.available or not expected_days or expected_days <= 0:
        return signals
    limit = None
    if limit_bytes:
        limit = int(limit_bytes)
    elif boundary is not None and getattr(boundary, 'new_volume_limit_bytes', None):
        limit = int(boundary.new_volume_limit_bytes)
    unlimited = bool(getattr(boundary, 'is_unlimited_volume', False)) or limit == 0
    if unlimited or not limit or limit <= 0:
        return signals

    used_ratio = float(cycle.usage_bytes) / float(limit)
    if used_ratio < 1.0:
        return signals

    ratio = float(cycle.elapsed_days) / float(expected_days)
    signals['ratio'] = ratio
    signals['early_exhaustion'] = ratio < 0.60
    if ratio < 0.35:
        signals['severity'] = 'critical'
    elif ratio < 0.60:
        signals['severity'] = 'high'
    return signals


def load_usage_context(server_id, sub_id, *, now=None, live_usage=None,
                       packages=None, rolling_days=ROLLING_WINDOW_DAYS,
                       limit_bytes=None) -> UsageContext:
    """Gather the cycle, rolling window, baseline, freshness and signals in one pass.

    Evidence is loaded once for the widest window the model needs - the older of the
    rolling window and (when a cycle exists) the pre-cycle baseline - and the per-window
    slices are taken in memory. That is what keeps this inside the RFP's query budget
    instead of issuing three near-identical history queries.
    """
    moment = now or datetime.utcnow()
    with count_queries() as counter:
        boundary = latest_cycle_boundary(server_id, sub_id)
        cycle_start = getattr(boundary, 'renewed_at', None)
        rolling_start = moment - timedelta(days=rolling_days)
        baseline_start = (cycle_start - timedelta(days=rolling_days)
                          if isinstance(cycle_start, datetime) else rolling_start)
        evidence = usage_metrics.load_evidence(
            server_id, sub_id,
            since=min(rolling_start, baseline_start),
            live_usage=live_usage,
        )
        cycle = cycle_analytics.build_current_cycle(
            server_id, sub_id, now=moment, live_usage=live_usage, boundary=boundary,
            evidence=evidence)
        rolling = cycle_analytics.build_rolling_window(
            server_id, sub_id, now=moment, window_days=rolling_days,
            live_usage=live_usage, evidence=evidence)
        baseline = cycle_analytics.build_historical_baseline(
            server_id, sub_id, cycle_start=cycle.started_at if cycle.available else None,
            window_days=rolling_days, evidence=evidence)

    freshness_state, age = usage_metrics.freshness(evidence.observed_at, now=moment)
    expected_days = estimate_expected_duration_days(boundary)
    exhaustion = _exhaustion_signals(cycle, boundary, expected_days=expected_days,
                                    limit_bytes=limit_bytes)
    signals = Signals(
        early_exhaustion=bool(exhaustion['early_exhaustion']),
        exhaustion_severity=str(exhaustion['severity']),
        exhaustion_ratio=exhaustion['ratio'],
        telemetry_stale=(freshness_state == 'stale'),
        telemetry_freshness=freshness_state,
        telemetry_age_seconds=age,
        expected_duration_days=(round(expected_days, 2) if expected_days else None),
    )
    return UsageContext(
        server_id=int(server_id),
        sub_id=str(sub_id),
        generated_at=moment,
        boundary=boundary,
        cycle=cycle,
        rolling=rolling,
        baseline=baseline,
        signals=signals,
        queries=int(counter['count']),
    )
