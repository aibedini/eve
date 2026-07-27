"""Billing pricing and subscription-package recommendation cluster (extracted from app.py)."""
from datetime import datetime, timedelta

from flask import session
from sqlalchemy import func

from panel.core.redis_client import GLOBAL_SERVER_DATA
from panel.extensions import db
from panel.models import (
    Admin,
    ClientOwnership,
    Package,
    RenewalEvent,
    SystemConfig,
    Transaction,
    UsageCounterState,
    UsageDaily,
)


def calculate_reseller_price(user, base_price=None, package=None, cost_type=None):
    """
    Calculate price for a reseller based on their settings.
    """
    if user.role != 'reseller':
        if package: return package.price
        return base_price if base_price is not None else 0

    # 1. Custom Plan Logic (Day/GB rates)
    if cost_type == 'day':
        if user.custom_cost_per_day is not None:
            return user.custom_cost_per_day
        discount = user.discount_percent or 0
        return int(base_price * (1 - discount / 100)) if base_price else 0
        
    if cost_type == 'gb':
        if user.custom_cost_per_gb is not None:
            return user.custom_cost_per_gb
        discount = user.discount_percent or 0
        return int(base_price * (1 - discount / 100)) if base_price else 0

    # 2. Package Logic
    if package:
        # Priority 1: Reseller Price on Package (Global Reseller Price)
        # If a specific reseller price is set on the package, use it.
        # However, if the user has a specific discount, maybe they want discount off the standard price?
        # Let's assume: Reseller Price is a fixed override.
        if package.reseller_price is not None and package.reseller_price > 0:
             # If user has a discount, we might want to apply it to the standard price and compare?
             # Or just take the reseller price.
             # Let's stick to: Reseller Price > Discounted Standard Price.
             return package.reseller_price
            
        # Priority 2: Discount on Standard Price
        discount = user.discount_percent or 0
        return int(package.price * (1 - discount / 100))

    return base_price if base_price is not None else 0

def _build_sub_page_packages(owner) -> list[dict]:
    """Packages to surface on a customer's subscription page, based on the
    account OWNER. Only packages flagged show_on_sub are eligible.

    - Reseller-owned account: global + packages assigned to that reseller +
      the reseller's own packages, each priced with the reseller's pricing.
    - No reseller (system/admin-managed): only global packages, standard price.
    """
    import json as _j
    try:
        pkgs = Package.query.filter_by(enabled=True).order_by(Package.display_order, Package.id).all()
    except Exception:
        return []

    is_reseller = bool(owner and getattr(owner, 'role', None) == 'reseller')

    shown_ids = set()
    if is_reseller:
        try:
            shown_ids = set(int(x) for x in _j.loads(owner.sub_shown_package_ids or '[]'))
        except Exception:
            shown_ids = set()

    out = []
    for p in pkgs:
        if not getattr(p, 'show_on_renew', True):
            continue
        scope = p.scope or 'global'
        if is_reseller:
            if p.created_by == owner.id:
                # Reseller's own package — controlled by its own show_on_sub flag.
                if not getattr(p, 'show_on_sub', False):
                    continue
                price = p.price
            else:
                # Global or assigned-to-reseller package — the reseller decides
                # per-package whether to surface it (default hidden).
                if scope == 'global':
                    visible = True
                elif scope == 'assigned':
                    try:
                        ids = _j.loads(p.assigned_reseller_ids or '[]')
                    except Exception:
                        ids = []
                    visible = owner.id in ids
                else:
                    visible = False
                if not visible or p.id not in shown_ids:
                    continue
                price = calculate_reseller_price(owner, package=p)
        else:
            # System/admin-managed account: only global packages the admin ticked.
            if scope != 'global' or not getattr(p, 'show_on_sub', False):
                continue
            price = p.price
        out.append({
            'id': p.id,
            'name': p.name,
            'days': int(p.days or 0),
            'volume': int(p.volume or 0),
            'price': int(price or 0),
        })
    return out


