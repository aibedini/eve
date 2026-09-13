"""Customer-facing explanations for a recommendation (RFP sections 26-27).

The model must be able to say *why* it recommends a package, in the customer's language, and
the reason has to come from the evidence rather than from a translator's guess. The sentences
live here - in Python, unit tested - so the template only picks a language and prints.

No identifier ever reaches this text: the sentences talk about usage and cycles, never about
an account, an email or a token (RFP sections 41-42).
"""
from panel.services.usage_intelligence.schemas import EARLY_MATURITY

INSUFFICIENT_STATES = ('unknown', 'insufficient_evidence')


def _num(value, places=2):
    fmt = '%.' + str(int(places)) + 'f'
    try:
        return fmt % float(value)
    except (TypeError, ValueError):
        # Keep the sentence's shape even when a metric is missing.
        return fmt % 0.0


def _sentence_for(state, *, cycle_gb, rolling_gb, days=None):
    """(fa, en) for one trend state, with the measured rates filled in."""
    cycle = _num(cycle_gb)
    rolling = _num(rolling_gb)
    if state == 'strong_increase':
        return (
            'مصرف شما بعد از آخرین تمدید افزایش قابل‌توجهی داشته است. '
            'در چرخه فعلی به‌طور میانگین روزانه %s گیگ مصرف کرده‌اید، در حالی که میانگین '
            '۳۱ روز اخیر شما %s گیگ در روز بوده است. به همین دلیل الگوی مصرف اخیر وزن '
            'بیشتری در پیشنهاد بسته داشته است.' % (cycle, rolling),
            'Your usage has increased significantly since the last renewal. In the current '
            'cycle you have used %s GB per day on average, while your last 31 days average '
            '%s GB per day. That is why recent usage was given more weight in this '
            'recommendation.' % (cycle, rolling),
        )
    if state == 'increasing':
        return (
            'مصرف شما بعد از آخرین تمدید افزایش داشته است: در چرخه فعلی روزانه %s گیگ در '
            'برابر میانگین %s گیگ در ۳۱ روز اخیر. الگوی مصرف اخیر در پیشنهاد وزن بیشتری '
            'داشته است.' % (cycle, rolling),
            'Your usage has increased since the last renewal: %s GB per day in the current '
            'cycle against %s GB per day over the last 31 days. Recent usage carried more '
            'weight in this recommendation.' % (cycle, rolling),
        )
    if state == 'stable':
        return (
            'الگوی مصرف اخیر شما با میانگین ماه گذشته تقریباً ثابت است. '
            'پیشنهاد بسته بر اساس همین روند محاسبه شده است.',
            'Your recent usage is roughly in line with the past month. The recommended '
            'package is based on that pattern.',
        )
    if state == 'decreasing':
        return (
            'مصرف شما بعد از آخرین تمدید کاهش داشته است: روزانه %s گیگ در برابر میانگین %s '
            'گیگ در ۳۱ روز اخیر. سابقه مصرف ماه گذشته هم در محاسبه لحاظ شده است.'
            % (cycle, rolling),
            'Your usage has decreased since the last renewal: %s GB per day against %s GB '
            'per day over the last 31 days. The past month is still part of the estimate.'
            % (cycle, rolling),
        )
    if state == 'strong_decrease':
        return (
            'مصرف شما بعد از آخرین تمدید کاهش قابل‌توجهی داشته است و الگوی اخیر در پیشنهاد '
            'لحاظ شده، ولی سابقه مصرف ماه گذشته هم وزن خودش را دارد.',
            'Your usage has dropped significantly since the last renewal. Recent behaviour '
            'is reflected in the recommendation, while the past month still carries its own '
            'weight.',
        )
    return (
        'از شروع چرخه فعلی هنوز داده کافی جمع نشده است؛ '
        'پیشنهاد فعلی بیشتر بر اساس سابقه مصرف شماست و ممکن است تغییر کند.',
        'There is not enough evidence since the current cycle started; this recommendation '
        'leans on your usage history and may change.',
    )


def _exhaustion_sentence(days):
    if days:
        return (
            'حجم بسته پیش از پایان مدت مورد انتظار (%s روز) تمام شده است و این موضوع در '
            'پیش‌بینی لحاظ شده است.' % _num(days, 0),
            "The plan's volume ran out before its expected duration (%s days), and the "
            'forecast accounts for that.' % _num(days, 0),
        )
    return (
        'حجم بسته پیش از پایان مدت مورد انتظار تمام شده است و این موضوع در پیش‌بینی لحاظ '
        'شده است.',
        "The plan's volume ran out before its expected duration, and the forecast accounts "
        'for that.',
    )


def explanation_state(*, trend_state, data_confidence='medium', cycle_available=True,
                      maturity=None):
    """Which sentence this recommendation should lead with."""
    if not cycle_available:
        return 'insufficient_evidence'
    if maturity in EARLY_MATURITY or data_confidence == 'early':
        return 'insufficient_evidence'
    if trend_state in INSUFFICIENT_STATES or not trend_state:
        return 'insufficient_evidence'
    return trend_state


def build_explanation(*, trend_state='unknown', cycle_daily_gb=0.0, rolling_daily_gb=0.0,
                      change_percent=None, forecast_gb=None, data_confidence='medium',
                      cycle_available=True, maturity=None, early_exhaustion=False,
                      expected_duration_days=None) -> dict:
    """{'state', 'fa', 'en'}: the reason, ready to print in either language.

    ``change_percent`` and ``forecast_gb`` are accepted so callers can pass the whole payload
    context, but the sentences themselves quote only the measured rates and the exhaustion
    fact - the UI shows the percentage and the forecast as their own rows.
    """
    _ = change_percent, forecast_gb
    state = explanation_state(trend_state=trend_state, data_confidence=data_confidence,
                              cycle_available=cycle_available, maturity=maturity)
    fa, en = _sentence_for(state, cycle_gb=cycle_daily_gb, rolling_gb=rolling_daily_gb)
    if early_exhaustion and state != 'insufficient_evidence':
        ex_fa, ex_en = _exhaustion_sentence(expected_duration_days)
        fa = fa + ' ' + ex_fa
        en = en + ' ' + ex_en
    return {'state': state, 'fa': fa, 'en': en}
