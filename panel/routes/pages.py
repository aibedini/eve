"""HTML page-render routes (extracted from app.py)."""
from flask import Blueprint, redirect, render_template, session, url_for

from telegram_xray import find_xray_binary

from panel.extensions import db
from panel.models import (
    Admin, BankCard, PulseAgent, PulseRun, PulseTemplate, Server, SystemConfig,
    get_pulse_settings,
)
from panel.routes.common import login_required, user_management_required
from panel.services.billing import calculate_reseller_price, get_config
from panel.services.subscription import (
    DEFAULT_SUBSCRIPTION_STATISTICS_TEMPLATE_EN,
    DEFAULT_SUBSCRIPTION_STATISTICS_TEMPLATE_FA,
    SUBSCRIPTION_STATISTICS_ENABLED_KEY,
    SUBSCRIPTION_STATISTICS_TEMPLATE_EN_KEY,
    SUBSCRIPTION_STATISTICS_TEMPLATE_FA_KEY,
)

bp = Blueprint('pages', __name__)


@bp.route('/')
@login_required
def dashboard():
    from app import get_accessible_servers  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    servers = get_accessible_servers(user)
    
    base_cost_day = get_config('cost_per_day', 0)
    base_cost_gb = get_config('cost_per_gb', 0)
    base_cost_day_unlimited = get_config('cost_per_day_unlimited', 0)

    # Calculate user-specific costs
    user_cost_day = calculate_reseller_price(user, base_price=base_cost_day, cost_type='day')
    user_cost_gb = calculate_reseller_price(user, base_price=base_cost_gb, cost_type='gb')
    user_cost_day_unlimited = calculate_reseller_price(user, base_price=base_cost_day_unlimited, cost_type='day')

    # Get active bank cards for payment forms
    bank_cards = BankCard.query.filter_by(is_active=True).all()

    return render_template('dashboard.html',
                         servers=servers,
                         server_count=len(servers),
                         admin_username=user.username,
                         is_superadmin=(user.role == 'superadmin' or user.is_superadmin),
                         role=user.role,
                         allow_free=(user.role != 'reseller' or bool(getattr(user, 'allow_free_creation', False))),
                         credit=user.credit,
                         base_cost_day=user_cost_day,
                         base_cost_gb=user_cost_gb,
                         base_cost_day_unlimited=user_cost_day_unlimited,
                         bank_cards=bank_cards)

@bp.route('/servers')
@login_required
def servers_page():
    return render_template('servers.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'))


@bp.route('/monitor')
@login_required
def monitor_page():
    return render_template('monitor.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'))

@bp.route('/pulse')
@login_required
def pulse_page():
    from app import (
        PULSE_COPY, _get_panel_ui_lang, _pulse_queue_snapshot,
        format_app_datetime, get_accessible_servers,
    )  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    servers = get_accessible_servers(user)
    runs = (PulseRun.query
            .order_by(PulseRun.created_at.desc(), PulseRun.id.desc())
            .limit(25).all())
    queue_runs = _pulse_queue_snapshot()
    history_runs = [run for run in runs if run.status not in ('queued', 'running')]
    agents = PulseAgent.query.order_by(PulseAgent.name.asc()).all()
    templates = PulseTemplate.query.order_by(PulseTemplate.name.asc()).all()
    panel_lang = _get_panel_ui_lang()
    xray_installed = bool(find_xray_binary())
    return render_template('pulse.html',
                         runs=history_runs,
                         queue_runs=queue_runs,
                         servers=servers,
                         settings=get_pulse_settings(),
                         agents=agents,
                         pulse_templates=templates,
                         pulse_copy=PULSE_COPY[panel_lang],
                         xray_installed=xray_installed,
                         format_app_datetime=format_app_datetime,
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'))

@bp.route('/royalty')
@login_required
def royalty_page():
    return render_template('royalty.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'))