def _select_subscription_package(packages: list[dict], daily_gb: float,
                                 safety_margin: float) -> tuple[dict | None, dict | None, float]:
    """Pick a best-fit package and an optional peace-of-mind alternative.

    Candidate generation keeps monthly/unlimited-duration offers. Scoring favors
    low excess volume, a duration close to 31 days, and then price. Unlimited
    volume is a fallback only when no finite package can cover the forecast.
    """
    candidates = []
    for package in packages or []:
        try:
            days = int(package.get('days') or 0)
            volume = int(package.get('volume') or 0)
            price = max(0, int(package.get('price') or 0))
        except (TypeError, ValueError):
            continue
        if days not in (0,) and days < 28:
            continue
        horizon_days = days if days > 0 else 31
        required_gb = max(0.0, float(daily_gb)) * horizon_days
        buffered_gb = required_gb * (1.0 + safety_margin)
        candidates.append((package, days, volume, price, required_gb, buffered_gb))

    if not candidates:
        return None, None, 0.0

    # Eligibility follows the user's point forecast. The uncertainty buffer is
    # diagnostic only; using it as a hard threshold silently upsells a 9.4 GB
    # forecast from a 10 GB package to 20 GB.
    # A tiny tolerance prevents elapsed-time floating point noise from turning
    # an exact 10.00 GB forecast into 10.0000001 and incorrectly upselling 20 GB.
    fit_tolerance_gb = 0.01
    finite_fits = [
        item for item in candidates
        if item[2] > 0 and item[2] + fit_tolerance_gb >= item[4]
    ]
    pool = finite_fits or [item for item in candidates if item[2] == 0]

    def _choose(candidate_pool, requirement_index):
        prices = [item[3] for item in candidate_pool]
        lo_price, hi_price = min(prices), max(prices)

        def _score(item):
            _package, days, volume, price, required, buffered = item
            target = required if requirement_index == 4 else buffered
            if volume == 0:
                excess = 1.25  # unlimited is useful, but only after finite fits fail
            else:
                excess = max(0.0, (volume - target) / max(target, 1.0))
            duration_penalty = abs((days if days > 0 else 31) - 31) / 31.0
            price_penalty = ((price - lo_price) / (hi_price - lo_price)) if hi_price > lo_price else 0.0
            return excess + (0.20 * duration_penalty) + (0.04 * price_penalty), price

        return min(candidate_pool, key=_score)

    if pool:
        selected = _choose(pool, 4)
    else:
        # A forecast above the largest finite package must still produce useful
        # guidance. Pick the highest-capacity offer (then the closest duration
        # and lowest price) and let the caller label it as capacity-limited.
        # Previously this branch returned None and silently removed the entire
        # recommendation for the users who consume the most.
        selected = min(
            (item for item in candidates if item[2] > 0),
            key=lambda item: (-item[2], abs((item[1] or 31) - 31), item[3]),
        )

    buffered_fits = [
        item for item in candidates
        if item[2] > 0 and item[2] + fit_tolerance_gb >= item[5]
    ]
    comfort_pool = buffered_fits or [item for item in candidates if item[2] == 0]
    comfort = _choose(comfort_pool, 5) if comfort_pool else None
    if comfort and comfort[0].get('id') == selected[0].get('id'):
        comfort = None

    return selected[0], (comfort[0] if comfort else None), selected[5]


