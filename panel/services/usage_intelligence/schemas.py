"""Contracts and constants for the v5 usage-intelligence model.

Nothing here touches the database: these are the shapes the analytics layers agree on,
so ``cycles``/``forecast``/``packages`` can be unit tested as pure functions (RFP
sections 63-65) and the API contract (section 24) is defined in one place.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime

MODEL_VERSION = 'usage-fit-v5'
ROLLING_WINDOW_DAYS = 31

# A forecast from a cycle that started minutes ago is noise (RFP section 12): the elapsed
# time used for rates never goes below a quarter of a day, and the maturity buckets below
# decide how much the cycle may outvote the rolling history.
MIN_EFFECTIVE_ELAPSED_DAYS = 0.25
MATURITY_BUCKETS = (
    ('insufficient', 0.25, 'less than six hours of cycle evidence'),
    ('very_early', 1.0, 'six to twenty-four hours'),
    ('early', 5.0, 'one to four days'),
    ('medium', 14.0, 'five to thirteen days'),
    ('mature', None, 'fourteen days or more'),
)

# Trend classification (RFP section 15).
TREND_THRESHOLDS = (
    ('strong_decrease', 0.70),
    ('decreasing', 0.90),
    ('stable', 1.15),
    ('increasing', 1.40),
    ('strong_increase', None),
)

# A short cycle is noisy evidence, so its thresholds are wider before it may claim a
# behaviour change (RFP section 15: "confidence-aware" thresholds for early cycles).
TREND_THRESHOLDS_EARLY = (
    ('strong_decrease', 0.60),
    ('decreasing', 0.80),
    ('stable', 1.25),
    ('increasing', 1.60),
    ('strong_increase', None),
)
# A short cycle is noisy evidence, so its thresholds are wider before it may claim a
# behaviour change (RFP section 15: "confidence-aware" thresholds for early cycles). The
# threshold widens for anything under five days; from five days the standard table applies.
EARLY_MATURITY = ('insufficient', 'very_early', 'early')

# Exhaustion severity (RFP section 29) by cycle_days / expected_duration.
EXHAUSTION_CRITICAL_RATIO = 0.35
EXHAUSTION_HIGH_RATIO = 0.60

# Forecast blend weights (RFP section 17): (cycle_rate, rolling_rate).
FORECAST_BLEND = {
    'mature_stable': (0.65, 0.35),
    'recent_increase': (0.80, 0.20),
    'early_cycle': (0.45, 0.55),
    'default': (0.55, 0.45),
}

# Safety margin per behaviour/confidence state (RFP section 21).
SAFETY_MARGINS = {
    'stable_high': 0.10,
    'medium': 0.15,
    'strong_increase': 0.20,
    'early_unstable': 0.25,
}

# Telemetry freshness (RFP section 20).
FRESHNESS_FRESH_SECONDS = 5 * 60
FRESHNESS_ACCEPTABLE_SECONDS = 30 * 60

FORECAST_BASIS_VALUES = (
    'current_cycle_dominant',
    'blended',
    'rolling_history',
    'live_fallback',
)


def maturity_for(elapsed_days: float) -> str:
    """Which maturity bucket a cycle's elapsed time falls in (RFP section 12)."""
    try:
        days = float(elapsed_days)
    except (TypeError, ValueError):
        return 'insufficient'
    for name, limit, _description in MATURITY_BUCKETS:
        if limit is None or days < limit:
            return name
    return 'mature'


def trend_thresholds_for(maturity: str):
    """The threshold table a cycle of this maturity is judged against."""
    return TREND_THRESHOLDS_EARLY if maturity in EARLY_MATURITY else TREND_THRESHOLDS


def classify_trend(ratio, *, maturity: str = 'mature') -> str:
    """Ratio of the current cycle rate to the rolling rate (RFP section 15)."""
    if ratio is None:
        return 'unknown'
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return 'unknown'
    for name, limit in trend_thresholds_for(maturity):
        if limit is None or value < limit:
            return name
    return 'strong_increase'


def _iso(value):
    return value.isoformat() + 'Z' if isinstance(value, datetime) else None


