"""Trend detection: how much recent behaviour differs from the month behind it.

RFP section 15. This is a pure function of the cycle and rolling metrics: it compares the
two rates and names the change, with wider thresholds while the cycle is still short
evidence. The result is what later steps use to decide how much weight recent consumption
deserves - the whole point of the model is that a 93% increase is not averaged away.

A missing or zero denominator is handled explicitly: without a history there is no ratio to
report, and the forecast falls back to the window that does exist instead of inventing a
percentage.
"""
from panel.services.usage_intelligence.schemas import (
    EARLY_MATURITY,
    TrendMetrics,
    classify_trend,
    trend_thresholds_for,
)


def detect_trend(cycle, rolling, *, maturity=None) -> TrendMetrics:
    """Compare the current cycle rate with the rolling window rate."""
    cycle_rate = float(getattr(cycle, 'average_daily_gb', 0.0) or 0.0)
    rolling_rate = float(getattr(rolling, 'average_daily_gb', 0.0) or 0.0)
    effective_maturity = maturity or getattr(cycle, 'maturity', 'mature') or 'mature'

    if not getattr(cycle, 'available', False) or cycle_rate <= 0:
        return TrendMetrics(ratio=None, change_percent=None, state='unknown')
    if not getattr(rolling, 'available', False) or rolling_rate <= 0:
        # No comparable history: the cycle stands on its own rate, with no ratio claim.
        return TrendMetrics(ratio=None, change_percent=None, state='unknown')

    ratio = cycle_rate / rolling_rate
    state = classify_trend(ratio, maturity=effective_maturity)
    return TrendMetrics(
        ratio=ratio,
        change_percent=(ratio - 1.0) * 100.0,
        state=state,
        confidence_aware=bool(effective_maturity in EARLY_MATURITY),
    )


def trend_weight(cycle, rolling, trend) -> float:
    """How much of the forecast should come from the cycle rather than the history.

    Weight is the cycle's share: 1.0 means "trust only this cycle", 0.0 means "trust only the
    month". It grows with maturity, with the observed change, and when the quota ran out
    early, and it never jumps to a verdict from a handful of minutes.
    """
    maturity = getattr(cycle, 'maturity', 'insufficient') or 'insufficient'
    base = {
        'mature': 0.65,
        'medium': 0.55,
        'early': 0.45,
        'very_early': 0.35,
        'insufficient': 0.25,
    }.get(maturity, 0.45)

    state = getattr(trend, 'state', 'unknown')
    if state == 'strong_increase':
        base = max(base, 0.80)
    elif state == 'increasing':
        base = max(base, 0.65)
    elif state == 'strong_decrease':
        # Recent behaviour dropped: it still matters, but it must not dominate a month of
        # evidence on its own.
        base = min(max(base, 0.40), 0.55)
    elif state == 'stable':
        base = max(base, 0.55)

    if getattr(cycle, 'available', False) and not getattr(rolling, 'available', False):
        # Nothing to blend with: use the cycle and say so through the basis.
        base = 1.0
    return max(0.0, min(1.0, float(base)))


def describe_state(state: str, language: str = 'fa') -> str:
    """Operator/customer facing sentence for the trend state (RFP sections 26-27)."""
    labels = {
        'strong_increase': {
            'fa': 'مصرف شما بعد از آخرین تمدید افزایش قابل‌توجهی داشته است',
            'en': 'Your usage has increased significantly since the last renewal',
        },
        'increasing': {
            'fa': 'مصرف شما بعد از آخرین تمدید افزایش داشته است',
            'en': 'Your usage has increased since the last renewal',
        },
        'stable': {
            'fa': 'الگوی مصرف اخیر شما با میانگین ماه گذشته تقریباً ثابت است',
            'en': 'Your recent usage is roughly in line with the past month',
        },
        'decreasing': {
            'fa': 'مصرف شما بعد از آخرین تمدید کاهش داشته است',
            'en': 'Your usage has decreased since the last renewal',
        },
        'strong_decrease': {
            'fa': 'مصرف شما بعد از آخرین تمدید کاهش قابل‌توجهی داشته است',
            'en': 'Your usage has dropped significantly since the last renewal',
        },
        'unknown': {
            'fa': 'هنوز داده کافی برای مقایسه رفتار مصرف وجود ندارد',
            'en': 'There is not enough evidence yet to compare usage behaviour',
        },
    }
    entry = labels.get(state, labels['unknown'])
    return entry['fa'] if language == 'fa' else entry['en']


def thresholds_used(maturity: str):
    """The thresholds a classification was made with (observability/tests)."""
    return trend_thresholds_for(maturity)