def _live_subscription_usage(server_id: int, sub_id: str) -> dict:
    """Return the newest in-memory panel counter for one subscription.

    This keeps every recommendation consumer (subscription page, templates and
    automations) on the same resilient path instead of requiring each caller to
    remember to supply live data. V3 may repeat one client across inbounds, so
    the highest counter is the canonical observation.
    """
    wanted_sid = int(server_id)
    wanted_sub = str(sub_id or '').strip()
    best = None
    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        try:
            if int(inbound.get('server_id')) != wanted_sid:
                continue
        except (TypeError, ValueError):
            continue
        for client in inbound.get('clients') or []:
            client_sub = str(client.get('subId') or client.get('id') or '').strip()
            if client_sub != wanted_sub:
                continue
            try:
                total = max(0, int(client.get('up') or 0)) + max(0, int(client.get('down') or 0))
                limit = max(0, int(client.get('totalGB') or 0))
            except (TypeError, ValueError):
                continue
            try:
                expiry = int(client.get('expiryTimestamp') or client.get('expiryTime') or 0)
            except (TypeError, ValueError):
                expiry = 0
            candidate = {
                'total_bytes': total,
                'volume_limit_bytes': limit,
                'expiry_ts_ms': expiry,
                'observed_at': datetime.utcnow(),
            }
            if best is None or total > best['total_bytes']:
                best = candidate
    return best or {}


