"""Forecast: blend the current cycle with the history, robustly (RFP sections 17-18, 21).

Pure functions. The blend weights are the RFP's: a mature and stable cycle takes 65% of the
weight, a strong increase or an early exhaustion 80%, a young cycle only 45% - and with no
authoritative cycle at all the history answers alone, with a low-confidence basis.

Robust bounds keep one freak day from rewriting a customer's package: each daily
observation is capped at ``max(3 x median, P90)`` before the rate is taken, so an unusual
25GB day in a 0.5GB/day account cannot triple the recommendation while a genuine sustained
increase (which raises the median with it) is preserved.
"""
from panel.services.usage_intelligence.schemas import (
    EARLY_MATURITY,
    FORECAST_BLEND,
    ROLLING_WINDOW_DAYS,
    SAFETY_MARGINS,
    ForecastMetrics,
)

MIN_ROBUST_DAYS = 2


def _percentile(ordered, fraction):
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * float(fraction)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return float(ordered[lower]) * (1.0 - weight) + float(ordered[upper]) * weight


def daily_series_stats(values) -> dict:
    """Median / P75 / P90 / MAD of a daily-usage series (GB)."""
    series = sorted(float(value) for value in (values or []) if value is not None)
    if not series:
        return {'count': 0, 'median': 0.0, 'p75': 0.0, 'p90': 0.0, 'mad': 0.0,
                'cap': None}
    median = _percentile(series, 0.5)
    deviations = sorted(abs(value - median) for value in series)
    return {
        'count': len(series),
        'median': median,
        'p75': _percentile(series, 0.75),
        'p90': _percentile(series, 0.90),
        'mad': _percentile(deviations, 0.5),
        'cap': robust_cap(series),
    }


def robust_cap(values) -> float | None:
    """The winsorization cap: ``max(3 x median, P90)`` (RFP section 18)."""
    series = sorted(float(value) for value in (values or []) if value is not None)
    if len(series) < MIN_ROBUST_DAYS:
        return None
    median = _percentile(series, 0.5)
    p90 = _percentile(series, 0.90)
    return max(3.0 * median, p90)


def robust_rate(values, *, basis_days, cap=None) -> float | None:
    """A daily rate whose outliers are trimmed, or None when there are too few days."""
    series = [float(value) for value in (values or []) if value is not None]
    if len(series) < MIN_ROBUST_DAYS or not basis_days or basis_days <= 0:
        return None
    limit = cap if cap is not None else robust_cap(series)
    if limit is None or limit <= 0:
        return None
    trimmed = sum(min(max(0.0, value), limit) for value in series)
    return trimmed / float(basis_days)


def safety_margin_for(state: str, *, data_confidence: str = 'medium',
                      maturity: str = 'medium') -> float:
    """How much headroom the primary recommendation gets (RFP section 21)."""
    if state == 'strong_increase':
        return SAFETY_MARGINS['strong_increase']
    if maturity in EARLY_MATURITY or data_confidence == 'early':
        return SAFETY_MARGINS['early_unstable']
    if state == 'stable' and data_confidence == 'high':
        return SAFETY_MARGINS['stable_high']
    return SAFETY_MARGINS['medium']


def _blend_for(cycle, rolling, signals, trend, maturity):
    """(cycle_weight, rolling_weight, forecast_basis) per RFP section 17."""
    cycle_rate = float(getattr(cycle, 'average_daily_gb', 0.0) or 0.0)
    rolling_rate = float(getattr(rolling, 'average_daily_gb', 0.0) or 0.0)
    cycle_ok = bool(getattr(cycle, 'available', False)) and cycle_rate > 0
    rolling_ok = bool(getattr(rolling, 'available', False)) and rolling_rate > 0

    if not cycle_ok and not rolling_ok:
        return 0.0, 0.0, 'live_fallback'
    if not cycle_ok:
        return 0.0, 1.0, 'rolling_history'
    if not rolling_ok:
        return 1.0, 0.0, 'current_cycle_dominant'

    state = getattr(trend, 'state', 'unknown')
    if state == 'strong_increase' or bool(getattr(signals, 'early_exhaustion', False)):
        weights = FORECAST_BLEND['recent_increase']
        return weights[0], weights[1], 'current_cycle_dominant'
    if maturity == 'mature' and state == 'stable':
        weights = FORECAST_BLEND['mature_stable']
        return weights[0], weights[1], 'blended'
    if maturity in EARLY_MATURITY:
        weights = FORECAST_BLEND['early_cycle']
        return weights[0], weights[1], 'blended'
    weights = FORECAST_BLEND['default']
    return weights[0], weights[1], 'blended'


def forecast_usage(cycle, rolling, baseline=None, signals=None, *, trend=None,
                   horizon_days=ROLLING_WINDOW_DAYS, safety_margin=None,
                   data_confidence='medium', cycle_daily_gb=None,
                   rolling_daily_gb=None) -> ForecastMetrics:
    """The projected 31-day need (or a package-length horizon) and its safety buffer.

    ``cycle_daily_gb`` / ``rolling_daily_gb`` are the observed per-day series; when they
    carry enough days the rates are winsorized, otherwise the window rates are used as they
    came from the loader.
    """
    maturity = getattr(cycle, 'maturity', 'insufficient') or 'insufficient'
    horizon = max(1, int(horizon_days or ROLLING_WINDOW_DAYS))

    cycle_rate = robust_rate(cycle_daily_gb,
                             basis_days=getattr(cycle, 'effective_elapsed_days', 0.0))
    if cycle_rate is None:
        cycle_rate = float(getattr(cycle, 'average_daily_gb', 0.0) or 0.0)
    rolling_rate = robust_rate(rolling_daily_gb,
                               basis_days=getattr(rolling, 'basis_days', 0.0))
    if rolling_rate is None:
        rolling_rate = float(getattr(rolling, 'average_daily_gb', 0.0) or 0.0)

    cycle_weight, rolling_weight, basis = _blend_for(cycle, rolling, signals, trend,
                                                    maturity)
    rate = max(0.0, cycle_rate) * cycle_weight + max(0.0, rolling_rate) * rolling_weight
    if basis == 'live_fallback' and rate <= 0:
        rate = 0.0

    state = getattr(trend, 'state', 'unknown')
    margin = (float(safety_margin) if safety_margin is not None
              else safety_margin_for(state, data_confidence=data_confidence,
                                     maturity=maturity))
    projected = rate * horizon
    cap_used = robust_cap(cycle_daily_gb) if cycle_daily_gb else None

    return ForecastMetrics(
        average_daily_gb=float(rate),
        projected_31d_gb=float(projected),
        horizon_days=horizon,
        safety_margin_percent=int(round(margin * 100)),
        buffered_requirement_gb=float(projected * (1.0 + margin)),
        basis=basis,
        blend=(float(cycle_weight), float(rolling_weight)),
        capped_daily_gb=(float(cap_used) if cap_used else None),
    )
