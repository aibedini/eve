"""The v5 recommendation: one payload built from the evidence (RFP sections 24-25, 28-29).

This module owns the assembly, not the arithmetic: it loads one :class:`UsageContext`, asks
the pure layers for the trend, forecast, confidence and package choice, and returns the
contract the API and UI consume - including the deprecated fields the transition still needs
(``source``, ``fast_cycle``, a single ``confidence`` label) whose values are derived from
v5 so nothing can disagree with itself.

Rollout is explicit (RFP sections 54-58): ``EVE_USAGE_RECOMMENDATION_V5`` (or the
``usage_recommendation_v5`` system setting) selects ``off`` / ``shadow`` / ``on``, so v5 can
be computed alongside v4 and compared before anybody sees it.
"""
import os

from panel.services.usage_intelligence import analysis
from panel.services.usage_intelligence.confidence import assess_confidence
from panel.services.usage_intelligence.copy import build_explanation
from panel.services.usage_intelligence.forecast import forecast_usage, safety_margin_for
from panel.services.usage_intelligence.packages import select_packages
from panel.services.usage_intelligence.schemas import (
    MODEL_VERSION,
    ROLLING_WINDOW_DAYS,
)
from panel.services.usage_intelligence.trend import detect_trend

FLAG_ENV = 'EVE_USAGE_RECOMMENDATION_V5'
FLAG_SETTING_KEY = 'usage_recommendation_v5'
MODES = ('off', 'shadow', 'on')


def _coerce_mode(value):
    raw = str(value or '').strip().lower()
    if raw in ('1', 'true', 'yes', 'on', 'enabled'):
        return 'on'
    if raw in ('shadow', 'compare', 'dry_run'):
        return 'shadow'
    if raw in ('0', 'false', 'no', 'off', 'disabled', ''):
        return 'off'
    return 'off'


def recommendation_mode() -> str:
    """The rollout stage: off / shadow / on (environment first, then the system setting)."""
    mode = _coerce_mode(os.environ.get(FLAG_ENV))
    if mode != 'off':
        return mode
    try:
        from panel.models import SystemSetting
        row = SystemSetting.query.filter_by(key=FLAG_SETTING_KEY).first()
        if row is not None:
            return _coerce_mode(row.value)
    except Exception:
        pass
    return 'off'


def _legacy_source(basis: str) -> str:
    """The pre-v5 ``source`` vocabulary, derived from the new basis (RFP section 28)."""
    if basis in ('current_cycle_dominant', 'blended'):
        return 'current_cycle' if basis == 'current_cycle_dominant' else 'last_31_days'
    if basis == 'rolling_history':
        return 'last_31_days'
    return 'live_counter'


def build_recommendation_v5(server_id, sub_id, packages, *, live_usage=None, now=None,
                            terminal=False, rolling_days=ROLLING_WINDOW_DAYS,
                            context=None) -> dict | None:
    """The usage-fit-v5 payload, or None when there is nothing to recommend."""
    if not packages:
        return None
    if context is None:
        context = analysis.load_usage_context(
            server_id, sub_id, now=now, live_usage=live_usage, rolling_days=rolling_days)
    cycle, rolling, baseline = context.cycle, context.rolling, context.baseline
    if not cycle.available and not rolling.available:
        return None

    trend = detect_trend(cycle, rolling)
    confidence = assess_confidence(
        cycle, rolling, baseline=baseline, trend=trend, signals=context.signals,
        freshness=context.signals.telemetry_freshness,
        observed_dates=(rolling.observed_dates if rolling.available else None),
        samples=(rolling.samples if rolling.available else None),
        daily_series=context.cycle_daily_gb or context.rolling_daily_gb,
    )
    margin = safety_margin_for(trend.state, data_confidence=confidence.data,
                               maturity=cycle.maturity)
    forecast = forecast_usage(
        cycle, rolling, baseline, context.signals, trend=trend, safety_margin=margin,
        data_confidence=confidence.data, horizon_days=rolling_days,
        cycle_daily_gb=context.cycle_daily_gb, rolling_daily_gb=context.rolling_daily_gb)
    choices = select_packages(packages, forecast)
    recommended = choices.get('recommended')
    if recommended is None or recommended.package_id is None:
        return None
    comfort = choices.get('comfort')

    payload = {
        'model_version': MODEL_VERSION,
        'current_cycle': cycle.to_dict(),
        'rolling_31d': rolling.to_dict(),
        'historical_baseline': baseline.to_dict(),
        'trend': trend.to_dict(),
        'forecast': forecast.to_dict(),
        'signals': context.signals.to_dict(),
        'confidence': {'data': confidence.data,
                       'behavior_stability': confidence.behavior_stability},
        'recommendation': {
            'package_id': recommended.package_id,
            'package_volume_gb': recommended.package_volume_gb,
            'capacity_limited': recommended.capacity_limited,
            'required_gb': recommended.required_gb,
            'reason': recommended.reason,
        },
        # ── transition fields (RFP section 25/28/29): values derived from v5 ──
        'forecast_basis': forecast.basis,
        'package_id': recommended.package_id,
        'package_name': recommended.package_name,
        'package_volume': recommended.package_volume_gb,
        'package_days': recommended.package_days or rolling_days,
        'package_price': recommended.package_price,
        'comfort_package_id': comfort.package_id if comfort else None,
        'comfort_package_name': comfort.package_name if comfort else '',
        'comfort_package_volume': comfort.package_volume_gb if comfort else 0,
        'comfort_package_days': comfort.package_days if comfort else 0,
        'comfort_package_price': comfort.package_price if comfort else 0,
        'average_daily_gb': round(forecast.average_daily_gb, 2),
        'projected_31d_gb': round(forecast.projected_31d_gb, 1),
        'buffered_requirement_gb': round(forecast.buffered_requirement_gb, 1),
        'basis_days': round(rolling.basis_days, 1) if rolling.available else 0.0,
        'covered_days': int(rolling.observed_dates or 0),
        'confidence_label': confidence.data,
        'safety_margin_percent': int(forecast.safety_margin_percent),
        'capacity_limited': bool(recommended.capacity_limited),
        'capacity_shortfall_gb': float(recommended.capacity_shortfall_gb),
        # Deprecated aliases: read them for one release, stop writing them after that.
        'source': _legacy_source(forecast.basis),
        'fast_cycle': bool(context.signals.early_exhaustion),
        'evidence': {
            'queries': context.queries,
            'usage_source': cycle.usage_source if cycle.available else 'none',
            'reasons': list(confidence.reasons),
            'blend': {'cycle': round(forecast.blend[0], 2),
                      'rolling': round(forecast.blend[1], 2)},
        },
    }
    # Why this package, in the customer's language(s) - computed here so the template only
    # picks a language and prints (RFP sections 26-27).
    payload['explanation'] = build_explanation(
        trend_state=trend.state,
        cycle_daily_gb=(cycle.average_daily_gb if cycle.available else 0.0),
        rolling_daily_gb=(rolling.average_daily_gb if rolling.available else 0.0),
        change_percent=trend.change_percent,
        forecast_gb=forecast.projected_31d_gb,
        data_confidence=confidence.data,
        cycle_available=cycle.available,
        maturity=cycle.maturity,
        early_exhaustion=context.signals.early_exhaustion,
        expected_duration_days=context.signals.expected_duration_days,
    )
    _ = terminal  # kept for call-site compatibility; exhaustion now comes from evidence
    return payload