def _build_subscription_package_recommendation(server_id: int, sub_id: str,
                                               packages: list[dict], *,
                                               terminal: bool = False,
                                               live_usage: dict | None = None) -> dict | None:
    """Build an explainable, uncertainty-aware 31-day usage recommendation.

    The latest renewal cycle is preferred when it contains enough evidence. If
    that cycle is new/sparse, the model falls back to all usage from the last 31
    days. Account state never gates eligibility: active, ending, ended and
    expired accounts are treated identically. Only a truly usage-free 31-day
    window produces no recommendation. ``live_usage`` is the request-time
    counter from the panel and closes the blind spot between hourly rollups.
    """
    if not packages:
        return None

    now = datetime.utcnow()
    cutoff = now - timedelta(days=31)
    try:
        latest_renewal = (RenewalEvent.query
                          .filter_by(server_id=int(server_id), sub_id=str(sub_id))
                          .filter(RenewalEvent.renewed_at >= cutoff)
                          .order_by(RenewalEvent.renewed_at.desc())
                          .first())
        rows_31d = (UsageDaily.query
                    .filter_by(server_id=int(server_id), sub_id=str(sub_id))
                    .filter(UsageDaily.last_observed_at >= cutoff)
                    .order_by(UsageDaily.usage_date.asc())
                    .all())
        state = UsageCounterState.query.filter_by(server_id=int(server_id), sub_id=str(sub_id)).first()
    except Exception as exc:
        # Recommendation is customer-facing and must survive a delayed/partial
        # rollup migration after an update. Degrade to the live panel counter;
        # the background worker can repair history independently.
        from app import app  # deferred: Flask instance lives in app.py (circular at module level)
        app.logger.warning('Recommendation history unavailable; using live counter: %s', exc)
        latest_renewal, rows_31d, state = None, [], None

    live_usage = live_usage or _live_subscription_usage(server_id, sub_id)
    try:
        live_total = max(0, int(live_usage.get('total_bytes') or 0))
    except (TypeError, ValueError):
        live_total = 0
    live_observed_at = live_usage.get('observed_at')
    if not isinstance(live_observed_at, datetime):
        live_observed_at = now

    def _evidence(candidate_rows):
        total = 0
        samples = 0
        active_rows = []
        for usage_row in candidate_rows:
            delta = (max(0, int(usage_row.upload_bytes or 0))
                     + max(0, int(usage_row.download_bytes or 0)))
            total += delta
            samples += max(0, int(usage_row.sample_count or 0))
            if delta > 0:
                active_rows.append(usage_row)
        return total, samples, active_rows

    cycle_anchor = max(cutoff, latest_renewal.renewed_at) if latest_renewal else None
    cycle_rows = ([row for row in rows_31d
                   if (row.last_observed_at or datetime.min) >= cycle_anchor]
                  if cycle_anchor else [])
    cycle_total, cycle_samples, cycle_active = _evidence(cycle_rows)
    full_total, full_samples, full_active = _evidence(rows_31d)

    # Infer a cycle anchor even when the hourly collector first sees an account
    # after it has already consumed traffic. A matching visible package gives us
    # the configured duration; otherwise the 31-day window is the conservative
    # fallback. This is evidence for elapsed time only—the live panel counter is
    # still the source of consumed bytes.
    inferred_live_start = None
    try:
        expiry_ms = int(live_usage.get('expiry_ts_ms') or 0)
        limit_bytes = max(0, int(live_usage.get('volume_limit_bytes') or 0))
        if expiry_ms > 0 and limit_bytes > 0:
            limit_gb = limit_bytes / float(1024 ** 3)
            matching_days = [
                int(package.get('days') or 0)
                for package in packages
                if int(package.get('days') or 0) > 0
                and int(package.get('volume') or 0) > 0
                and abs(float(package.get('volume') or 0) - limit_gb) <= 0.25
            ]
            if matching_days:
                inferred_live_start = (
                    datetime.utcfromtimestamp(expiry_ms / 1000.0)
                    - timedelta(days=min(matching_days, key=lambda days: abs(days - 31)))
                )
    except (TypeError, ValueError, OverflowError, OSError):
        inferred_live_start = None

    # A mature current cycle gives the cleanest daily rate. A sparse/new cycle
    # is not a reason to hide the recommendation: use the complete 31-day view.
    cycle_is_reliable = bool(
        cycle_active and cycle_samples >= 2 and cycle_total >= 100 * 1024 * 1024
    )
    if cycle_anchor and (cycle_is_reliable or live_total > 0):
        rows = cycle_rows
        # The panel's current counter is the complete current-cycle total. It
        # repairs the collector's intentional zero delta on a first observation.
        total_bytes = max(cycle_total, live_total)
        total_samples, meaningful_rows = cycle_samples, cycle_active
        anchor = cycle_anchor
        source = 'current_cycle'
    else:
        rows = rows_31d
        if latest_renewal and live_total > 0:
            # Preserve previous-cycle rollups while filling only the unobserved
            # part of the current counter; never double-count tracked bytes.
            total_bytes = full_total + max(0, live_total - cycle_total)
        else:
            # Without a known reset boundary overlap is unknowable. max() is a
            # safe lower bound and is preferable to hiding the recommendation.
            total_bytes = max(full_total, live_total)
        total_samples, meaningful_rows = full_samples, full_active
        anchor = cutoff
        source = 'last_31_days'

    # Any real traffic is enough to calculate a recommendation. Zero usage is
    # the only intentional no-recommendation outcome.
    if total_bytes <= 0:
        return None

    if source == 'current_cycle' and live_total > 0:
        # The live counter covers the whole reset cycle, not merely the period
        # after the first rollup sample.
        first_at = max(anchor, inferred_live_start or anchor)
        last_active_at = max(
            meaningful_rows[-1].last_observed_at if meaningful_rows else first_at,
            live_observed_at,
        )
    elif meaningful_rows:
        first_at = max(anchor, meaningful_rows[0].first_observed_at or anchor)
        last_active_at = meaningful_rows[-1].last_observed_at or first_at
    else:
        first_at = max(anchor, inferred_live_start or anchor)
        last_active_at = live_observed_at

    # If the live counter contains bytes not represented by rollups, it is the
    # newest evidence even for an ended account (for example, a 10 GB package
    # exhausted between two hourly samples).
    has_untracked_live_usage = live_total > cycle_total
    if terminal and not has_untracked_live_usage:
        end_at = last_active_at
    else:
        observed_at = (state.observed_at if state and state.observed_at
                       else (rows[-1].last_observed_at if rows else live_observed_at))
        end_at = max(last_active_at, min(observed_at or live_observed_at or now, now))

    # A one-day floor prevents a partial first day from projecting an absurd
    # monthly amount while preserving the requested "used it in 3 days" signal.
    basis_days = min(31.0, max(1.0, (end_at - first_at).total_seconds() / 86400.0))
    average_daily_gb = (total_bytes / float(1024 ** 3)) / basis_days
    if average_daily_gb <= 0:
        return None

    covered_dates = len({row.usage_date for row in rows})
    if live_total > 0:
        covered_dates = max(1, covered_dates)
    if basis_days >= 14 and covered_dates >= 10:
        confidence, safety_margin = 'high', 0.10
    elif basis_days >= 5 and covered_dates >= 4:
        confidence, safety_margin = 'medium', 0.15
    else:
        confidence, safety_margin = 'early', 0.20

    selected, comfort, buffered_requirement = _select_subscription_package(
        packages, average_daily_gb, safety_margin,
    )
    if not selected:
        return None

    projected_31d_gb = average_daily_gb * 31.0
    selected_volume = int(selected.get('volume') or 0)
    capacity_limited = bool(selected_volume > 0 and selected_volume + 0.01 < projected_31d_gb)
    return {
        'model_version': 'usage-fit-v3',
        'package_id': int(selected.get('id')),
        'package_name': str(selected.get('name') or ''),
        'package_volume': int(selected.get('volume') or 0),
        'package_days': int(selected.get('days') or 0),
        'package_price': int(selected.get('price') or 0),
        'comfort_package_id': int(comfort.get('id')) if comfort else None,
        'comfort_package_name': str(comfort.get('name') or '') if comfort else '',
        'comfort_package_volume': int(comfort.get('volume') or 0) if comfort else 0,
        'comfort_package_days': int(comfort.get('days') or 0) if comfort else 0,
        'comfort_package_price': int(comfort.get('price') or 0) if comfort else 0,
        'average_daily_gb': round(average_daily_gb, 2),
        'projected_31d_gb': round(projected_31d_gb, 1),
        'buffered_requirement_gb': round(buffered_requirement, 1),
        'basis_days': round(basis_days, 1),
        'covered_days': covered_dates,
        'confidence': confidence,
        'safety_margin_percent': int(round(safety_margin * 100)),
        'source': source,
        'fast_cycle': bool(source == 'current_cycle' and terminal and basis_days <= 7),
        'capacity_limited': capacity_limited,
        'capacity_shortfall_gb': round(max(0.0, projected_31d_gb - selected_volume), 1) if capacity_limited else 0,
    }