@bp.route('/merger')
@login_required
def merger_page():
    from app import _merger_user_is_allowed  # deferred: app-level helper, avoids circular import
    if not _merger_user_is_allowed():
        return redirect(url_for('pages.dashboard'))
    return render_template('merger.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'))

@bp.route('/admins')
@login_required
def admins_page():
    from app import _get_panel_ui_lang  # deferred: app-level helper, avoids circular import
    if session.get('role') == 'reseller':
        return redirect(url_for('pages.dashboard'))
    return render_template('admins.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         panel_lang=_get_panel_ui_lang(),
                         role=session.get('role', 'admin'))

@bp.route('/settings')
@login_required
def settings_page():
    from app import (
        APP_VERSION,
        DEFAULT_TG_TPL_NEAR_EXPIRY, DEFAULT_TG_TPL_LOW_VOLUME, DEFAULT_TG_TPL_RENEW,
        DEFAULT_WHATSAPP_BOT_TPL_CREATED, DEFAULT_WHATSAPP_BOT_TPL_ENDED,
        DEFAULT_WHATSAPP_BOT_TPL_INFO, DEFAULT_WHATSAPP_BOT_TPL_RENEW,
        DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY, DEFAULT_WHATSAPP_TEMPLATE_RENEW,
        DEFAULT_WHATSAPP_TEMPLATE_WELCOME,
        _get_sms_runtime_settings, _get_telegram_depletion_settings,
        _get_whatsapp_runtime_settings,
    )  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user.is_superadmin:
        return redirect(url_for('pages.dashboard'))
    whatsapp_cfg = _get_whatsapp_runtime_settings()
    sms_cfg = _get_sms_runtime_settings()
    telegram_notify_cfg = _get_telegram_depletion_settings()
    return render_template('settings.html',
                         current_user=user,
                         is_superadmin=user.is_superadmin,
                         app_version=APP_VERSION,
                         admin_username=user.username,
                         role=user.role,
                         sms_automation_enabled=sms_cfg.get('enabled', False),
                         sms_gmweb_base_url=sms_cfg.get('base_url', ''),
                         sms_gmweb_api_key=sms_cfg.get('api_key', ''),
                         sms_gmweb_timeout=sms_cfg.get('timeout_seconds', 15),
                         sms_trigger_created=sms_cfg.get('trigger_created', True),
                         sms_trigger_renew=sms_cfg.get('trigger_renew', True),
                         sms_trigger_depletion=sms_cfg.get('trigger_depletion', False),
                         sms_trigger_near_expiry=sms_cfg.get('trigger_near_expiry', False),
                         sms_trigger_low_volume=sms_cfg.get('trigger_low_volume', False),
                         sms_trigger_expired=sms_cfg.get('trigger_expired', False),
                         sms_trigger_ended=sms_cfg.get('trigger_ended', False),
                         sms_depletion_expiry_days=sms_cfg.get('depletion_expiry_days', 3),
                         sms_depletion_volume_gb=sms_cfg.get('depletion_volume_gb', 2.0),
                         sms_depletion_cooldown_days=sms_cfg.get('depletion_cooldown_days', 7),
                         sms_cooldown_hours_near_expiry=(sms_cfg.get('cooldown_hours') or {}).get('near_expiry', 24),
                         sms_cooldown_hours_low_volume=(sms_cfg.get('cooldown_hours') or {}).get('low_volume', 24),
                         sms_cooldown_hours_expired=(sms_cfg.get('cooldown_hours') or {}).get('expired', 48),
                         sms_cooldown_hours_ended=(sms_cfg.get('cooldown_hours') or {}).get('ended', 24),
                         sms_expired_max_age_days=sms_cfg.get('expired_max_age_days', 30),
                         sms_ended_max_age_days=sms_cfg.get('ended_max_age_days', 0),
                         sms_min_interval_seconds=sms_cfg.get('min_interval_seconds', 30),
                         sms_daily_limit=sms_cfg.get('daily_limit', 200),
                         sms_send_pace_seconds=sms_cfg.get('send_pace_seconds', 3.0),
                         sms_quiet_enabled=sms_cfg.get('quiet_enabled', False),
                         sms_quiet_start=sms_cfg.get('quiet_start', 2),
                         sms_quiet_end=sms_cfg.get('quiet_end', 8),
                         sms_skip_unlimited=sms_cfg.get('skip_unlimited', False),
                         sms_trigger_royalty=sms_cfg.get('trigger_royalty', False),
                         sms_royalty_days=sms_cfg.get('royalty_days', 3),
                         sms_royalty_cooldown_days=sms_cfg.get('royalty_cooldown_days', 30),
                         tg_depletion_enabled=telegram_notify_cfg.get('enabled', False),
                         tg_depletion_recommend=telegram_notify_cfg.get('recommend', False),
                         tg_trigger_renew_success=telegram_notify_cfg.get('trigger_renew_success', True),
                         tg_depletion_expiry_days=telegram_notify_cfg.get('expiry_days', 3),
                         tg_depletion_volume_gb=telegram_notify_cfg.get('volume_gb', 2.0),
                         tg_depletion_cooldown_days=telegram_notify_cfg.get('cooldown_days', 7),
                         tg_tpl_renew=telegram_notify_cfg.get('tpl_renew', DEFAULT_TG_TPL_RENEW),
                         tg_tpl_near_expiry=telegram_notify_cfg.get('tpl_near_expiry', DEFAULT_TG_TPL_NEAR_EXPIRY),
                         tg_tpl_low_volume=telegram_notify_cfg.get('tpl_low_volume', DEFAULT_TG_TPL_LOW_VOLUME),
                         whatsapp_deployment_region=whatsapp_cfg.get('deployment_region', 'outside'),
                         whatsapp_enabled=whatsapp_cfg.get('enabled_requested', False),
                         whatsapp_provider=whatsapp_cfg.get('provider', 'baileys'),
                         whatsapp_trigger_renew_success=whatsapp_cfg.get('trigger_renew_success', True),
                         whatsapp_trigger_welcome=whatsapp_cfg.get('trigger_welcome', False),
                         whatsapp_trigger_pre_expiry=whatsapp_cfg.get('trigger_pre_expiry', False),
                         whatsapp_min_interval_seconds=whatsapp_cfg.get('min_interval_seconds', 45),
                         whatsapp_daily_limit=whatsapp_cfg.get('daily_limit', 100),
                         whatsapp_pre_expiry_hours=whatsapp_cfg.get('pre_expiry_hours', 24),
                         whatsapp_retry_count=whatsapp_cfg.get('retry_count', 3),
                         whatsapp_backoff_seconds=whatsapp_cfg.get('backoff_seconds', 30),
                         whatsapp_circuit_breaker=whatsapp_cfg.get('circuit_breaker', True),
                         whatsapp_gateway_url=whatsapp_cfg.get('gateway_url', ''),
                         whatsapp_gateway_api_key=whatsapp_cfg.get('gateway_api_key', ''),
                         whatsapp_gateway_timeout_seconds=whatsapp_cfg.get('gateway_timeout_seconds', 10),
                         whatsapp_session_id=whatsapp_cfg.get('session_id', ''),
                         whatsapp_template_renew=whatsapp_cfg.get('template_renew', DEFAULT_WHATSAPP_TEMPLATE_RENEW),
                         whatsapp_template_welcome=whatsapp_cfg.get('template_welcome', DEFAULT_WHATSAPP_TEMPLATE_WELCOME),
                         whatsapp_template_pre_expiry=whatsapp_cfg.get('template_pre_expiry', DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY),
                         whatsapp_warmup_enabled=whatsapp_cfg.get('warmup_enabled', False),
                         whatsapp_warmup_start_date=whatsapp_cfg.get('warmup_start_date', ''),
                         whatsapp_warmup_start_per_day=whatsapp_cfg.get('warmup_start_per_day', 20),
                         whatsapp_warmup_ramp_days=whatsapp_cfg.get('warmup_ramp_days', 14),
                         whatsapp_pace_enabled=whatsapp_cfg.get('pace_enabled', False),
                         whatsapp_pace_min_gap_seconds=whatsapp_cfg.get('pace_min_gap_seconds', 8),
                         whatsapp_pace_jitter_seconds=whatsapp_cfg.get('pace_jitter_seconds', 5),
                         whatsapp_depletion_enabled=whatsapp_cfg.get('depletion_enabled', False),
                         whatsapp_depletion_expiry_days=whatsapp_cfg.get('depletion_expiry_days', 3),
                         whatsapp_depletion_volume_gb=whatsapp_cfg.get('depletion_volume_gb', 2.0),
                         whatsapp_depletion_cooldown_days=whatsapp_cfg.get('depletion_cooldown_days', 7),
                         whatsapp_bot_tpl_created=whatsapp_cfg.get('bot_tpl_created', DEFAULT_WHATSAPP_BOT_TPL_CREATED),
                         whatsapp_bot_tpl_renew=whatsapp_cfg.get('bot_tpl_renew', DEFAULT_WHATSAPP_BOT_TPL_RENEW),
                         whatsapp_bot_tpl_ended=whatsapp_cfg.get('bot_tpl_ended', DEFAULT_WHATSAPP_BOT_TPL_ENDED),
                         whatsapp_bot_tpl_info=whatsapp_cfg.get('bot_tpl_info', DEFAULT_WHATSAPP_BOT_TPL_INFO))

