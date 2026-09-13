"""Confidence: how much data there is, and how steady the behaviour behind it is.

RFP section 19. These are two different questions and the model keeps them apart:

* ``data_confidence`` - is there enough evidence? elapsed days, observed days, sample
  count, telemetry freshness, and whether the cycle window is long enough to measure;
* ``behavior_stability`` - is the behaviour behind that evidence steady? the cycle against
  the historical baseline, the day-to-day variance, and whether the quota ran out early.

A customer can therefore have plenty of data about behaviour that just changed:
``data=high, behavior_stability=low`` is a meaningful, supported answer - and it is exactly
the reported bug's shape.
"""
from panel.services.usage_intelligence.schemas import (
    EARLY_MATURITY,
    ConfidenceMetrics,
    maturity_for,
)

STABLE_MAD_RATIO = 0.40
UNSTABLE_MAD_RATIO = 0.75
_DOWNGRADE = {'high': 'medium', 'medium': 'early', 'early': 'early', 'unknown': 'early'}


def _mad_ratio(daily_series):
    """MAD / median: a scale-free measure of day-to-day variance."""
    series = sorted(float(value) for value in (daily_series or []) if value is not None)
    if len(series) < 3:
        return None
    median = series[len(series) // 2]
    if median <= 0:
        return None
    deviations = sorted(abs(value - median) for value in series)
    mad = deviations[len(deviations) // 2]
    return mad / median


def assess_data_confidence(cycle, rolling, *, freshness='fresh',
                           observed_dates=None, samples=None) -> tuple:
    """(label, reasons) for the 'is there enough evidence' question."""
    reasons = []
    if not getattr(cycle, 'available', False):
        reasons.append('no_verified_cycle')
    elapsed = float(getattr(cycle, 'effective_elapsed_days', 0.0) or 0.0)
    dates = observed_dates
    if dates is None:
        dates = int(getattr(rolling, 'observed_dates', 0) or 0)
    sample_count = samples
    if sample_count is None:
        sample_count = int(getattr(rolling, 'samples', 0) or 0)
    maturity = getattr(cycle, 'maturity', None) or maturity_for(
        getattr(cycle, 'elapsed_days', 0.0))

    if elapsed >= 14 and dates >= 10 and sample_count >= 10:
        label = 'high'
    elif elapsed >= 5 and dates >= 4:
        label = 'medium'
    else:
        label = 'early'
        if maturity in EARLY_MATURITY:
            reasons.append('cycle_too_young')
        if dates < 4:
            reasons.append('few_observed_days')

    if freshness == 'stale':
        downgraded = _DOWNGRADE.get(label, 'early')
        if downgraded != label:
            reasons.append('stale_telemetry')
        label = downgraded
    elif freshness == 'unknown':
        label = _DOWNGRADE.get(label, 'early')
        reasons.append('telemetry_timestamp_unknown')

    if not getattr(cycle, 'available', False) and label == 'high':
        label = 'medium'
    return label, tuple(reasons)


def assess_behavior_stability(cycle, rolling, baseline=None, trend=None, signals=None, *,
                              daily_series=None) -> tuple:
    """(label, reasons) for the 'is the behaviour steady' question."""
    reasons = []
    state = getattr(trend, 'state', 'unknown')
    mad_ratio = _mad_ratio(daily_series)
    exhausted = bool(getattr(signals, 'early_exhaustion', False))

    if state in ('strong_increase', 'strong_decrease'):
        return 'low', (('strong_increase' if state == 'strong_increase'
                        else 'strong_decrease'),) + (('early_exhaustion',) if exhausted else ())
    if exhausted:
        return 'low', ('early_exhaustion',)
    if mad_ratio is not None and mad_ratio > UNSTABLE_MAD_RATIO:
        return 'low', ('high_daily_variance',)

    if state == 'stable' and (mad_ratio is None or mad_ratio <= STABLE_MAD_RATIO):
        # A stable recent cycle that also looks like the month before it is the steadiest
        # evidence the model can have.
        baseline_rate = float(getattr(baseline, 'average_daily_gb', 0.0) or 0.0)
        cycle_rate = float(getattr(cycle, 'average_daily_gb', 0.0) or 0.0)
        if not getattr(baseline, 'available', False) or baseline_rate <= 0:
            reasons.append('no_baseline_window')
            return 'medium', tuple(reasons)
        drift = abs(cycle_rate - baseline_rate) / baseline_rate
        if drift > 0.40:
            return 'medium', ('baseline_drift',)
        return 'high', tuple(reasons)

    if state in ('increasing', 'decreasing', 'stable'):
        return 'medium', ((state,) if state != 'stable' else ('moderate_variance',))
    return 'medium', ('insufficient_trend_evidence',)


def assess_confidence(cycle, rolling, baseline=None, trend=None, signals=None, *,
                      freshness='fresh', observed_dates=None, samples=None,
                      daily_series=None) -> ConfidenceMetrics:
    """Both confidence axes, with the reasons that produced them."""
    data_label, data_reasons = assess_data_confidence(
        cycle, rolling, freshness=freshness, observed_dates=observed_dates, samples=samples)
    stability_label, stability_reasons = assess_behavior_stability(
        cycle, rolling, baseline=baseline, trend=trend, signals=signals,
        daily_series=daily_series)
    return ConfidenceMetrics(
        data=data_label,
        behavior_stability=stability_label,
        reasons=tuple(data_reasons) + tuple(stability_reasons),
    )