RECOMMENDATION_TEMPLATE_TOKENS = (
    '{if_recommendation}', '{recommended_package}', '{recommended_volume}',
    '{recommended_days}', '{recommended_price}', '{recommended_daily_usage}',
    '{recommended_31d_usage}', '{if_comfort_recommendation}',
    '{comfort_package}', '{comfort_volume}', '{comfort_days}', '{comfort_price}',
)


def _template_wants_recommendation(template: str | None) -> bool:
    raw = str(template or '')
    return any(token in raw for token in RECOMMENDATION_TEMPLATE_TOKENS)


def _empty_recommendation_template_vars() -> dict:
    return {
        'recommendation': '', 'recommendation_given': False,
        'recommended_package': '', 'recommended_volume': '',
        'recommended_days': '', 'recommended_price': '',
        'recommended_daily_usage': '', 'recommended_31d_usage': '',
        'recommended_package_id': None,
        'comfort_recommendation': '', 'comfort_recommendation_given': False,
        'comfort_package': '', 'comfort_volume': '',
        'comfort_days': '', 'comfort_price': '',
    }


def _recommendation_template_vars(server_id, sub_id: str, email: str = '', *,
                                  terminal: bool = False) -> dict:
    """Return safe template variables backed by the subscription recommender.

    Missing/insufficient usage history is represented by false conditional flags,
    so `{if_recommendation}...{/if_recommendation}` disappears cleanly.
    """
    values = _empty_recommendation_template_vars()
    try:
        sid = int(server_id)
        account_sub_id = str(sub_id or '').strip()
        if not account_sub_id:
            return values

        owner = None
        email_l = str(email or '').strip().lower()
        if email_l:
            ownership = ClientOwnership.query.filter(
                ClientOwnership.server_id == sid,
                func.lower(ClientOwnership.client_email) == email_l,
            ).first()
            if ownership and ownership.reseller and ownership.reseller.role == 'reseller':
                owner = ownership.reseller

        recommendation = _build_subscription_package_recommendation(
            sid, account_sub_id, _build_sub_page_packages(owner), terminal=terminal,
        )
        if not recommendation:
            return values

        def _volume(value):
            amount = int(value or 0)
            return 'Unlimited' if amount == 0 else str(amount)

        def _days(value):
            amount = int(value or 0)
            return 'Unlimited' if amount == 0 else str(amount)

        values.update({
            'recommendation': recommendation.get('package_name') or '',
            'recommendation_given': True,
            'recommended_package': recommendation.get('package_name') or '',
            'recommended_volume': _volume(recommendation.get('package_volume')),
            'recommended_days': _days(recommendation.get('package_days')),
            'recommended_price': f"{int(recommendation.get('package_price') or 0):,}",
            'recommended_daily_usage': str(recommendation.get('average_daily_gb') or ''),
            'recommended_31d_usage': str(recommendation.get('projected_31d_gb') or ''),
            'recommended_package_id': recommendation.get('package_id'),
        })
        if recommendation.get('comfort_package_id'):
            values.update({
                'comfort_recommendation': recommendation.get('comfort_package_name') or '',
                'comfort_recommendation_given': True,
                'comfort_package': recommendation.get('comfort_package_name') or '',
                'comfort_volume': _volume(recommendation.get('comfort_package_volume')),
                'comfort_days': _days(recommendation.get('comfort_package_days')),
                'comfort_price': f"{int(recommendation.get('comfort_package_price') or 0):,}",
            })
        return values
    except Exception:
        from app import app  # deferred: Flask instance lives in app.py (circular at module level)
        app.logger.exception('Failed to build package recommendation template variables')
        return values