@bp.route('/my-packages')
@login_required
def reseller_packages_page():
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return redirect(url_for('pages.dashboard'))
    return render_template('reseller_packages.html',
                           admin_username=session.get('admin_username'),
                           is_superadmin=False,
                           role='reseller')


@bp.route('/reseller/telegram-bot')
@login_required
def reseller_telegram_bot_page():
    user = db.session.get(Admin, session['admin_id'])
    if not user or user.role != 'reseller':
        return redirect(url_for('pages.dashboard'))
    return render_template('reseller_telegram_bot.html',
                           admin_username=session.get('admin_username'),
                           is_superadmin=False,
                           role='reseller')

@bp.route('/finance')
@login_required
def finance_page():
    user = db.session.get(Admin, session['admin_id'])
    cards = BankCard.query.filter_by(is_active=True).all()
    servers = Server.query.order_by(Server.name).all()
    
    # Always show user column, but for reseller only their own username
    admin_options = []
    is_superadmin_view = (user.role == 'superadmin')
    if is_superadmin_view:
        admin_options = Admin.query.order_by(Admin.username).all()
    else:
        admin_options = [user]
    return render_template('finance.html', 
                           cards=cards, 
                           is_superadmin=(user.role == 'superadmin' or user.is_superadmin),
                           admin_username=user.username,
                           role=user.role,
                           wallet_credit=user.credit,
                           admin_options=admin_options,
                           servers=servers)