@dataclass(frozen=True)
class CycleMetrics:
    """The current renewal cycle: the window that recent behaviour lives in."""
    available: bool = False
    reason: str | None = None
    started_at: datetime | None = None
    event_type: str | None = None
    traffic_reset: bool = False
    elapsed_days: float = 0.0
    effective_elapsed_days: float = 0.0
    usage_bytes: int = 0
    usage_source: str = 'none'
    daily_sum_bytes: int = 0
    average_daily_gb: float = 0.0
    projected_31d_gb: float = 0.0
    maturity: str = 'insufficient'
    granted_volume_bytes: int | None = None
    new_volume_limit_bytes: int | None = None
    previous_volume_limit_bytes: int | None = None

    def to_dict(self) -> dict:
        return {
            'available': bool(self.available),
            'reason': self.reason,
            'started_at': _iso(self.started_at),
            'elapsed_days': round(self.elapsed_days, 2),
            'effective_elapsed_days': round(self.effective_elapsed_days, 2),
            'maturity': self.maturity,
            'event_type': self.event_type,
            'traffic_reset': bool(self.traffic_reset),
            'usage_gb': round(self.usage_bytes / float(1024 ** 3), 2),
            'average_daily_gb': round(self.average_daily_gb, 2),
            'projected_31d_gb': round(self.projected_31d_gb, 1),
            'usage_source': self.usage_source,
        }


@dataclass(frozen=True)
class WindowMetrics:
    """A bounded usage window (rolling 31 days, or the pre-cycle baseline)."""
    available: bool = False
    label: str = ''
    window_days: int = ROLLING_WINDOW_DAYS
    usage_bytes: int = 0
    basis_days: float = 1.0
    observed_dates: int = 0
    samples: int = 0
    average_daily_gb: float = 0.0
    projected_31d_gb: float = 0.0

    def to_dict(self) -> dict:
        return {
            'available': bool(self.available),
            'basis_days': round(self.basis_days, 1),
            'usage_gb': round(self.usage_bytes / float(1024 ** 3), 2),
            'average_daily_gb': round(self.average_daily_gb, 2),
            'projected_31d_gb': round(self.projected_31d_gb, 1),
        }


@dataclass(frozen=True)
class Signals:
    """Signals that change how much the recent window may be trusted."""
    early_exhaustion: bool = False
    exhaustion_severity: str = 'normal'
    exhaustion_ratio: float | None = None
    telemetry_stale: bool = False
    telemetry_freshness: str = 'fresh'
    telemetry_age_seconds: float | None = None
    expected_duration_days: float | None = None

    def to_dict(self) -> dict:
        return {
            'early_exhaustion': bool(self.early_exhaustion),
            'exhaustion_severity': self.exhaustion_severity,
            'exhaustion_ratio': (None if self.exhaustion_ratio is None
                                 else round(self.exhaustion_ratio, 3)),
            'telemetry_stale': bool(self.telemetry_stale),
            'telemetry_freshness': self.telemetry_freshness,
            'expected_duration_days': self.expected_duration_days,
        }


@dataclass(frozen=True)
class TrendMetrics:
    ratio: float | None = None
    change_percent: float | None = None
    state: str = 'unknown'
    confidence_aware: bool = True

    def to_dict(self) -> dict:
        return {
            'ratio': None if self.ratio is None else round(self.ratio, 2),
            'change_percent': (None if self.change_percent is None
                               else int(round(self.change_percent))),
            'state': self.state,
        }


@dataclass(frozen=True)
class ForecastMetrics:
    average_daily_gb: float = 0.0
    projected_31d_gb: float = 0.0
    horizon_days: int = ROLLING_WINDOW_DAYS
    safety_margin_percent: int = 0
    buffered_requirement_gb: float = 0.0
    basis: str = 'rolling_history'
    blend: tuple = field(default=(0.0, 0.0))
    capped_daily_gb: float | None = None

    def to_dict(self) -> dict:
        return {
            'average_daily_gb': round(self.average_daily_gb, 2),
            'projected_31d_gb': round(self.projected_31d_gb, 1),
            'horizon_days': int(self.horizon_days),
            'safety_margin_percent': int(self.safety_margin_percent),
            'buffered_requirement_gb': round(self.buffered_requirement_gb, 1),
            'basis': self.basis,
        }


@dataclass(frozen=True)
class ConfidenceMetrics:
    data: str = 'early'
    behavior_stability: str = 'unknown'
    reasons: tuple = field(default=())

    def to_dict(self) -> dict:
        return {
            'data': self.data,
            'behavior_stability': self.behavior_stability,
            'reasons': list(self.reasons),
        }


@dataclass(frozen=True)
class PackageChoice:
    package_id: int | None = None
    package_name: str = ''
    package_volume_gb: int = 0
    package_days: int = 0
    package_price: int = 0
    capacity_limited: bool = False
    capacity_shortfall_gb: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)