def get_config(key, default=0):
    conf = db.session.get(SystemConfig, key)
    return int(conf.value) if conf else default

def log_transaction(user_id, amount, type, desc, server_id=None, card_id=None, sender_card=None, category='usage', client_email=None, package_name=None, volume_gb=None, days=None):
    trans = Transaction(
        admin_id=user_id,
        amount=amount,
        type=type,
        description=desc,
        server_id=server_id,
        card_id=card_id,
        sender_card=sender_card,
        category=category,
        client_email=client_email,
        package_name=package_name,
        volume_gb=volume_gb,
        days=days
    )
    db.session.add(trans)

def inject_wallet_credit():
    # Deferred: timezone/calendar/lang helpers live in app.py (circular at module level).
    from app import (
        DEFAULT_APP_TIMEZONE, DEFAULT_APP_CALENDAR,
        _get_app_timezone_name, _get_app_calendar_name, _get_panel_ui_lang,
    )
    wallet_credit = 0
    app_timezone = DEFAULT_APP_TIMEZONE
    app_calendar = DEFAULT_APP_CALENDAR
    panel_lang = 'en'
    admin_id = session.get('admin_id')
    if admin_id:
        user = db.session.get(Admin, admin_id)
        if user:
            wallet_credit = user.credit or 0
    try:
        app_timezone = _get_app_timezone_name()
    except Exception:
        app_timezone = DEFAULT_APP_TIMEZONE

    try:
        app_calendar = _get_app_calendar_name()
    except Exception:
        app_calendar = DEFAULT_APP_CALENDAR

    try:
        panel_lang = _get_panel_ui_lang()
    except Exception:
        panel_lang = 'en'

    return {
        "wallet_credit": wallet_credit,
        "app_timezone": app_timezone,
        "app_calendar": app_calendar,
        "panel_lang": panel_lang,
        "panel_dir": ('rtl' if panel_lang == 'fa' else 'ltr'),
    }