@bp.route('/sub-manager')
@user_management_required
def sub_manager_page():
    from app import (
        DEFAULT_WHATSAPP_BOT_TPL_CREATED, DEFAULT_WHATSAPP_BOT_TPL_ENDED,
        DEFAULT_WHATSAPP_BOT_TPL_INFO, DEFAULT_WHATSAPP_BOT_TPL_RENEW,
        DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY, DEFAULT_WHATSAPP_TEMPLATE_RENEW,
        DEFAULT_WHATSAPP_TEMPLATE_WELCOME,
        _get_system_configs_batch, _get_whatsapp_runtime_settings,
    )  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    
    _support_cfg = _get_system_configs_batch(['support_telegram', 'support_whatsapp', 'support_sms', 'channel_telegram', 'channel_whatsapp'])
    whatsapp_cfg = _get_whatsapp_runtime_settings()
    statistics_rows = _get_system_configs_batch([
        SUBSCRIPTION_STATISTICS_ENABLED_KEY,
        SUBSCRIPTION_STATISTICS_TEMPLATE_FA_KEY,
        SUBSCRIPTION_STATISTICS_TEMPLATE_EN_KEY,
    ])
    statistics_enabled_raw = statistics_rows.get(SUBSCRIPTION_STATISTICS_ENABLED_KEY)
    statistics_enabled = (
        True if statistics_enabled_raw is None
        else str(statistics_enabled_raw).strip().lower() in {'1', 'true', 'yes', 'on'}
    )
    
    return render_template('sub_manager.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'),
                         support_telegram=_support_cfg.get('support_telegram') or '',
                         support_whatsapp=_support_cfg.get('support_whatsapp') or '',
                         support_sms=_support_cfg.get('support_sms') or '',
                         channel_telegram=_support_cfg.get('channel_telegram') or '',
                         channel_whatsapp=_support_cfg.get('channel_whatsapp') or '',
                         whatsapp_deployment_region=whatsapp_cfg.get('deployment_region', 'outside'),
                         whatsapp_enabled=whatsapp_cfg.get('enabled_requested', False),
                         whatsapp_provider=whatsapp_cfg.get('provider', 'baileys'),
                         whatsapp_trigger_renew_success=whatsapp_cfg.get('trigger_renew_success', True),
                         whatsapp_trigger_welcome=whatsapp_cfg.get('trigger_welcome', False),
                         whatsapp_trigger_pre_expiry=whatsapp_cfg.get('trigger_pre_expiry', False),
                         whatsapp_min_interval_seconds=whatsapp_cfg.get('min_interval_seconds', 45),
                         whatsapp_daily_limit=whatsapp_cfg.get('daily_limit', 100),
                         whatsapp_pre_expiry_hours=whatsapp_cfg.get('pre_expiry_hours', 24),
                         whatsapp_retry_count=whatsapp_cfg.get('retry_count', 3),
                         whatsapp_backoff_seconds=whatsapp_cfg.get('backoff_seconds', 30),
                         whatsapp_circuit_breaker=whatsapp_cfg.get('circuit_breaker', True),
                         whatsapp_gateway_url=whatsapp_cfg.get('gateway_url', ''),
                         whatsapp_gateway_api_key=whatsapp_cfg.get('gateway_api_key', ''),
                         whatsapp_gateway_timeout_seconds=whatsapp_cfg.get('gateway_timeout_seconds', 10),
                         whatsapp_session_id=whatsapp_cfg.get('session_id', ''),
                         whatsapp_template_renew=whatsapp_cfg.get('template_renew', DEFAULT_WHATSAPP_TEMPLATE_RENEW),
                         whatsapp_template_welcome=whatsapp_cfg.get('template_welcome', DEFAULT_WHATSAPP_TEMPLATE_WELCOME),
                         statistics_enabled=statistics_enabled,
                         statistics_template_fa=(
                             statistics_rows.get(SUBSCRIPTION_STATISTICS_TEMPLATE_FA_KEY)
                             or DEFAULT_SUBSCRIPTION_STATISTICS_TEMPLATE_FA
                         ),
                         statistics_template_en=(
                             statistics_rows.get(SUBSCRIPTION_STATISTICS_TEMPLATE_EN_KEY)
                             or DEFAULT_SUBSCRIPTION_STATISTICS_TEMPLATE_EN
                         ),
                         whatsapp_template_pre_expiry=whatsapp_cfg.get('template_pre_expiry', DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY),
                         whatsapp_warmup_enabled=whatsapp_cfg.get('warmup_enabled', False),
                         whatsapp_warmup_start_date=whatsapp_cfg.get('warmup_start_date', ''),
                         whatsapp_warmup_start_per_day=whatsapp_cfg.get('warmup_start_per_day', 20),
                         whatsapp_warmup_ramp_days=whatsapp_cfg.get('warmup_ramp_days', 14),
                         whatsapp_pace_enabled=whatsapp_cfg.get('pace_enabled', False),
                         whatsapp_pace_min_gap_seconds=whatsapp_cfg.get('pace_min_gap_seconds', 8),
                         whatsapp_pace_jitter_seconds=whatsapp_cfg.get('pace_jitter_seconds', 5),
                         whatsapp_depletion_enabled=whatsapp_cfg.get('depletion_enabled', False),
                         whatsapp_depletion_expiry_days=whatsapp_cfg.get('depletion_expiry_days', 3),
                         whatsapp_depletion_volume_gb=whatsapp_cfg.get('depletion_volume_gb', 2.0),
                         whatsapp_depletion_cooldown_days=whatsapp_cfg.get('depletion_cooldown_days', 7),
                         whatsapp_bot_tpl_created=whatsapp_cfg.get('bot_tpl_created', DEFAULT_WHATSAPP_BOT_TPL_CREATED),
                         whatsapp_bot_tpl_renew=whatsapp_cfg.get('bot_tpl_renew', DEFAULT_WHATSAPP_BOT_TPL_RENEW),
                         whatsapp_bot_tpl_ended=whatsapp_cfg.get('bot_tpl_ended', DEFAULT_WHATSAPP_BOT_TPL_ENDED),
                         whatsapp_bot_tpl_info=whatsapp_cfg.get('bot_tpl_info', DEFAULT_WHATSAPP_BOT_TPL_INFO))

