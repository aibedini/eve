"""Usage Intelligence: deterministic analytics over telemetry + business events.

The RFP's architectural rule, enforced by this package's layout:

* **telemetry** - ``UsageCounterState`` / ``UsageHourly`` / ``UsageDaily`` say how much
  traffic was recorded (owned by ``panel/jobs/schedulers.py``);
* **business events** - ``RenewalEvent`` says when a customer's commercial cycle
  changed and on what terms (written by ``events.py`` from an explicit, verified
  mutation, never inferred from a counter movement);
* **analytics** - cycles, metrics, trend, forecast, confidence and package selection
  combine the two and may never impersonate either.

The analytics modules are pure functions over dataclasses so they can be unit tested
without Flask, the database or a panel (RFP sections 63-65).
"""
from panel.services.usage_intelligence.cycles import (  # noqa: F401
    build_current_cycle,
    build_historical_baseline,
    build_rolling_window,
)
from panel.services.usage_intelligence.forecast import (  # noqa: F401
    daily_series_stats,
    forecast_usage,
    robust_cap,
    robust_rate,
    safety_margin_for,
)
from panel.services.usage_intelligence.events import (  # noqa: F401
    has_recent_cycle_boundary,
    latest_cycle_boundary,
    record_inferred_reset,
    record_renewal_event,
    record_verified_renewal,
)
from panel.services.usage_intelligence.packages import (  # noqa: F401
    required_for,
    select_packages,
)
from panel.services.usage_intelligence.recommendation import (  # noqa: F401
    build_recommendation_v5,
    recommendation_mode,
)
from panel.services.usage_intelligence.shadow import (  # noqa: F401
    record_shadow_comparison,
    reset_shadow_metrics,
    shadow_metrics,
)
from panel.services.usage_intelligence.confidence import (  # noqa: F401
    assess_behavior_stability,
    assess_confidence,
    assess_data_confidence,
)
from panel.services.usage_intelligence.schemas import (  # noqa: F401
    MODEL_VERSION,
    ConfidenceMetrics,
    CycleMetrics,
    ForecastMetrics,
    PackageChoice,
    Signals,
    TrendMetrics,
    WindowMetrics,
    classify_trend,
    maturity_for,
    trend_thresholds_for,
)
from panel.services.usage_intelligence.trend import (  # noqa: F401
    describe_state,
    detect_trend,
    trend_weight,
)

__all__ = [
    'MODEL_VERSION',
    'ConfidenceMetrics',
    'CycleMetrics',
    'ForecastMetrics',
    'Signals',
    'TrendMetrics',
    'WindowMetrics',
    'build_current_cycle',
    'build_historical_baseline',
    'build_rolling_window',
    'classify_trend',
    'has_recent_cycle_boundary',
    'latest_cycle_boundary',
    'maturity_for',
    'record_inferred_reset',
    'record_renewal_event',
    'record_verified_renewal',
    'daily_series_stats',
    'describe_state',
    'forecast_usage',
    'robust_cap',
    'robust_rate',
    'safety_margin_for',
    'detect_trend',
    'trend_thresholds_for',
    'trend_weight',
    'PackageChoice',
    'required_for',
    'select_packages',
    'assess_behavior_stability',
    'assess_confidence',
    'assess_data_confidence',
    'build_recommendation_v5',
    'recommendation_mode',
    'record_shadow_comparison',
    'reset_shadow_metrics',
    'shadow_metrics',
]