@bp.route('/packages')
@user_management_required
def packages_page():
    cost_gb = db.session.get(SystemConfig, 'cost_per_gb')
    cost_day = db.session.get(SystemConfig, 'cost_per_day')
    cost_day_unlimited = db.session.get(SystemConfig, 'cost_per_day_unlimited')

    return render_template('packages.html',
                         base_cost_gb=int(cost_gb.value) if cost_gb else 0,
                         base_cost_day=int(cost_day.value) if cost_day else 0,
                         base_cost_day_unlimited=int(cost_day_unlimited.value) if cost_day_unlimited else 0,
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'))

@bp.route('/bank-cards')
@user_management_required
def bank_cards_page():
    return render_template('bank_cards.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'))

@bp.route('/custom-subscriptions')
@user_management_required
def custom_subscriptions_page():
    return render_template(
        'custom_subscriptions.html',
        admin_username=session.get('admin_username'),
        is_superadmin=session.get('is_superadmin', False),
        role=session.get('role', 'admin'),
    )

@bp.route('/transactions')
@login_required
def transactions_page():
    from app import get_accessible_servers  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    servers = get_accessible_servers(user, include_disabled=True) if user else []
    admin_options = []
    if user and (user.role == 'superadmin' or user.is_superadmin):
        admin_options = Admin.query.order_by(Admin.username.asc()).all()
    return render_template('transactions.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'),
                         servers=servers,
                         admin_options=admin_options)

@bp.route('/receipts')
@login_required
def receipts_page():
    return render_template('receipts.html',
                         admin_username=session.get('admin_username'),
                         is_superadmin=session.get('is_superadmin', False),
                         role=session.get('role', 'admin'),
                         current_admin_id=session.get('admin_id'))

@bp.route('/telegram-operations')
@login_required
def telegram_operations_page():
    from app import APP_VERSION, _telegram_operations_admin  # deferred: app-level helper, avoids circular import
    user = _telegram_operations_admin()
    return render_template(
        'telegram_operations.html', current_user=user,
        is_superadmin=bool(user and user.is_superadmin),
        app_version=APP_VERSION, admin_username=user.username if user else '',
        role=user.role if user else '',
    )

@bp.route('/traffic-check')
@login_required
def traffic_check_page():
    return render_template('traffic_check.html',
                           admin_username=session.get('admin_username'),
                           is_superadmin=session.get('is_superadmin', False),
                           role=session.get('role', 'admin'))
