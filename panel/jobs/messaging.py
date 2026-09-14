"""Messaging automation workers: WhatsApp, Telegram notify, SMS/GMweb (extracted from app.py).

Depletion scans, quiet-hours/pace accounting, opt-out handling, transactional
sends, and the long-running bot worker loops for all three channels.
"""
import hashlib
import json
import logging
import math
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from sqlalchemy import func, or_

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Windows without tzdata
    ZoneInfo = None

from telegram_diagnostics import redact_connection_error

from panel.core.redis_client import (
    GLOBAL_SERVER_DATA,
    get_redis,
    load_snapshot_from_redis,
)
from panel.extensions import db
from panel.security import outbound_tls_verify
from panel.models import (
    Admin,
    Announcement,
    AnnouncementDelivery,
    ClientOwnership,
    MonitorMessageLog,
    NotificationTemplate,
    PendingSms,
    ServiceOwnership,
    SmsSendLog,
    TelegramAnnouncement,
    TelegramAnnouncementDelivery,
    TelegramBotInstance,
    TelegramIdentity,
    WhatsappBotLog,
)
from panel.jobs.refresh import (
    SMS_SCAN_CANCEL_REDIS_KEY,
    SMS_SCAN_JOB,
    SMS_SCAN_JOB_LOCK,
    SMS_SCAN_REDIS_KEY,
    SMS_SCAN_REDIS_TTL,
)
from panel.routes.templates_api import (
    DEFAULT_ROYALTY_INFO_SMS_TEMPLATE,
    ROYALTY_INFO_SMS_TEMPLATE_TYPE,
)
from panel.services import depletion_pipeline, telemetry_state
from panel.services import gmweb_contract
from panel.services import lifecycle as lifecycle_service
from panel.services.lifecycle import flush_invalidation_outbox
from panel.services.backup import _get_system_setting_value, _parse_int
from panel.services.billing import _recommendation_template_vars, _template_wants_recommendation

WHATSAPP_DEPLOYMENT_REGION_KEY = 'whatsapp_deployment_region'
WHATSAPP_ENABLED_KEY = 'whatsapp_enabled'
WHATSAPP_PROVIDER_KEY = 'whatsapp_provider'
WHATSAPP_TRIGGER_RENEW_KEY = 'whatsapp_trigger_renew_success'
WHATSAPP_TRIGGER_WELCOME_KEY = 'whatsapp_trigger_welcome'
WHATSAPP_TRIGGER_PRE_EXPIRY_KEY = 'whatsapp_trigger_pre_expiry'
WHATSAPP_MIN_INTERVAL_SECONDS_KEY = 'whatsapp_min_interval_seconds'
WHATSAPP_DAILY_LIMIT_KEY = 'whatsapp_daily_limit'
WHATSAPP_PRE_EXPIRY_HOURS_KEY = 'whatsapp_pre_expiry_hours'
WHATSAPP_RETRY_COUNT_KEY = 'whatsapp_retry_count'
WHATSAPP_BACKOFF_SECONDS_KEY = 'whatsapp_backoff_seconds'
WHATSAPP_CIRCUIT_BREAKER_KEY = 'whatsapp_circuit_breaker'
WHATSAPP_TEMPLATE_RENEW_KEY = 'whatsapp_template_renew'
WHATSAPP_TEMPLATE_WELCOME_KEY = 'whatsapp_template_welcome'
WHATSAPP_TEMPLATE_PRE_EXPIRY_KEY = 'whatsapp_template_pre_expiry'
WHATSAPP_GATEWAY_URL_KEY = 'whatsapp_gateway_url'
WHATSAPP_GATEWAY_API_KEY = 'whatsapp_gateway_api_key'
WHATSAPP_GATEWAY_TIMEOUT_KEY = 'whatsapp_gateway_timeout_seconds'
# OpenWA (self-hosted gateway) session name, e.g. "navid". Used only when
# provider == 'openwa' to address POST /api/sessions/{session}/messages/send-text.
WHATSAPP_SESSION_KEY = 'whatsapp_session_id'

# --- Anti-ban / warm-up / bot controls (mostly for the OpenWA provider) ---
# Warm-up: linearly ramp the daily send cap from a low start value up to the
# configured daily_limit over N days, starting from a chosen date. Lowers the
# odds of a fresh number getting flagged for a sudden volume spike.
WHATSAPP_WARMUP_ENABLED_KEY = 'whatsapp_warmup_enabled'
WHATSAPP_WARMUP_START_DATE_KEY = 'whatsapp_warmup_start_date'      # YYYY-MM-DD
WHATSAPP_WARMUP_START_PER_DAY_KEY = 'whatsapp_warmup_start_per_day'  # cap on day 0
WHATSAPP_WARMUP_RAMP_DAYS_KEY = 'whatsapp_warmup_ramp_days'        # days to reach full cap

# Global pace gate (item #4): enforce a minimum gap + random jitter between ANY
# two outbound sends (not just same-recipient), so a batch never turns into a
# burst. Built but OFF by default — turn on only when automated bulk sending.
WHATSAPP_PACE_ENABLED_KEY = 'whatsapp_pace_enabled'
WHATSAPP_PACE_MIN_GAP_KEY = 'whatsapp_pace_min_gap_seconds'
WHATSAPP_PACE_JITTER_KEY = 'whatsapp_pace_jitter_seconds'

# Near-depletion bot: a background scanner that proactively messages accounts
# whose time/volume is about to run out, exactly once per cooldown window.
WHATSAPP_DEPLETION_ENABLED_KEY = 'whatsapp_depletion_enabled'
WHATSAPP_DEPLETION_EXPIRY_DAYS_KEY = 'whatsapp_depletion_expiry_days'  # <= days left
WHATSAPP_DEPLETION_VOLUME_GB_KEY = 'whatsapp_depletion_volume_gb'      # <= GB left
WHATSAPP_DEPLETION_COOLDOWN_DAYS_KEY = 'whatsapp_depletion_cooldown_days'

# Dedicated "bot" templates for the OpenWA automation, per scenario. These are
# distinct from the SMS/monitor templates so the WhatsApp voice can differ.
WHATSAPP_BOT_TPL_CREATED_KEY = 'whatsapp_bot_tpl_created'
WHATSAPP_BOT_TPL_RENEW_KEY = 'whatsapp_bot_tpl_renew'
WHATSAPP_BOT_TPL_ENDED_KEY = 'whatsapp_bot_tpl_ended'
WHATSAPP_BOT_TPL_INFO_KEY = 'whatsapp_bot_tpl_info'

DEFAULT_WHATSAPP_BOT_TPL_CREATED = """اکانت شما ساخته شد ✅
اسم اکانت: {email}
حجم: {volume} | مدت: {days} روز
لینک اتصال: {dashboard_link}

لطفا از طریق لینک بالا به سرویس خود متصل شین."""

DEFAULT_WHATSAPP_BOT_TPL_RENEW = """تمدید شد ✅
اسم اکانت: {email}
{days_label} | {volume_label}
تاریخ انقضا: {date}
لینک: {dashboard_link}"""

DEFAULT_WHATSAPP_BOT_TPL_ENDED = """مشترک گرامی {email}،
سرویس شما رو به پایانه ⏳
زمان باقی‌مانده: {remaining_time}
حجم باقی‌مانده: {remaining_volume}

برای جلوگیری از قطعی، لطفا تمدید کنید 🙏
لینک: {dashboard_link}"""

DEFAULT_WHATSAPP_BOT_TPL_INFO = """اطلاعات اکانت شما
اسم اکانت: {email}
مدت باقی‌مانده: {remaining_time}
حجم باقی‌مانده: {remaining_volume}
لینک اتصال: {dashboard_link}"""

DEFAULT_WHATSAPP_TEMPLATE_RENEW = "سلام {user}، تمدید شما با موفقیت انجام شد."
DEFAULT_WHATSAPP_TEMPLATE_WELCOME = "سلام {user}، اشتراک شما فعال شد."
DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY = "سلام {user}، اشتراک شما تا {time_left} دیگر منقضی می‌شود."


WHATSAPP_SEND_TRACKER = {
    'per_recipient': {},
    'daily': {'date': '', 'count': 0},
    'last_global_send': 0.0,  # ts of the most recent send of any recipient (pace gate)
}
WHATSAPP_SEND_TRACKER_LOCK = threading.Lock()

def _normalize_whatsapp_region(value: str | None) -> str:
    raw = (value or '').strip().lower()
    if raw in ('iran', 'ir', 'inside', 'inside_iran', 'local', 'domestic'):
        return 'iran'
    return 'outside'


def _normalize_whatsapp_provider(value: str | None) -> str:
    raw = (value or '').strip().lower()
    if raw in ('cloud', 'meta', 'official'):
        return 'cloud'
    if raw in ('openwa', 'open-wa', 'open_wa', 'owa'):
        return 'openwa'
    return 'baileys'


def _normalize_whatsapp_session(value: str | None) -> str:
    """OpenWA session name: letters, digits, hyphens, underscores only."""
    raw = (value or '').strip()
    if not raw:
        return ''
    return re.sub(r'[^A-Za-z0-9_-]', '', raw)[:64]


def _whatsapp_chat_id(recipient: str) -> str:
    """Convert a +98XXXXXXXXXX recipient to OpenWA chatId '98XXXXXXXXXX@c.us'."""
    digits = re.sub(r'[^\d]', '', recipient or '')
    if not digits:
        return ''
    return f"{digits}@c.us"


def _normalize_whatsapp_gateway_url(value: str | None) -> str:
    raw = (value or '').strip()
    if not raw:
        return ''
    if not raw.startswith('http://') and not raw.startswith('https://'):
        raw = f"https://{raw}"
    return raw.rstrip('/')




def _openwa_session_status(gateway_url: str, api_key: str, session_name: str, timeout_seconds: int) -> dict:
    """Look up an OpenWA session by name OR UUID in GET /api/sessions.

    OpenWA's runtime engine is keyed by the session UUID, not its name —
    messaging by name fails with "not active" even when the session is ready.
    So we always resolve to the real UUID ('id') and return it for callers to
    address the send-text endpoint with.

    Returns {'found','connected','status','phone','id','error'}.
    """
    normalized = _normalize_whatsapp_gateway_url(gateway_url)
    result = {'found': False, 'connected': False, 'status': '', 'phone': '', 'id': '', 'error': None}
    if not normalized or not session_name:
        result['error'] = 'missing_gateway_or_session'
        return result
    headers = {}
    if (api_key or '').strip():
        headers['X-API-Key'] = api_key.strip()
    try:
        resp = requests.get(
            f"{normalized}/api/sessions",
            headers=headers,
            timeout=max(3, int(timeout_seconds or 10)),
            verify=outbound_tls_verify('EVE_WHATSAPP_CA_BUNDLE'),
        )
        if resp.status_code != 200:
            result['error'] = f"sessions_http_{resp.status_code}"
            return result
        rows = resp.json() or []
        target = session_name.strip().lower()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get('name') or '').strip().lower() == target or str(row.get('id') or '').strip().lower() == target:
                status = str(row.get('status') or '').strip().lower()
                result['found'] = True
                result['status'] = status
                result['phone'] = str(row.get('phone') or '')
                result['id'] = str(row.get('id') or '')
                result['connected'] = status in ('connected', 'ready', 'authenticated', 'active')
                return result
        result['error'] = 'session_not_found'
        return result
    except Exception as exc:
        result['error'] = str(exc)
        return result


# Short-lived cache of {(gateway_url, session_value): (uuid, expires_ts)} so we
# don't hit GET /api/sessions on every single message send.
_OPENWA_SESSION_ID_CACHE = {}
_OPENWA_SESSION_ID_TTL = 300  # seconds


def _openwa_resolve_session_id(gateway_url: str, api_key: str, session_value: str, timeout_seconds: int) -> tuple[str, str | None]:
    """Resolve a configured session name/UUID to the active UUID OpenWA needs.

    Returns (uuid, error). On a cache hit returns immediately. On miss, queries
    the gateway; only caches when the session is connected so a freshly
    reconnected session is picked up promptly.
    """
    normalized = _normalize_whatsapp_gateway_url(gateway_url)
    cache_key = (normalized, (session_value or '').strip().lower())
    cached = _OPENWA_SESSION_ID_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0], None

    sess = _openwa_session_status(normalized, api_key, session_value, timeout_seconds)
    if not sess.get('found'):
        return '', (sess.get('error') or 'session_not_found')
    if not sess.get('connected'):
        return sess.get('id') or '', f"session_{sess.get('status') or 'disconnected'}"
    uuid = sess.get('id') or ''
    if uuid:
        _OPENWA_SESSION_ID_CACHE[cache_key] = (uuid, time.time() + _OPENWA_SESSION_ID_TTL)
    return uuid, None




def _get_whatsapp_runtime_settings() -> dict:
    from app import _get_system_configs_batch, _parse_bool  # deferred: app-level helper, avoids circular import
    _wa_keys = [
        WHATSAPP_DEPLOYMENT_REGION_KEY, WHATSAPP_PROVIDER_KEY, WHATSAPP_ENABLED_KEY,
        WHATSAPP_TRIGGER_RENEW_KEY, WHATSAPP_TRIGGER_WELCOME_KEY, WHATSAPP_TRIGGER_PRE_EXPIRY_KEY,
        WHATSAPP_MIN_INTERVAL_SECONDS_KEY, WHATSAPP_DAILY_LIMIT_KEY, WHATSAPP_PRE_EXPIRY_HOURS_KEY,
        WHATSAPP_RETRY_COUNT_KEY, WHATSAPP_BACKOFF_SECONDS_KEY, WHATSAPP_CIRCUIT_BREAKER_KEY,
        WHATSAPP_TEMPLATE_RENEW_KEY, WHATSAPP_TEMPLATE_WELCOME_KEY, WHATSAPP_TEMPLATE_PRE_EXPIRY_KEY,
        WHATSAPP_GATEWAY_URL_KEY, WHATSAPP_GATEWAY_API_KEY, WHATSAPP_GATEWAY_TIMEOUT_KEY,
        WHATSAPP_SESSION_KEY,
        WHATSAPP_WARMUP_ENABLED_KEY, WHATSAPP_WARMUP_START_DATE_KEY,
        WHATSAPP_WARMUP_START_PER_DAY_KEY, WHATSAPP_WARMUP_RAMP_DAYS_KEY,
        WHATSAPP_PACE_ENABLED_KEY, WHATSAPP_PACE_MIN_GAP_KEY, WHATSAPP_PACE_JITTER_KEY,
        WHATSAPP_DEPLETION_ENABLED_KEY, WHATSAPP_DEPLETION_EXPIRY_DAYS_KEY,
        WHATSAPP_DEPLETION_VOLUME_GB_KEY, WHATSAPP_DEPLETION_COOLDOWN_DAYS_KEY,
        WHATSAPP_BOT_TPL_CREATED_KEY, WHATSAPP_BOT_TPL_RENEW_KEY,
        WHATSAPP_BOT_TPL_ENDED_KEY, WHATSAPP_BOT_TPL_INFO_KEY,
    ]
    _c = _get_system_configs_batch(_wa_keys)

    def _txt(key, default=''):
        v = _c.get(key)
        return str(v) if v is not None else default

    def _bool(key, default=False):
        return _parse_bool(_txt(key, 'true' if default else 'false'))

    def _int(key, default, min_value=None, max_value=None):
        return _parse_int(_txt(key, str(default)), default, min_value=min_value, max_value=max_value)

    def _float(key, default, min_value=None, max_value=None):
        try:
            val = float(_txt(key, str(default)))
        except (TypeError, ValueError):
            val = float(default)
        if min_value is not None:
            val = max(val, min_value)
        if max_value is not None:
            val = min(val, max_value)
        return val

    region = _normalize_whatsapp_region(_txt(WHATSAPP_DEPLOYMENT_REGION_KEY, 'outside'))
    provider = _normalize_whatsapp_provider(_txt(WHATSAPP_PROVIDER_KEY, 'baileys'))
    enabled_requested = _bool(WHATSAPP_ENABLED_KEY, False)
    enabled = bool(enabled_requested and region != 'iran')

    config = {
        'deployment_region': region,
        'provider': provider,
        'enabled_requested': enabled_requested,
        'enabled': enabled,
        'trigger_renew_success': _bool(WHATSAPP_TRIGGER_RENEW_KEY, True),
        'trigger_welcome': _bool(WHATSAPP_TRIGGER_WELCOME_KEY, False),
        'trigger_pre_expiry': _bool(WHATSAPP_TRIGGER_PRE_EXPIRY_KEY, False),
        'min_interval_seconds': _int(WHATSAPP_MIN_INTERVAL_SECONDS_KEY, 45, min_value=45, max_value=3600),
        'daily_limit': _int(WHATSAPP_DAILY_LIMIT_KEY, 100, min_value=1, max_value=50000),
        'pre_expiry_hours': _int(WHATSAPP_PRE_EXPIRY_HOURS_KEY, 24, min_value=1, max_value=720),
        'retry_count': _int(WHATSAPP_RETRY_COUNT_KEY, 3, min_value=0, max_value=10),
        'backoff_seconds': _int(WHATSAPP_BACKOFF_SECONDS_KEY, 30, min_value=5, max_value=3600),
        'circuit_breaker': _bool(WHATSAPP_CIRCUIT_BREAKER_KEY, True),
        'template_renew': _txt(WHATSAPP_TEMPLATE_RENEW_KEY, DEFAULT_WHATSAPP_TEMPLATE_RENEW).strip() or DEFAULT_WHATSAPP_TEMPLATE_RENEW,
        'template_welcome': _txt(WHATSAPP_TEMPLATE_WELCOME_KEY, DEFAULT_WHATSAPP_TEMPLATE_WELCOME).strip() or DEFAULT_WHATSAPP_TEMPLATE_WELCOME,
        'template_pre_expiry': _txt(WHATSAPP_TEMPLATE_PRE_EXPIRY_KEY, DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY).strip() or DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY,
        'gateway_url': _normalize_whatsapp_gateway_url(_txt(WHATSAPP_GATEWAY_URL_KEY, '')),
        'gateway_api_key': _txt(WHATSAPP_GATEWAY_API_KEY, '').strip(),
        'gateway_timeout_seconds': _int(WHATSAPP_GATEWAY_TIMEOUT_KEY, 10, min_value=3, max_value=60),
        'session_id': _normalize_whatsapp_session(_txt(WHATSAPP_SESSION_KEY, '')),
        # Warm-up
        'warmup_enabled': _bool(WHATSAPP_WARMUP_ENABLED_KEY, False),
        'warmup_start_date': _txt(WHATSAPP_WARMUP_START_DATE_KEY, '').strip(),
        'warmup_start_per_day': _int(WHATSAPP_WARMUP_START_PER_DAY_KEY, 20, min_value=1, max_value=50000),
        'warmup_ramp_days': _int(WHATSAPP_WARMUP_RAMP_DAYS_KEY, 14, min_value=1, max_value=120),
        # Global pace gate (#4)
        'pace_enabled': _bool(WHATSAPP_PACE_ENABLED_KEY, False),
        'pace_min_gap_seconds': _int(WHATSAPP_PACE_MIN_GAP_KEY, 8, min_value=0, max_value=600),
        'pace_jitter_seconds': _int(WHATSAPP_PACE_JITTER_KEY, 5, min_value=0, max_value=600),
        # Near-depletion scanner
        'depletion_enabled': _bool(WHATSAPP_DEPLETION_ENABLED_KEY, False),
        'depletion_expiry_days': _int(WHATSAPP_DEPLETION_EXPIRY_DAYS_KEY, 3, min_value=0, max_value=60),
        'depletion_volume_gb': _float(WHATSAPP_DEPLETION_VOLUME_GB_KEY, 2.0, min_value=0.0, max_value=1000.0),
        'depletion_cooldown_days': _int(WHATSAPP_DEPLETION_COOLDOWN_DAYS_KEY, 7, min_value=1, max_value=120),
        # Bot templates (per scenario)
        'bot_tpl_created': _txt(WHATSAPP_BOT_TPL_CREATED_KEY, DEFAULT_WHATSAPP_BOT_TPL_CREATED).strip() or DEFAULT_WHATSAPP_BOT_TPL_CREATED,
        'bot_tpl_renew': _txt(WHATSAPP_BOT_TPL_RENEW_KEY, DEFAULT_WHATSAPP_BOT_TPL_RENEW).strip() or DEFAULT_WHATSAPP_BOT_TPL_RENEW,
        'bot_tpl_ended': _txt(WHATSAPP_BOT_TPL_ENDED_KEY, DEFAULT_WHATSAPP_BOT_TPL_ENDED).strip() or DEFAULT_WHATSAPP_BOT_TPL_ENDED,
        'bot_tpl_info': _txt(WHATSAPP_BOT_TPL_INFO_KEY, DEFAULT_WHATSAPP_BOT_TPL_INFO).strip() or DEFAULT_WHATSAPP_BOT_TPL_INFO,
    }

    if region == 'iran':
        config['blocked_reason'] = 'deployment_in_iran'
    return config


def _whatsapp_effective_daily_cap(runtime_cfg: dict) -> int:
    """Daily send cap after applying warm-up. Returns the full daily_limit when
    warm-up is off or finished; otherwise a linearly interpolated cap based on
    days elapsed since the warm-up start date."""
    daily_limit = int(runtime_cfg.get('daily_limit') or 100)
    if not runtime_cfg.get('warmup_enabled'):
        return daily_limit
    start_raw = (runtime_cfg.get('warmup_start_date') or '').strip()
    start_per_day = int(runtime_cfg.get('warmup_start_per_day') or 20)
    ramp_days = max(1, int(runtime_cfg.get('warmup_ramp_days') or 14))
    # No/!invalid start date → treat today as day 0 (most conservative).
    try:
        start_date = datetime.strptime(start_raw, '%Y-%m-%d').date() if start_raw else datetime.utcnow().date()
    except ValueError:
        start_date = datetime.utcnow().date()
    days_elapsed = (datetime.utcnow().date() - start_date).days
    if days_elapsed >= ramp_days:
        return daily_limit
    if days_elapsed <= 0:
        return max(1, min(start_per_day, daily_limit))
    # Linear interpolation from start_per_day (day 0) to daily_limit (day ramp_days).
    span = daily_limit - start_per_day
    cap = start_per_day + (span * days_elapsed / ramp_days)
    return max(1, min(daily_limit, int(round(cap))))


def _whatsapp_render_bot_template(event_name: str, vars_dict: dict, runtime_cfg: dict | None = None) -> str:
    """Render the dedicated WhatsApp 'bot' template for a scenario.

    Returns '' when no bot template is configured for the event, so callers can
    fall back to the generic copy text. Events map to the four scenarios:
    created / renew / ended / info.
    """
    from app import _render_text_template  # deferred: app-level helper, avoids circular import
    cfg = runtime_cfg or _get_whatsapp_runtime_settings()
    mapping = {
        'created': cfg.get('bot_tpl_created'),
        'client_created': cfg.get('bot_tpl_created'),
        'renew': cfg.get('bot_tpl_renew'),
        'renew_success': cfg.get('bot_tpl_renew'),
        'ended': cfg.get('bot_tpl_ended'),
        'expired': cfg.get('bot_tpl_ended'),
        'depletion': cfg.get('bot_tpl_ended'),
        'info': cfg.get('bot_tpl_info'),
    }
    tpl = (mapping.get(event_name) or '').strip()
    if not tpl:
        return ''
    try:
        return _render_text_template(tpl, vars_dict or {})
    except Exception:
        return ''


def _public_base_url() -> str:
    """Absolute base URL of the panel, derived from the configured domain.
    Used to build dashboard links from background workers (no request context)."""
    from panel.services.subscription import get_public_base_url
    return get_public_base_url()


def _whatsapp_blocked_account_keys() -> set:
    """Return the set of (server_id, email_lower) accounts that belong to a
    reseller WITHOUT WhatsApp automation permission. These must never be
    messaged from the system number by any automated send. Accounts not owned
    by a reseller (owner/admin/superadmin) are never in this set."""
    try:
        disabled = {a.id for a in Admin.query.filter_by(role='reseller').all()
                    if not a.whatsapp_automation_enabled}
        if not disabled:
            return set()
        keys = set()
        for own in ClientOwnership.query.filter(ClientOwnership.reseller_id.in_(disabled)).all():
            email_l = (own.client_email or '').strip().lower()
            if email_l:
                keys.add((own.server_id, email_l))
        return keys
    except Exception:
        return set()


def _whatsapp_automation_allowed_for_account(server_id, email) -> bool:
    """A reseller-owned account is eligible for automated WhatsApp sends only
    when its owner reseller has whatsapp_automation_enabled. Accounts not owned
    by any reseller are always eligible (owner/admin/superadmin)."""
    try:
        email_l = (email or '').strip().lower()
        if not email_l:
            return True
        try:
            sid_norm = int(server_id)
        except (TypeError, ValueError):
            sid_norm = server_id
        own = ClientOwnership.query.filter(
            ClientOwnership.server_id == sid_norm,
            db.func.lower(ClientOwnership.client_email) == email_l,
        ).first()
        if not own:
            return True
        reseller = db.session.get(Admin, own.reseller_id)
        if not reseller or reseller.role != 'reseller':
            return True
        return bool(reseller.whatsapp_automation_enabled)
    except Exception:
        return True


def _run_whatsapp_depletion_scan() -> dict:
    """Scan cached clients and proactively message accounts whose time or volume
    is about to run out, once per cooldown window. Honors enable/region/warm-up/
    rate-limit via _send_whatsapp_message; stops early when the daily cap or pace
    gate is hit so it never bursts."""
    from app import format_remaining_days  # deferred: app-level helper, avoids circular import
    cfg = _get_whatsapp_runtime_settings()
    if cfg.get('deployment_region') == 'iran' or not cfg.get('enabled') or not cfg.get('depletion_enabled'):
        return {'scanned': 0, 'sent': 0, 'reason': 'disabled'}

    expiry_days = int(cfg.get('depletion_expiry_days') or 3)
    volume_gb_thr = float(cfg.get('depletion_volume_gb') or 2.0)
    cooldown_days = int(cfg.get('depletion_cooldown_days') or 7)
    base_url = _public_base_url()
    cooldown_cut = datetime.utcnow() - timedelta(days=cooldown_days)

    inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    scanned = 0
    sent = 0
    seen = set()
    # Reseller-owned accounts whose owner has not enabled WhatsApp automation
    # must be skipped — we never message them from the system number.
    blocked_keys = _whatsapp_blocked_account_keys()

    for inbound in inbounds:
        sid = inbound.get('server_id')
        try:
            sid_norm = int(sid)
        except (TypeError, ValueError):
            sid_norm = None
        for client in (inbound.get('clients') or []):
            email = (client.get('email') or '').strip()
            email_l = email.lower()
            if not email_l:
                continue
            dedupe = (sid_norm, email_l)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            if (sid_norm, email_l) in blocked_keys:
                continue
            scanned += 1

            # Operator opt-out: comment tagged #nopm ⇒ no WhatsApp private message.
            if _comment_opted_out(client.get('comment'), 'nopm'):
                continue

            recipient = _extract_iran_mobile_from_text(email, client.get('comment') or '')
            if not recipient:
                continue

            total_bytes = int(client.get('totalGB') or 0)
            try:
                used = int(client.get('up') or 0) + int(client.get('down') or 0)
            except Exception:
                used = 0
            rem_bytes = client.get('remaining_bytes')
            if rem_bytes is None or rem_bytes == -1:
                rem_bytes = max(total_bytes - used, 0) if total_bytes > 0 else None
            rem_gb = (float(rem_bytes) / (1024 ** 3)) if rem_bytes is not None else None

            expiry_ts = int(client.get('expiryTimestamp') or 0)
            exp = format_remaining_days(expiry_ts)
            days_left = int(exp.get('days') or 0)
            near_time = bool(expiry_ts and exp.get('type') in ('today', 'soon') and days_left <= expiry_days)
            near_vol = bool(rem_gb is not None and 0 < rem_gb <= volume_gb_thr)
            if not (near_time or near_vol):
                continue

            try:
                # Event-agnostic so the WhatsApp cooldown is shared with the SMS
                # automation: a user texted via SMS won't also get a WhatsApp ping
                # inside the window (and vice-versa).
                recent = WhatsappBotLog.query.filter(
                    WhatsappBotLog.email == email_l,
                    WhatsappBotLog.server_id == (sid_norm or 0),
                    WhatsappBotLog.sent_at >= cooldown_cut,
                ).first()
            except Exception:
                recent = None
            if recent:
                continue

            final_id = client.get('subId') or client.get('id') or ''
            dash = (client.get('dash_sub_url') or '').strip()
            if dash and not dash.startswith('http') and base_url:
                dash = base_url + (dash if dash.startswith('/') else f"/{dash}")
            elif not dash and base_url and final_id and sid_norm is not None:
                dash = f"{base_url}/s/{sid_norm}/{final_id}"

            vars_dict = {
                'email': email, 'account_name': email, 'user': email,
                'remaining_time': exp.get('text') or '-',
                'remaining_volume': client.get('remaining_formatted') or '-',
                'dashboard_link': dash, 'sub_link': dash,
                'server_name': inbound.get('server_name') or '',
            }
            ended_template = cfg.get('bot_tpl_ended') or ''
            if _template_wants_recommendation(ended_template):
                vars_dict.update(_recommendation_template_vars(
                    sid_norm, final_id, email,
                    terminal=bool((rem_gb is not None and rem_gb <= 0) or
                                  (expiry_ts and expiry_ts <= int(time.time() * 1000))),
                ))
            text_msg = _whatsapp_render_bot_template('depletion', vars_dict, cfg)
            if not text_msg:
                continue

            res = _send_whatsapp_message('depletion', email, text_msg, recipient_comment=client.get('comment') or '')
            if res.get('sent'):
                try:
                    db.session.add(WhatsappBotLog(email=email_l, server_id=(sid_norm or 0), event='depletion'))
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                sent += 1
            else:
                reason = res.get('reason') or ''
                # Cap/pace hit → stop this cycle; the next scan will resume.
                if reason in ('daily_limit_reached', 'pace_gated'):
                    return {'scanned': scanned, 'sent': sent, 'stopped': reason}

    return {'scanned': scanned, 'sent': sent}


def whatsapp_bot_worker():
    """Background loop running the near-depletion scan periodically."""
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        try:
            with app.app_context():
                result = _run_whatsapp_depletion_scan()
                if result.get('sent'):
                    app.logger.info("[whatsapp-bot] depletion scan sent=%s scanned=%s", result.get('sent'), result.get('scanned'))
        except Exception as exc:
            try:
                app.logger.warning("[whatsapp-bot] scan error: %s", exc)
            except Exception:
                pass
        time.sleep(1800)  # every 30 minutes


# ─────────────────────────────────────────────────────────────────────────────
# Telegram customer notifications
# Near-depletion scan + renewal confirmations delivered through the interactive
# bot (reseller-owned bot when the account belongs to a reseller, else central).
# ─────────────────────────────────────────────────────────────────────────────
TG_DEPLETION_ENABLED_KEY = 'tg_depletion_enabled'
TG_DEPLETION_RECOMMEND_KEY = 'tg_depletion_recommend'
TG_TRIGGER_RENEW_KEY = 'tg_trigger_renew_success'
TG_DEPLETION_EXPIRY_DAYS_KEY = 'tg_depletion_expiry_days'
TG_DEPLETION_VOLUME_GB_KEY = 'tg_depletion_volume_gb'
TG_DEPLETION_COOLDOWN_DAYS_KEY = 'tg_depletion_cooldown_days'
TG_TPL_RENEW_KEY = 'tg_tpl_renew'
TG_TPL_NEAR_EXPIRY_KEY = 'tg_tpl_near_expiry'
TG_TPL_LOW_VOLUME_KEY = 'tg_tpl_low_volume'

DEFAULT_TG_TPL_NEAR_EXPIRY = (
    '⏳ سرویس «{account_name}» {remaining_time} دیگر منقضی می‌شود.\n'
    '{if_dashboard_link}برای تمدید: {dashboard_link}{/if_dashboard_link}'
    '{if_recommendation}\n💡 با توجه به مصرف شما، بسته «{recommended_package}» ({recommended_volume} گیگ / {recommended_days} روز — {recommended_price} تومان) پیشنهاد می‌شود.{/if_recommendation}')
DEFAULT_TG_TPL_RENEW = (
    'Service "{account_name}" was renewed successfully.\n'
    'New expiry: {date}\n'
    '{if_dashboard_link}{dashboard_link}{/if_dashboard_link}')
DEFAULT_TG_TPL_LOW_VOLUME = (
    '📉 حجم سرویس «{account_name}» رو به اتمام است (باقی‌مانده: {remaining_volume}).\n'
    '{if_dashboard_link}برای تمدید: {dashboard_link}{/if_dashboard_link}'
    '{if_recommendation}\n💡 با توجه به مصرف شما، بسته «{recommended_package}» ({recommended_volume} گیگ / {recommended_days} روز — {recommended_price} تومان) پیشنهاد می‌شود.{/if_recommendation}')


def _get_telegram_depletion_settings() -> dict:
    from app import _get_system_configs_batch, _parse_bool  # deferred: app-level helper, avoids circular import
    keys = [
        TG_DEPLETION_ENABLED_KEY, TG_DEPLETION_RECOMMEND_KEY, TG_TRIGGER_RENEW_KEY,
        TG_DEPLETION_EXPIRY_DAYS_KEY,
        TG_DEPLETION_VOLUME_GB_KEY, TG_DEPLETION_COOLDOWN_DAYS_KEY,
        TG_TPL_RENEW_KEY, TG_TPL_NEAR_EXPIRY_KEY, TG_TPL_LOW_VOLUME_KEY,
    ]
    c = _get_system_configs_batch(keys)
    wa = _get_whatsapp_runtime_settings()

    def _txt(k, d=''):
        v = c.get(k)
        return str(v) if v is not None else d

    def _bool(k, d=False):
        return _parse_bool(_txt(k, 'true' if d else 'false'))

    def _int(k, d, lo=None, hi=None):
        return _parse_int(_txt(k, str(d)), d, min_value=lo, max_value=hi)

    try:
        volume_gb = float(_txt(TG_DEPLETION_VOLUME_GB_KEY, ''))
    except (TypeError, ValueError):
        volume_gb = float(wa.get('depletion_volume_gb') or 2.0)
    return {
        'enabled': _bool(TG_DEPLETION_ENABLED_KEY, False),
        'recommend': _bool(TG_DEPLETION_RECOMMEND_KEY, False),
        'trigger_renew_success': _bool(TG_TRIGGER_RENEW_KEY, True),
        # Thresholds fall back to the WhatsApp/SMS depletion thresholds so one
        # operator-tuned window drives every notification channel.
        'expiry_days': _int(TG_DEPLETION_EXPIRY_DAYS_KEY,
                            int(wa.get('depletion_expiry_days') or 3), lo=0, hi=60),
        'volume_gb': max(0.0, volume_gb),
        'cooldown_days': _int(TG_DEPLETION_COOLDOWN_DAYS_KEY,
                              int(wa.get('depletion_cooldown_days') or 7), lo=1, hi=120),
        'tpl_renew': _txt(TG_TPL_RENEW_KEY, '').strip() or DEFAULT_TG_TPL_RENEW,
        'tpl_near_expiry': _txt(TG_TPL_NEAR_EXPIRY_KEY, '').strip() or DEFAULT_TG_TPL_NEAR_EXPIRY,
        'tpl_low_volume': _txt(TG_TPL_LOW_VOLUME_KEY, '').strip() or DEFAULT_TG_TPL_LOW_VOLUME,
    }


def _notification_bot_for_reseller(reseller_id):
    """Active, non-archived bot owned by this reseller; else the central bot."""
    bot = None
    if reseller_id:
        bot = TelegramBotInstance.query.filter(
            TelegramBotInstance.owner_type == 'reseller',
            TelegramBotInstance.owner_admin_id == int(reseller_id),
            TelegramBotInstance.enabled.is_(True),
            TelegramBotInstance.archived_at.is_(None),
            TelegramBotInstance.token_encrypted.isnot(None),
        ).order_by(TelegramBotInstance.id.asc()).first()
    if bot is None:
        bot = TelegramBotInstance.query.filter(
            TelegramBotInstance.scope_key == 'system',
            TelegramBotInstance.enabled.is_(True),
            TelegramBotInstance.archived_at.is_(None),
            TelegramBotInstance.token_encrypted.isnot(None),
        ).first()
    return bot


def _notification_bot_for_account(server_id, email):
    """(bot, ownership) pair used to reach the owner of a panel account."""
    try:
        sid_norm = int(server_id)
    except (TypeError, ValueError):
        sid_norm = None
    email_l = (email or '').strip().lower()
    ownership = None
    if sid_norm is not None and email_l:
        ownership = ServiceOwnership.query.filter(
            ServiceOwnership.server_id == sid_norm,
            db.func.lower(ServiceOwnership.client_email_snapshot) == email_l,
            ServiceOwnership.revoked_at.is_(None),
        ).first()
    bot = _notification_bot_for_reseller(ownership.reseller_id if ownership else None)
    return bot, ownership


def _notify_customer_telegram(customer_id, text, bot=None) -> None:
    """Send a Telegram message to a customer's linked identity in a background
    thread. No-op without a linked chat or a usable bot."""
    from app import _telegram_bot_api_client, app  # deferred: app-level helper, avoids circular import
    if not customer_id or not (text or '').strip():
        return

    def _worker():
        with app.app_context():
            try:
                identity = TelegramIdentity.query.filter_by(customer_id=int(customer_id)).first()
                chat_id = int(getattr(identity, 'telegram_chat_id', 0) or 0) if identity else 0
                if not chat_id:
                    return
                target = bot or _notification_bot_for_reseller(None)
                if target is None:
                    return
                _telegram_bot_api_client(target).send_message(chat_id, text)
            except Exception as exc:
                try:
                    db.session.rollback()
                    app.logger.warning("[telegram-notify] customer %s: %s", customer_id, exc)
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()


def _depletion_renew_reply_markup(bot, ownership, package_id, package_name):
    """Inline 'quick renew' button for a depletion reminder, or None.

    The callback jumps into the existing renewal card-payment flow
    (``renew-pay-card:<ownership_id>:<package_id>`` in telegram_bot_worker).
    The button is only attached when the package is actually renewable for this
    ownership; the worker re-validates regardless, so any failure here degrades
    to no button rather than a dead one."""
    try:
        package_id = int(package_id)
        ownership_id = int(ownership.id)
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        from telegram_bot_worker import _available_packages
        allowed_ids = {int(p.id) for p in _available_packages(ownership)}
        if package_id not in allowed_ids:
            return None
    except Exception:
        # Availability check unavailable; the worker-side handler re-validates
        # ownership and package availability before starting the payment flow.
        pass
    try:
        from telegram_bot_runtime import COPY as _TG_COPY, resolve_copy
        copy = resolve_copy(bot)
        lang = str(getattr(bot, 'default_language', 'fa') or 'fa')
        if lang not in copy:
            lang = 'fa'
        label = str(copy[lang].get('depletion_renew_button')
                    or _TG_COPY['fa']['depletion_renew_button'])
        try:
            label = label.format(package=package_name or '')
        except Exception:
            label = _TG_COPY['fa']['depletion_renew_button'].format(package=package_name or '')
        return {'inline_keyboard': [[{
            'text': label,
            'callback_data': f'renew-pay-card:{ownership_id}:{package_id}',
        }]]}
    except Exception:
        return None


def _run_telegram_depletion_scan() -> dict:
    """Telegram twin of _run_whatsapp_depletion_scan: message bot-linked
    customers whose service is near expiry or low on volume, once per event
    per cooldown window (shared WhatsappBotLog with tg_* events)."""
    from app import _render_text_template, _telegram_bot_api_client, app, format_remaining_days  # deferred: app-level helper, avoids circular import
    cfg = _get_telegram_depletion_settings()
    if not cfg.get('enabled'):
        return {'scanned': 0, 'sent': 0, 'reason': 'disabled'}

    expiry_days = int(cfg.get('expiry_days') or 3)
    volume_gb_thr = float(cfg.get('volume_gb') or 2.0)
    cooldown_cut = datetime.utcnow() - timedelta(days=int(cfg.get('cooldown_days') or 7))
    base_url = _public_base_url()

    inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    scanned = 0
    sent = 0
    seen = set()
    api_by_bot = {}

    for inbound in inbounds:
        sid = inbound.get('server_id')
        try:
            sid_norm = int(sid)
        except (TypeError, ValueError):
            sid_norm = None
        for client in (inbound.get('clients') or []):
            email = (client.get('email') or '').strip()
            email_l = email.lower()
            if not email_l:
                continue
            dedupe = (sid_norm, email_l)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            scanned += 1

            if sid_norm is None:
                continue
            ownership = ServiceOwnership.query.filter(
                ServiceOwnership.server_id == sid_norm,
                db.func.lower(ServiceOwnership.client_email_snapshot) == email_l,
                ServiceOwnership.revoked_at.is_(None),
            ).first()
            if not ownership:
                continue
            identity = TelegramIdentity.query.filter_by(
                customer_id=ownership.customer_id).first()
            chat_id = int(getattr(identity, 'telegram_chat_id', 0) or 0) if identity else 0
            if not chat_id:
                continue
            bot = _notification_bot_for_reseller(ownership.reseller_id)
            if bot is None:
                continue

            total_bytes = int(client.get('totalGB') or 0)
            try:
                used = int(client.get('up') or 0) + int(client.get('down') or 0)
            except Exception:
                used = 0
            rem_bytes = client.get('remaining_bytes')
            if rem_bytes is None or rem_bytes == -1:
                rem_bytes = max(total_bytes - used, 0) if total_bytes > 0 else None
            rem_gb = (float(rem_bytes) / (1024 ** 3)) if rem_bytes is not None else None

            expiry_ts = int(client.get('expiryTimestamp') or 0)
            exp = format_remaining_days(expiry_ts)
            days_left = int(exp.get('days') or 0)
            near_time = bool(expiry_ts and exp.get('type') in ('today', 'soon') and days_left <= expiry_days)
            near_vol = bool(rem_gb is not None and 0 < rem_gb <= volume_gb_thr)
            if not (near_time or near_vol):
                continue
            event = 'tg_near_expiry' if near_time else 'tg_low_volume'

            try:
                recent = WhatsappBotLog.query.filter(
                    WhatsappBotLog.email == email_l,
                    WhatsappBotLog.server_id == (sid_norm or 0),
                    WhatsappBotLog.event == event,
                    WhatsappBotLog.sent_at >= cooldown_cut,
                ).first()
            except Exception:
                recent = None
            if recent:
                continue

            final_id = client.get('subId') or client.get('id') or ''
            dash = (client.get('dash_sub_url') or '').strip()
            if dash and not dash.startswith('http') and base_url:
                dash = base_url + (dash if dash.startswith('/') else f"/{dash}")
            elif not dash and base_url and final_id and sid_norm is not None:
                dash = f"{base_url}/s/{sid_norm}/{final_id}"

            vars_dict = {
                'email': email, 'account_name': email, 'user': email,
                'remaining_time': exp.get('text') or '-',
                'remaining_volume': client.get('remaining_formatted') or '-',
                'days_left': days_left,
                'dashboard_link': dash, 'sub_link': dash,
                'server_name': inbound.get('server_name') or '',
            }
            reply_markup = None
            if cfg.get('recommend'):
                rec_vars = _recommendation_template_vars(
                    sid_norm, final_id, email,
                    terminal=bool((rem_gb is not None and rem_gb <= 0) or
                                  (expiry_ts and expiry_ts <= int(time.time() * 1000))),
                )
                vars_dict.update(rec_vars)
                if rec_vars.get('recommended_package_id'):
                    reply_markup = _depletion_renew_reply_markup(
                        bot, ownership, rec_vars['recommended_package_id'],
                        rec_vars.get('recommended_package'))
            template = cfg['tpl_near_expiry'] if near_time else cfg['tpl_low_volume']
            try:
                text_msg = _render_text_template(template, vars_dict)
            except Exception:
                continue
            if not (text_msg or '').strip():
                continue

            try:
                api = api_by_bot.get(bot.id)
                if api is None:
                    api = _telegram_bot_api_client(bot)
                    api_by_bot[bot.id] = api
                if reply_markup:
                    api.send_message(chat_id, text_msg, reply_markup=reply_markup)
                else:
                    api.send_message(chat_id, text_msg)
            except Exception as exc:
                db.session.rollback()
                app.logger.warning("[telegram-bot] depletion send failed for bot %s: %s", bot.id, exc)
                continue
            try:
                db.session.add(WhatsappBotLog(
                    email=email_l, server_id=(sid_norm or 0), event=event))
                db.session.commit()
            except Exception:
                db.session.rollback()
            sent += 1

    return {'scanned': scanned, 'sent': sent}


def telegram_depletion_worker():
    """Background loop running the Telegram near-depletion scan periodically."""
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        try:
            with app.app_context():
                result = _run_telegram_depletion_scan()
                if result.get('sent'):
                    app.logger.info("[telegram-bot] depletion scan sent=%s scanned=%s", result.get('sent'), result.get('scanned'))
        except Exception as exc:
            try:
                app.logger.warning("[telegram-bot] scan error: %s", exc)
            except Exception:
                pass
        time.sleep(1800)  # every 30 minutes


def _run_telegram_announcement_batch(batch_size=25):
    """Send a small resumable batch; delivery rows make retries idempotent."""
    from app import _telegram_bot_api_client  # deferred: app-level helper, avoids circular import
    now = datetime.utcnow()
    campaigns = TelegramAnnouncement.query.filter(
        TelegramAnnouncement.status.in_(('queued', 'sending')),
    ).order_by(TelegramAnnouncement.id.asc()).limit(5).all()
    processed = 0
    api_by_bot = {}
    for campaign in campaigns:
        campaign.status = 'sending'
        deliveries = TelegramAnnouncementDelivery.query.filter(
            TelegramAnnouncementDelivery.announcement_id == campaign.id,
            TelegramAnnouncementDelivery.status.in_(('pending', 'retry')),
            db.or_(TelegramAnnouncementDelivery.next_attempt_at.is_(None),
                   TelegramAnnouncementDelivery.next_attempt_at <= now),
        ).order_by(TelegramAnnouncementDelivery.id.asc()).limit(max(1, batch_size - processed)).all()
        for delivery in deliveries:
            bot = db.session.get(TelegramBotInstance, delivery.bot_instance_id)
            if not bot or not bot.enabled or bot.archived_at is not None:
                delivery.status, delivery.last_error = 'failed', 'Bot is unavailable'
                continue
            try:
                api = api_by_bot.get(bot.id)
                if api is None:
                    api = _telegram_bot_api_client(bot)
                    api_by_bot[bot.id] = api
                api.send_message(delivery.chat_id, campaign.message_text,
                                 disable_web_page_preview=True)
                delivery.status, delivery.sent_at, delivery.last_error = 'sent', datetime.utcnow(), None
            except Exception as exc:
                safe_error = redact_connection_error(exc)
                delivery.attempts = int(delivery.attempts or 0) + 1
                lower_error = str(safe_error).lower()
                if any(term in lower_error for term in ('blocked', 'chat not found', 'user is deactivated', 'forbidden')):
                    delivery.status = 'blocked'
                elif delivery.attempts >= 5:
                    delivery.status = 'failed'
                else:
                    delivery.status = 'retry'
                    retry_after = max(5, int(getattr(exc, 'retry_after', 0) or 0))
                    delivery.next_attempt_at = datetime.utcnow() + timedelta(seconds=retry_after)
                delivery.last_error = str(safe_error)[:500]
            processed += 1
            if processed >= batch_size:
                break
        counts = dict(db.session.query(
            TelegramAnnouncementDelivery.status, db.func.count(TelegramAnnouncementDelivery.id),
        ).filter_by(announcement_id=campaign.id).group_by(TelegramAnnouncementDelivery.status).all())
        campaign.sent_count = int(counts.get('sent', 0))
        campaign.failed_count = int(counts.get('failed', 0))
        campaign.blocked_count = int(counts.get('blocked', 0))
        if not any(counts.get(value, 0) for value in ('pending', 'retry')):
            campaign.status, campaign.finished_at = 'completed', datetime.utcnow()
        db.session.commit()
        if processed >= batch_size:
            break
    return processed


def telegram_announcement_worker():
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        try:
            with app.app_context():
                processed = _run_telegram_announcement_batch()
                processed += _run_announcement_campaign_batch()
        except Exception as exc:
            processed = 0
            try:
                db.session.rollback()
                app.logger.warning('[telegram-announcement] worker error: %s', exc)
            except Exception:
                pass
        time.sleep(1 if processed else 10)


def _announcement_target_rules(raw):
    if raw in (None, '', '*'):
        return '*'
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        value = []
    rules = {}
    for item in value if isinstance(value, list) else []:
        try:
            sid = int((item or {}).get('server_id'))
        except (TypeError, ValueError, AttributeError):
            continue
        inbounds = (item or {}).get('inbounds', '*')
        if inbounds == '*':
            rules[sid] = '*'
        elif isinstance(inbounds, list):
            rules[sid] = {int(v) for v in inbounds if str(v).lstrip('-').isdigit()}
    return rules


def _announcement_target_allows(rules, server_id, inbound_id):
    if rules == '*':
        return True
    try:
        sid, iid = int(server_id), int(inbound_id)
    except (TypeError, ValueError):
        return False
    allowed = rules.get(sid)
    return allowed == '*' or (isinstance(allowed, set) and iid in allowed)


def _announcement_client_context(client, inbound):
    from app import format_remaining_days  # deferred: compatibility helper
    email = str(client.get('email') or '').strip()
    sid = inbound.get('server_id')
    iid = inbound.get('id') or inbound.get('inbound_id')
    expiry_ts = int(client.get('expiryTimestamp') or 0)
    expiry = format_remaining_days(expiry_ts)
    sub_id = client.get('subId') or client.get('id') or ''
    dash = str(client.get('dash_sub_url') or '').strip()
    base = _public_base_url()
    if dash and not dash.startswith('http') and base:
        dash = base + (dash if dash.startswith('/') else f'/{dash}')
    elif not dash and base and sub_id and sid is not None:
        dash = f'{base}/s/{sid}/{sub_id}'
    return {
        'email': email, 'account_name': email, 'service_name': email, 'user': email,
        'remaining_time': expiry.get('text') or '-',
        'remaining_volume': client.get('remaining_formatted') or '-',
        'dashboard_link': dash, 'sub_link': str(client.get('sub_url') or dash),
        'server_name': inbound.get('server_name') or '',
        'server_id': sid, 'inbound_id': iid,
        'comment': client.get('comment') or '',
    }


ANNOUNCEMENT_OWNER_TYPES = ('system', 'reseller', 'unowned')
ANNOUNCEMENT_STATUS_GROUPS = (
    'other', 'expired', 'volume_ended', 'expiring_soon', 'volume_low',
)


def _announcement_filter_values(raw, allowed, default):
    if raw in (None, ''):
        return set(default)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = [part.strip() for part in raw.split(',')]
    values = {str(value or '').strip().lower() for value in (raw if isinstance(raw, list) else [raw])}
    return {value for value in values if value in allowed} or set(default)


def _announcement_account_owner_types(accounts):
    """Classify accounts without an ownership query per recipient.

    Reseller ownership wins when legacy data contains both a reseller and a
    system owner, preserving the previous safe default that excluded resellers.
    """
    account_keys = {
        (int(context['server_id']), str(context.get('email') or '').lower())
        for _, _, context in accounts if context.get('server_id') is not None
    }
    if not account_keys:
        return {}
    server_ids = sorted({key[0] for key in account_keys})
    roles_by_key = {}
    rows = (db.session.query(
        ClientOwnership.server_id, ClientOwnership.client_email, Admin.role,
    ).join(Admin, Admin.id == ClientOwnership.reseller_id)
      .filter(ClientOwnership.server_id.in_(server_ids)).all())
    for server_id, email, role in rows:
        key = (int(server_id), str(email or '').strip().lower())
        if key in account_keys:
            roles_by_key.setdefault(key, set()).add(str(role or '').strip().lower())
    result = {}
    for key in account_keys:
        roles = roles_by_key.get(key, set())
        result[key] = 'reseller' if 'reseller' in roles else ('system' if roles else 'unowned')
    return result


def _announcement_client_status_group(client):
    state = str(client.get('service_state') or '').strip().lower()
    # ``inactive`` is intentionally not an audience option. A manually
    # disabled account must never fall through to the selectable ``other``
    # bucket (shown as "Active / other" in the campaign form).
    if state == 'inactive':
        return 'inactive'
    if state in ANNOUNCEMENT_STATUS_GROUPS[1:]:
        return state
    volume_status = str(client.get('volume_status') or '').strip().lower()
    if volume_status in ('suspended', 'ended', 'volume_ended'):
        return 'volume_ended'
    if volume_status in ('low', 'volume_low'):
        return 'volume_low'
    expiry_type = str(client.get('expiryType') or '').strip().lower()
    if expiry_type == 'expired':
        return 'expired'
    try:
        expiry_ts = int(client.get('expiryTimestamp') or 0)
    except (TypeError, ValueError):
        expiry_ts = 0
    if expiry_ts > 0 and expiry_ts <= int(time.time() * 1000):
        return 'expired'
    return 'other'


def _announcement_campaign_recipients(
        channel, targets='*', owner_types=None, statuses=None):
    """Resolve a server/inbound audience and deduplicate by actual destination."""
    from app import _telegram_announcement_recipients  # deferred: app helper
    channel = str(channel or '').strip().lower()
    if channel not in ('sms', 'whatsapp', 'telegram'):
        raise ValueError('Outbound channel must be SMS, WhatsApp, or Telegram')
    try:
        load_snapshot_from_redis()
    except Exception:
        pass
    rules = _announcement_target_rules(targets)
    accounts = []
    seen_accounts = set()
    stats = {
        'clients': 0, 'unique': 0, 'duplicates': 0, 'missing_contact': 0,
        'opted_out': 0, 'blocked_by_policy': 0, 'owner_filtered': 0,
        'status_filtered': 0,
    }
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        sid = inbound.get('server_id')
        iid = inbound.get('id') or inbound.get('inbound_id')
        if not _announcement_target_allows(rules, sid, iid):
            continue
        for client in (inbound.get('clients') or []):
            email = str(client.get('email') or '').strip()
            try:
                key = (int(sid), email.lower())
            except (TypeError, ValueError):
                continue
            if not email or key in seen_accounts:
                continue
            seen_accounts.add(key)
            stats['clients'] += 1
            accounts.append((client, inbound, _announcement_client_context(client, inbound)))

    recipients = []
    seen_destinations = set()
    if channel in ('sms', 'whatsapp'):
        selected_owners = _announcement_filter_values(
            owner_types, ANNOUNCEMENT_OWNER_TYPES, ('system', 'unowned'))
        selected_statuses = _announcement_filter_values(
            statuses, ANNOUNCEMENT_STATUS_GROUPS, ANNOUNCEMENT_STATUS_GROUPS)
        owners_by_account = _announcement_account_owner_types(accounts) if channel == 'sms' else {}
        for client, inbound, context in accounts:
            comment = context.get('comment') or ''
            if channel == 'sms':
                account_key = (int(context['server_id']), str(context.get('email') or '').lower())
                owner_type = owners_by_account.get(account_key, 'unowned')
                context['owner_type'] = owner_type
                if owner_type not in selected_owners:
                    stats['owner_filtered'] += 1
                    if owner_type == 'reseller':
                        stats['blocked_by_policy'] += 1
                    continue
                status_group = _announcement_client_status_group(client)
                context['service_state'] = status_group
                if status_group not in selected_statuses:
                    stats['status_filtered'] += 1
                    continue
            elif not _whatsapp_automation_allowed_for_account(
                    context.get('server_id'), context.get('email')):
                stats['blocked_by_policy'] += 1
                continue
            if (channel == 'sms' and _sms_comment_opted_out(comment)) or (
                    channel == 'whatsapp' and _comment_opted_out(comment, 'nopm')):
                stats['opted_out'] += 1
                continue
            phone = _extract_iran_mobile_from_text(context.get('email'), comment)
            if not phone:
                stats['missing_contact'] += 1
                continue
            if phone in seen_destinations:
                stats['duplicates'] += 1
                continue
            seen_destinations.add(phone)
            recipients.append({
                'recipient_key': f'{channel}:{phone}', 'recipient': phone,
                'email': context.get('email'), 'server_id': context.get('server_id'),
                'inbound_id': context.get('inbound_id'), 'context': context,
            })
    else:
        contexts_by_customer = {}
        if accounts:
            keys = {(int(ctx['server_id']), str(ctx['email']).lower()): ctx for _, _, ctx in accounts}
            server_ids = sorted({key[0] for key in keys})
            ownerships = ServiceOwnership.query.filter(
                ServiceOwnership.server_id.in_(server_ids),
                ServiceOwnership.revoked_at.is_(None),
            ).all()
            for ownership in ownerships:
                key = (int(ownership.server_id), str(ownership.client_email_snapshot or '').lower())
                if key in keys and ownership.customer_id not in contexts_by_customer:
                    contexts_by_customer[int(ownership.customer_id)] = keys[key]
        tg_filters = {'bot_scope': 'all', 'linked_only': True, 'server_ids': []}
        for item in _telegram_announcement_recipients(tg_filters):
            customer_id = item.get('customer_id')
            context = contexts_by_customer.get(int(customer_id)) if customer_id else None
            if not context:
                continue
            user_key = int(item['telegram_user_id'])
            if user_key in seen_destinations:
                stats['duplicates'] += 1
                continue
            seen_destinations.add(user_key)
            bot_id, chat_id = int(item['bot_instance_id']), int(item['chat_id'])
            recipients.append({
                'recipient_key': f'telegram:{user_key}',
                'recipient': str(chat_id), 'bot_instance_id': bot_id,
                'email': context.get('email'), 'server_id': context.get('server_id'),
                'inbound_id': context.get('inbound_id'), 'context': context,
            })
        stats['missing_contact'] = max(0, len(contexts_by_customer) - len(recipients))
    stats['unique'] = len(recipients)
    return recipients, stats


def _announcement_delivery_segment_count(channel, message, context):
    if channel != 'sms':
        return 1
    from app import _render_text_template  # deferred compatibility helper
    rendered = _render_text_template(message or '', context or {})
    return max(1, int(_sms_segment_info(rendered).get('sms_segments') or 1))


def _recount_announcement_campaign(announcement):
    counts = dict(db.session.query(
        AnnouncementDelivery.status, db.func.count(AnnouncementDelivery.id),
    ).filter_by(announcement_id=announcement.id).group_by(AnnouncementDelivery.status).all())
    announcement.total_count = int(sum(counts.values()))
    announcement.sent_count = int(counts.get('sent', 0))
    announcement.failed_count = int(
        counts.get('failed', 0) + counts.get('manual_review', 0))
    announcement.skipped_count = int(counts.get('skipped', 0))
    return counts


def _reconcile_announcement_campaign_deliveries(announcement):
    """Apply edited targeting to unsent rows while never recreating sent rows."""
    recipients, stats = _announcement_campaign_recipients(
        announcement.channel, announcement.targets,
        announcement.audience_owner_types, announcement.audience_statuses,
    )
    candidates = {item['recipient_key']: item for item in recipients}
    existing = AnnouncementDelivery.query.filter_by(announcement_id=announcement.id).all()
    existing_by_key = {delivery.recipient_key: delivery for delivery in existing}
    now = datetime.utcnow()

    for delivery in existing:
        item = candidates.get(delivery.recipient_key)
        can_reconcile = delivery.status in ('pending', 'retry') or (
            delivery.status == 'skipped' and delivery.last_error == 'audience_changed')
        if not can_reconcile:
            continue
        if item is None:
            delivery.status = 'skipped'
            delivery.last_error = 'audience_changed'
            delivery.processed_at = now
            delivery.next_attempt_at = None
            continue
        context = item['context']
        delivery.recipient = item['recipient']
        delivery.email = item.get('email')
        delivery.server_id = item.get('server_id')
        delivery.inbound_id = item.get('inbound_id')
        delivery.bot_instance_id = item.get('bot_instance_id')
        delivery.context_json = json.dumps(context, ensure_ascii=False)
        delivery.segment_count = _announcement_delivery_segment_count(
            announcement.channel, announcement.message, context)
        if delivery.status == 'skipped':
            delivery.status = 'pending'
            delivery.last_error = None
            delivery.processed_at = None
        candidates.pop(delivery.recipient_key, None)

    for recipient_key, item in candidates.items():
        # Terminal rows, especially sent rows, are intentionally never revived.
        if recipient_key in existing_by_key:
            continue
        context = item.pop('context')
        db.session.add(AnnouncementDelivery(
            announcement_id=announcement.id,
            context_json=json.dumps(context, ensure_ascii=False),
            segment_count=_announcement_delivery_segment_count(
                announcement.channel, announcement.message, context),
            **item,
        ))
    db.session.flush()
    return _recount_announcement_campaign(announcement), stats


def _estimate_sms_campaign_duration(
        recipient_count, estimated_segments, delivery_mode='all', daily_limit=None, cfg=None,
        announcement_daily_limit=None):
    """Return a conservative ETA based on current SMS pace, caps and quiet hours."""
    count = max(0, int(recipient_count or 0))
    segments = max(0, int(estimated_segments or 0))
    if count <= 0:
        return {'seconds': 0, 'label': '0 minutes', 'recipients_per_day': 0,
                'bottleneck': 'none'}
    cfg = cfg or _get_sms_runtime_settings()
    avg_segments = max(1.0, segments / count) if segments else 1.0
    if announcement_daily_limit is not None:
        global_daily_segments = max(1, int(announcement_daily_limit))
    else:
        global_daily_segments = max(1, int(cfg.get('daily_limit') or 1))
    by_segments = max(1, int(global_daily_segments // avg_segments))
    pace = max(0.0, float(cfg.get('send_pace_seconds') or 0))
    active_seconds = 86400
    if cfg.get('quiet_enabled'):
        start, end = int(cfg.get('quiet_start') or 0), int(cfg.get('quiet_end') or 0)
        quiet_hours = (end - start) % 24
        active_seconds = max(3600, 86400 - quiet_hours * 3600)
    by_pace = max(1, int(active_seconds // max(pace, 0.01)))
    capacities = {'SMS daily segment limit': by_segments, 'send pace': by_pace}
    if delivery_mode == 'daily' and int(daily_limit or 0) > 0:
        capacities['campaign daily limit'] = int(daily_limit)
    bottleneck, per_day = min(capacities.items(), key=lambda item: item[1])
    full_days = (count - 1) // per_day
    last_day_count = count - (full_days * per_day)
    seconds = int(full_days * 86400 + max(1, math.ceil(last_day_count * max(pace, 0.01))))
    if seconds < 3600:
        label = f'about {max(1, math.ceil(seconds / 60))} minutes'
    elif seconds < 86400:
        label = f'about {math.ceil(seconds / 3600)} hours'
    else:
        label = f'about {math.ceil(seconds / 86400)} days'
    return {
        'seconds': seconds, 'label': label, 'recipients_per_day': per_day,
        'bottleneck': bottleneck, 'average_segments': round(avg_segments, 2),
    }


def _announcement_campaign_eta(announcement):
    if announcement.channel != 'sms' or announcement.status == 'draft':
        return None
    remaining_count = max(0, int(announcement.total_count or 0) - int(announcement.sent_count or 0)
                          - int(announcement.failed_count or 0) - int(announcement.skipped_count or 0))
    remaining_segments = int(db.session.query(
        db.func.coalesce(db.func.sum(AnnouncementDelivery.segment_count), 0)
    ).filter(
        AnnouncementDelivery.announcement_id == announcement.id,
        AnnouncementDelivery.status.in_(('pending', 'retry')),
    ).scalar() or 0)
    sms_cfg = _get_sms_runtime_settings()
    ann_daily_limit = int(sms_cfg.get('announcement_daily_limit') or 500)
    estimate = _estimate_sms_campaign_duration(
        remaining_count, remaining_segments, announcement.delivery_mode,
        announcement.daily_limit, cfg=sms_cfg,
        announcement_daily_limit=ann_daily_limit,
    )
    estimate['remaining_segments'] = remaining_segments
    if estimate['seconds']:
        estimate['estimated_finish_at'] = (
            datetime.utcnow() + timedelta(seconds=estimate['seconds'])).isoformat()
    return estimate


def _announcement_campaign_estimate(
        channel, message, targets='*', owner_types=None, statuses=None,
        delivery_mode='all', daily_limit=None):
    """Build the canonical recipient estimate returned by the API and saved on drafts."""
    from app import _render_text_template  # deferred: compatibility helper

    recipients, stats = _announcement_campaign_recipients(
        channel, targets, owner_types, statuses)
    result = {**stats}
    if str(channel or '').strip().lower() == 'sms':
        template = str(message or '')
        segment_counts = [
            int(_sms_segment_info(
                _render_text_template(template, item.get('context') or {})
            ).get('sms_segments') or 0)
            for item in recipients
        ]
        total_segments = sum(segment_counts)
        result['sms'] = {
            **_sms_segment_info(template),
            'estimated_total_segments': total_segments,
            'min_segments_per_recipient': min(segment_counts) if segment_counts else 0,
            'max_segments_per_recipient': max(segment_counts) if segment_counts else 0,
        }
        sms_cfg = _get_sms_runtime_settings()
        ann_daily_limit = int(sms_cfg.get('announcement_daily_limit') or 500)
        result['eta'] = _estimate_sms_campaign_duration(
            len(recipients), total_segments, delivery_mode, daily_limit,
            cfg=sms_cfg, announcement_daily_limit=ann_daily_limit)
    return result


def _queue_announcement_campaign(announcement):
    if announcement.channel == 'subscription':
        raise ValueError('Subscription announcements do not use the message queue')
    if announcement.status not in ('draft', 'paused'):
        raise ValueError('Only draft or paused campaigns can be queued')
    if announcement.channel == 'sms':
        cfg = _get_sms_runtime_settings()
        if not cfg.get('enabled') or not cfg.get('base_url') or not cfg.get('api_key'):
            raise ValueError('SMS automation and its selected gateway must be enabled first')
    elif announcement.channel == 'whatsapp':
        cfg = _get_whatsapp_runtime_settings()
        if not cfg.get('enabled') or not cfg.get('gateway_url'):
            raise ValueError('WhatsApp messaging must be enabled and connected first')
    if announcement.status == 'draft':
        recipients, _ = _announcement_campaign_recipients(
            announcement.channel, announcement.targets,
            announcement.audience_owner_types, announcement.audience_statuses)
        for item in recipients:
            context = item.pop('context')
            db.session.add(AnnouncementDelivery(
                announcement_id=announcement.id,
                context_json=json.dumps(context, ensure_ascii=False),
                segment_count=_announcement_delivery_segment_count(
                    announcement.channel, announcement.message, context),
                **item,
            ))
        announcement.total_count = len(recipients)
    announcement.status = 'queued'
    announcement.started_at = announcement.started_at or datetime.utcnow()
    announcement.finished_at = datetime.utcnow() if not announcement.total_count else None
    if not announcement.total_count:
        announcement.status = 'completed'


def _announcement_campaign_day_bounds():
    now_utc = datetime.now(timezone.utc)
    try:
        tehran_zone = ZoneInfo('Asia/Tehran') if ZoneInfo is not None else timezone(timedelta(hours=3, minutes=30))
    except Exception:
        tehran_zone = timezone(timedelta(hours=3, minutes=30))
    tehran = now_utc.astimezone(tehran_zone)
    start_local = tehran.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    return start_utc.replace(tzinfo=None), (start_utc + timedelta(days=1)).replace(tzinfo=None)


def _run_announcement_campaign_batch(batch_size=25):
    """Process outbound announcement campaigns while honoring channel and campaign caps."""
    from app import _render_text_template, _telegram_bot_api_client
    now = datetime.utcnow()
    campaigns = Announcement.query.filter(
        Announcement.channel.in_(('sms', 'whatsapp', 'telegram')),
        Announcement.status.in_(('queued', 'sending')),
    ).order_by(Announcement.id.asc()).limit(5).all()
    processed = 0
    api_by_bot = {}
    sms_capacity = None
    for campaign in campaigns:
        available = max(1, batch_size - processed)
        if campaign.channel == 'sms':
            if sms_capacity is None:
                sms_capacity = _get_gmweb_send_capacity(_get_sms_runtime_settings())
            if not sms_capacity.get('ok'):
                campaign.status = 'queued'
                db.session.commit()
                continue
            capacity_data = sms_capacity.get('announcement') or {}
            available = min(
                available,
                int(capacity_data.get('available') or 0),
                int(capacity_data.get('recommended_batch_size') or 1),
                50,
            )
            if available <= 0:
                campaign.status = 'queued'
                db.session.commit()
                continue
        campaign.status = 'sending'
        if campaign.delivery_mode == 'daily':
            day_start, day_end = _announcement_campaign_day_bounds()
            used_today = AnnouncementDelivery.query.filter(
                AnnouncementDelivery.announcement_id == campaign.id,
                AnnouncementDelivery.processed_at >= day_start,
                AnnouncementDelivery.processed_at < day_end,
            ).count()
            available = min(available, max(0, int(campaign.daily_limit or 1) - used_today))
            if available <= 0:
                campaign.status = 'queued'
                db.session.commit()
                continue
        deliveries = AnnouncementDelivery.query.filter(
            AnnouncementDelivery.announcement_id == campaign.id,
            AnnouncementDelivery.status.in_(('pending', 'retry')),
            db.or_(AnnouncementDelivery.next_attempt_at.is_(None), AnnouncementDelivery.next_attempt_at <= now),
        ).order_by(AnnouncementDelivery.id.asc()).limit(available).all()
        current_sms_clients = {}
        selected_sms_statuses = None
        if campaign.channel == 'sms':
            try:
                load_snapshot_from_redis()
            except Exception:
                pass
            selected_sms_statuses = _announcement_filter_values(
                campaign.audience_statuses, ANNOUNCEMENT_STATUS_GROUPS,
                ANNOUNCEMENT_STATUS_GROUPS)
            for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                try:
                    server_id = int(inbound.get('server_id'))
                except (TypeError, ValueError):
                    continue
                for client in (inbound.get('clients') or []):
                    email = str(client.get('email') or '').strip().lower()
                    if email:
                        current_sms_clients[(server_id, email)] = client
        for delivery in deliveries:
            context = delivery.context()
            current_client = None
            if selected_sms_statuses is not None:
                try:
                    current_client = current_sms_clients.get((
                        int(delivery.server_id), str(delivery.email or '').strip().lower()))
                except (TypeError, ValueError):
                    current_client = None
            if (current_client is not None
                    and _announcement_client_status_group(current_client)
                    not in selected_sms_statuses):
                delivery.status = 'skipped'
                delivery.last_error = 'audience_changed'
                delivery.last_error_source = 'panel'
                delivery.processed_at = datetime.utcnow()
                delivery.next_attempt_at = None
                processed += 1
                continue
            message = _render_text_template(campaign.message, context).strip()
            delivery.attempts = int(delivery.attempts or 0) + 1
            if not message:
                delivery.status = 'failed'
                delivery.last_error = 'rendered_message_empty'
                delivery.last_error_source = 'panel'
                delivery.processed_at = datetime.utcnow()
                processed += 1
                continue
            try:
                if campaign.channel == 'sms':
                    if _sms_account_opted_out(delivery.server_id, delivery.email or '', context.get('comment', ''), refresh_shared=True):
                        delivery.status, delivery.last_error = 'skipped', 'opted_out'
                    else:
                        sms_cfg = _get_sms_runtime_settings()
                        if _sms_in_quiet_hours(sms_cfg):
                            delivery.status, delivery.next_attempt_at = 'retry', datetime.utcnow() + timedelta(minutes=30)
                            delivery.last_error = 'quiet_hours'
                            continue
                        pace = float(sms_cfg.get('send_pace_seconds') or 0)
                        if pace > 0 and SMS_LAST_SEND_TS[0] > 0:
                            gap = pace - (time.time() - SMS_LAST_SEND_TS[0])
                            if gap > 0:
                                delivery.status = 'retry'
                                delivery.next_attempt_at = datetime.utcnow() + timedelta(seconds=max(1, math.ceil(gap)))
                                delivery.last_error = 'pace_gated'
                                continue
                        segment_info = _sms_segment_info(message)
                        delivery.segment_count = max(
                            1, int(segment_info.get('sms_segments') or 1))
                        ann_daily_limit = int(sms_cfg.get('announcement_daily_limit') or 500)
                        ann_used_today = _sms_announcement_segments_used_today()
                        slot_ok, slot_reason = _sms_take_send_slot(
                            delivery.recipient, sms_cfg, segment_info['sms_segments'],
                            daily_limit=ann_daily_limit, used_today=ann_used_today,
                            priority='announcement')
                        if not slot_ok:
                            delivery.status = 'retry'
                            delivery.next_attempt_at = datetime.utcnow() + timedelta(
                                minutes=30 if slot_reason == 'daily_limit_reached' else
                                        (60 if slot_reason == 'hourly_limit_reached' else 5))
                            delivery.last_error = slot_reason
                            continue
                        result = _send_sms_via_gmweb(
                            delivery.recipient, message, cfg=sms_cfg,
                            priority=_gmweb_sms_priority('announcement'),
                            idempotency_key=(
                                f'announcement-{campaign.id}-{delivery.id}'
                                f'-r{int(delivery.resend_count or 0)}'
                            ),
                        )
                        SMS_LAST_SEND_TS[0] = time.time()
                        reason = result.get('reason') or ''
                        delivery.gateway_request_id = (
                            str(result.get('request_id'))[:128]
                            if result.get('request_id') else None)
                        delivery.gateway_provider = str(result.get('provider') or 'gmweb')[:24]
                        delivery.gateway_state = (
                            str(result.get('status'))[:32]
                            if result.get('status') else None)
                        delivery.gateway_stage = (
                            str(result.get('verification_status'))[:64]
                            if result.get('verification_status') else None)
                        delivery.gateway_priority = (
                            str(result.get('priority'))[:24]
                            if result.get('priority') else 'announcement')
                        delivery.gateway_priority_level = int(
                            result.get('priority_level') or 10)
                        delivery.gateway_submitted_once = result.get('submitted_once')
                        delivery.gateway_verification_status = (
                            str(result.get('verification_status'))[:64]
                            if result.get('verification_status') else None)
                        delivery.gateway_sent_to = (
                            str(result.get('sent_to'))[:32]
                            if result.get('sent_to') else None)
                        if result.get('manual_review'):
                            delivery.status = 'manual_review'
                            delivery.sent_at = None
                            delivery.last_error = 'unverified_manual_review'
                            delivery.last_error_source = result.get('provider') or 'gmweb'
                            delivery.processed_at = datetime.utcnow()
                            delivery.next_attempt_at = None
                        elif result.get('accepted') and result.get('request_id') and not result.get('terminal'):
                            delivery.status = 'queued'
                            delivery.sent_at = None
                            delivery.last_error = None
                            delivery.last_error_source = None
                            delivery.processed_at = None
                            capacity_data['available'] = max(
                                0, int(capacity_data.get('available') or 0) - 1)
                        elif result.get('sent'):
                            delivery.status, delivery.sent_at, delivery.last_error = 'sent', datetime.utcnow(), None
                            delivery.last_error_source = None
                        elif (result.get('status_code') == 429
                              or reason in ('daily_limit_reached', 'recipient_rate_limited')):
                            retry_after = int(result.get('retry_after_seconds') or 60)
                            delivery.status = 'retry'
                            delivery.next_attempt_at = datetime.utcnow() + timedelta(seconds=retry_after)
                            delivery.last_error = reason
                            delivery.last_error_source = (
                                'gmweb' if result.get('status_code') == 429 else 'panel')
                            _sms_refund_daily_segments(delivery.segment_count)
                        else:
                            delivery.status, delivery.last_error = 'failed', reason or 'send_failed'
                            delivery.last_error_source = (
                                'gmweb' if result.get('status_code') is not None else 'panel')
                        _sms_log_row(
                            f'announcement-{campaign.id}', (delivery.email or '').lower(),
                            delivery.server_id, context.get('server_name'), 'announcement',
                            delivery.recipient,
                            ('manual_review' if result.get('manual_review') else
                             _sms_accepted_status(result) if result.get('sent') else 'failed'),
                            (None if result.get('sent') else delivery.last_error), result,
                        )
                        if result.get('error_code') == 'announcement_queue_full':
                            campaign.status = 'queued'
                            db.session.commit()
                            return processed + 1
                elif campaign.channel == 'whatsapp':
                    if _comment_opted_out(context.get('comment'), 'nopm'):
                        delivery.status, delivery.last_error = 'skipped', 'opted_out'
                    else:
                        result = _send_whatsapp_message('announcement', delivery.recipient, message)
                        reason = result.get('reason') or ''
                        if result.get('sent'):
                            delivery.status, delivery.sent_at, delivery.last_error = 'sent', datetime.utcnow(), None
                        elif reason in ('daily_limit_reached', 'pace_gated', 'recipient_rate_limited'):
                            delivery.status, delivery.next_attempt_at = 'retry', datetime.utcnow() + timedelta(minutes=5)
                            delivery.last_error = reason
                        else:
                            delivery.status, delivery.last_error = 'failed', reason or 'send_failed'
                else:
                    bot = db.session.get(TelegramBotInstance, delivery.bot_instance_id)
                    if not bot or not bot.enabled or bot.archived_at is not None:
                        delivery.status, delivery.last_error = 'failed', 'Bot is unavailable'
                        delivery.last_error_source = 'panel'
                    else:
                        api = api_by_bot.get(bot.id)
                        if api is None:
                            api = _telegram_bot_api_client(bot)
                            api_by_bot[bot.id] = api
                        api.send_message(int(delivery.recipient), message, disable_web_page_preview=True)
                        delivery.status, delivery.sent_at, delivery.last_error = 'sent', datetime.utcnow(), None
            except Exception as exc:
                delivery.last_error = str(redact_connection_error(exc))[:500]
                delivery.last_error_source = 'panel'
                if delivery.attempts >= 5:
                    delivery.status = 'failed'
                else:
                    delivery.status = 'retry'
                    delivery.next_attempt_at = datetime.utcnow() + timedelta(minutes=5)
            if delivery.status in ('sent', 'failed', 'skipped'):
                delivery.processed_at = datetime.utcnow()
            processed += 1
            if processed >= batch_size:
                break
        counts = _recount_announcement_campaign(campaign)
        if not any(counts.get(value, 0) for value in ('pending', 'retry', 'queued', 'active')):
            campaign.status, campaign.finished_at = 'completed', datetime.utcnow()
        db.session.commit()
        if processed >= batch_size:
            break
    return processed


# ─────────────────────────────────────────────────────────────────────────────
# SMS Automation (via the GMweb-API gateway)
# Auto-sends SMS on create / renew / near-depletion using the existing SMS
# templates — but ONLY for accounts NOT owned by a reseller (i.e. system /
# admin / superadmin accounts). Reseller-owned accounts are never messaged.
# ─────────────────────────────────────────────────────────────────────────────
SMS_AUTOMATION_ENABLED_KEY      = 'sms_automation_enabled'
SMS_PROVIDER_KEY                = 'sms_provider'
SMS_GMWEB_BASE_URL_KEY          = 'sms_gmweb_base_url'
SMS_GMWEB_API_KEY_KEY           = 'sms_gmweb_api_key'
SMS_GMWEB_TIMEOUT_KEY           = 'sms_gmweb_timeout'
SMS_CUSTOM_BASE_URL_KEY         = 'sms_custom_base_url'
SMS_CUSTOM_API_KEY_KEY          = 'sms_custom_api_key'
SMS_CUSTOM_TIMEOUT_KEY          = 'sms_custom_timeout'
SMS_TRIGGER_CREATED_KEY         = 'sms_trigger_created'
SMS_TRIGGER_RENEW_KEY           = 'sms_trigger_renew'
SMS_TRIGGER_DEPLETION_KEY       = 'sms_trigger_depletion'  # legacy combined trigger (back-compat)
# Granular state triggers — map 1:1 to the monitor service-state tags so the
# automated SMS uses the SAME per-state template the operator edits in Monitor.
SMS_TRIGGER_NEAR_EXPIRY_KEY     = 'sms_trigger_near_expiry'  # monitor tag 'soon'
SMS_TRIGGER_LOW_VOLUME_KEY      = 'sms_trigger_low_volume'   # monitor tag 'low'
SMS_TRIGGER_EXPIRED_KEY         = 'sms_trigger_expired'      # monitor tag 'expired'
SMS_TRIGGER_ENDED_KEY           = 'sms_trigger_ended'        # monitor tag 'ended'
SMS_DEPLETION_EXPIRY_DAYS_KEY   = 'sms_depletion_expiry_days'
SMS_DEPLETION_VOLUME_GB_KEY     = 'sms_depletion_volume_gb'
SMS_DEPLETION_COOLDOWN_DAYS_KEY = 'sms_depletion_cooldown_days'  # legacy fallback (days) for per-state cooldown
# Per-state resend cooldown in HOURS — shared across SMS + WhatsApp so a user/uuid
# is never double-messaged on either channel inside the window. Survives crashes
# because it's enforced from the persisted WhatsappBotLog, not in-memory state.
SMS_COOLDOWN_HOURS_NEAR_EXPIRY_KEY = 'sms_cooldown_hours_near_expiry'
SMS_COOLDOWN_HOURS_LOW_VOLUME_KEY  = 'sms_cooldown_hours_low_volume'
SMS_COOLDOWN_HOURS_EXPIRED_KEY     = 'sms_cooldown_hours_expired'
SMS_COOLDOWN_HOURS_ENDED_KEY       = 'sms_cooldown_hours_ended'
SMS_EXPIRED_MAX_AGE_DAYS_KEY    = 'sms_expired_max_age_days'  # don't SMS accounts expired longer than this (0 = use Monitor hide_days)
SMS_ENDED_MAX_AGE_DAYS_KEY      = 'sms_ended_max_age_days'    # stop SMS this many days after first 'ended' message (0 = no cutoff)
SMS_MIN_INTERVAL_SECONDS_KEY    = 'sms_min_interval_seconds'
SMS_DAILY_LIMIT_KEY             = 'sms_daily_limit'
SMS_HOURLY_LIMIT_KEY            = 'sms_hourly_limit'          # 0 = unlimited
# How long a non-terminal send may stay unresolved before a 404 from the gateway
# is treated as "the record aged out of the ledger" rather than a transient miss.
SMS_STATUS_UNRESOLVABLE_AFTER   = timedelta(hours=6)
SMS_ANNOUNCEMENT_DAILY_LIMIT_KEY = 'sms_announcement_daily_limit'
SMS_SEND_PACE_SECONDS_KEY       = 'sms_send_pace_seconds'  # global gap between ANY two sends so the gateway doesn't return HTTP 429
# Quiet hours (Tehran time): no automated SMS goes out inside this window. Scan
# candidates simply wait for the next run after the window; transactional
# create/renew messages are parked in PendingSms and flushed once it ends.
SMS_QUIET_ENABLED_KEY = 'sms_quiet_enabled'
SMS_QUIET_START_KEY   = 'sms_quiet_start_hour'  # 0-23, inclusive
SMS_QUIET_END_KEY     = 'sms_quiet_end_hour'    # 0-23, exclusive
# When on, the scan skips any account that is unlimited in either dimension
# (no volume cap or no expiry date), so only fully-limited accounts get messaged.
SMS_SKIP_UNLIMITED_KEY = 'sms_skip_unlimited'
# Royalty SMS: nudge owner-less idle accounts (enabled, zero traffic since the
# window start). Big lists drain over days under the shared daily cap; a long
# cooldown keeps it fair (each user once per window) — skips are logged.
SMS_TRIGGER_ROYALTY_KEY        = 'sms_trigger_royalty'
SMS_ROYALTY_DAYS_KEY           = 'sms_royalty_days'           # idle window (days)
SMS_ROYALTY_COOLDOWN_DAYS_KEY  = 'sms_royalty_cooldown_days'  # don't re-royalty within N days

SMS_SEND_TRACKER = {'daily': {}, 'per_recipient': {}}
SMS_SCAN_CANCEL = threading.Event()  # set → running scan aborts after current item
SMS_LAST_SEND_TS = [0.0]             # monotonic-ish wall clock of the previous send (global pace)
SMS_SEND_TRACKER_LOCK = threading.RLock()

_GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ "
    "!\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENDED = set("\f^{}\\[~]|€")


def _sms_segment_info(message: str | None) -> dict:
    """Calculate billable SMS segments using GSM-7/UCS-2 concatenation rules."""
    value = str(message or '')
    gsm7 = all(ch in _GSM7_BASIC or ch in _GSM7_EXTENDED for ch in value)
    if gsm7:
        units = sum(2 if ch in _GSM7_EXTENDED else 1 for ch in value)
        single_limit, multipart_limit, encoding = 160, 153, 'GSM-7'
    else:
        # Providers count Unicode SMS in UTF-16/UCS-2 code units. Emoji outside
        # the BMP consume two units, which Python len() alone would undercount.
        units = len(value.encode('utf-16-be')) // 2
        single_limit, multipart_limit, encoding = 70, 67, 'UCS-2'
    segments = 0 if units == 0 else (1 if units <= single_limit else math.ceil(units / multipart_limit))
    return {
        'sms_encoding': encoding,
        'sms_units': units,
        'sms_characters': len(value),
        'sms_segments': segments,
    }


def _sms_tehran_day() -> tuple[str, datetime, datetime]:
    now_utc = datetime.utcnow()
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo('Asia/Tehran')
            local = now_utc.replace(tzinfo=timezone.utc).astimezone(tz)
            start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local + timedelta(days=1)
            return (start_local.date().isoformat(),
                    start_local.astimezone(timezone.utc).replace(tzinfo=None),
                    end_local.astimezone(timezone.utc).replace(tzinfo=None))
        except Exception:
            pass
    local = now_utc + timedelta(hours=3, minutes=30)
    start_utc = datetime.combine(local.date(), datetime.min.time()) - timedelta(hours=3, minutes=30)
    return local.date().isoformat(), start_utc, start_utc + timedelta(days=1)


def _sms_db_segments_used_today() -> int:
    return _sms_db_segment_stats_today().get('completed', 0)


def _sms_announcement_segments_used_today() -> int:
    """Count billable SMS segments consumed by announcement campaigns today (Tehran time)."""
    try:
        _day, start_utc, end_utc = _sms_tehran_day()
        total = db.session.query(
            db.func.coalesce(db.func.sum(AnnouncementDelivery.segment_count), 0)
        ).filter(
            AnnouncementDelivery.status == 'sent',
            AnnouncementDelivery.sent_at >= start_utc,
            AnnouncementDelivery.sent_at < end_utc,
            AnnouncementDelivery.segment_count.isnot(None),
        ).scalar()
        return max(0, int(total or 0))
    except Exception:
        return 0


def _sms_tehran_hour_bucket(now_utc: datetime | None = None) -> tuple[str, datetime, datetime]:
    """[hour_key, window_start_utc, window_end_utc) for the Tehran-clock hour."""
    base = now_utc or datetime.utcnow()
    try:
        tz = ZoneInfo('Asia/Tehran')
        local = base.replace(tzinfo=timezone.utc).astimezone(tz)
    except Exception:
        local = base + timedelta(hours=3, minutes=30)
    hour_start_local = local.replace(minute=0, second=0, microsecond=0)
    try:
        start_utc = hour_start_local.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        start_utc = hour_start_local - timedelta(hours=3, minutes=30)
    return (f'{local.year:04d}{local.month:02d}{local.day:02d}{local.hour:02d}',
            start_utc, start_utc + timedelta(hours=1))


def _sms_db_segments_used_this_hour(now_utc: datetime | None = None) -> int:
    """Billable SMS segments completed inside the current Tehran-clock hour.

    Same accounting as the daily counter (completed-only), so failed/queued
    sends never burn the hourly budget — the status worker's reconciliation
    moves rows into the completed bucket only once the gateway confirms.
    """
    _key, start_utc, end_utc = _sms_tehran_hour_bucket(now_utc)
    try:
        total = db.session.query(
            db.func.coalesce(db.func.sum(SmsSendLog.segment_count), 0)
        ).filter(
            SmsSendLog.created_at >= start_utc,
            SmsSendLog.created_at < end_utc,
            SmsSendLog.status.in_(('sent', 'completed', 'delivered')),
            SmsSendLog.segment_count.isnot(None),
        ).scalar()
        return max(0, int(total or 0))
    except Exception:
        # Best-effort like every other counter here: a DB hiccup must not
        # wedge the whole scan — 0 lets this hour proceed (daily cap still
        # applies on the next gate).
        return 0


def _sms_db_segment_stats_today() -> dict:
    """Daily SMS segment accounting in Tehran time.

    submitted: accepted by GMweb / attempted through a status-capable gateway.
    completed: billable-successful sends only; this is the daily limit basis.
    failed: terminal failed sends; not billable and must not consume the daily cap.
    inflight: accepted by GMweb but still not terminal.
    """
    stats = {'submitted': 0, 'completed': 0, 'failed': 0, 'inflight': 0}
    try:
        _day, start_utc, end_utc = _sms_tehran_day()
        base = SmsSendLog.query.filter(
            SmsSendLog.created_at >= start_utc,
            SmsSendLog.created_at < end_utc,
        )
        rows = base.with_entities(
            SmsSendLog.status,
            SmsSendLog.gateway_state,
            SmsSendLog.terminal,
            SmsSendLog.successful,
            SmsSendLog.request_id,
            SmsSendLog.segment_count,
        ).all()
        success_states = {'sent', 'completed', 'delivered'}
        failed_states = {'failed', 'expired', 'cancelled'}
        for status, gateway_state, terminal, successful, request_id, segment_count in rows:
            seg = max(1, int(segment_count or 1))
            st = str(status or '').strip().lower()
            gw = str(gateway_state or '').strip().lower()
            has_request = bool(request_id)
            if has_request:
                stats['submitted'] += seg

            is_completed = (
                successful is True
                or (terminal is True and (st in success_states or gw in success_states))
                or (not has_request and st == 'sent')
                or st in {'completed', 'delivered'}
            )
            is_failed = (
                st in failed_states
                or gw in failed_states
                or successful is False
                or (terminal is True and successful is not True and not is_completed)
            )
            if is_completed:
                stats['completed'] += seg
            elif is_failed:
                stats['failed'] += seg
            elif has_request and terminal is not True:
                stats['inflight'] += seg
    except Exception:
        pass
    return {k: max(0, int(v or 0)) for k, v in stats.items()}


def _sms_segment_counter_key(day: str) -> str:
    return f'eve:sms:segments:{day}'


def _sms_reserve_daily_segments(segments: int, daily_limit: int,
                                 used_today: int | None = None) -> bool:
    segments = max(1, int(segments or 1))
    # Daily cap is billable/completed SMS, not mere requests accepted by GMweb.
    # If Google Messages is unpaired or a queued send later fails, it must not
    # burn the day's budget. We therefore don't "reserve" here; status polling
    # moves rows into the completed bucket when the gateway confirms success.
    if used_today is None:
        used_today = _sms_db_segments_used_today()
    return (used_today + segments) <= int(daily_limit or 0)


def _sms_refund_daily_segments(segments: int) -> None:
    # Kept for old call sites. The new daily cap is computed from completed
    # status rows, so failed/cancelled/unpaired requests need no counter refund.
    return None


def _sms_daily_segments_used() -> int:
    return _sms_db_segments_used_today()


def _get_sms_runtime_settings() -> dict:
    from app import _get_system_configs_batch, _parse_bool  # deferred: app-level helper, avoids circular import
    keys = [
        SMS_AUTOMATION_ENABLED_KEY, SMS_PROVIDER_KEY,
        SMS_GMWEB_BASE_URL_KEY, SMS_GMWEB_API_KEY_KEY, SMS_GMWEB_TIMEOUT_KEY,
        SMS_CUSTOM_BASE_URL_KEY, SMS_CUSTOM_API_KEY_KEY, SMS_CUSTOM_TIMEOUT_KEY,
        SMS_TRIGGER_CREATED_KEY, SMS_TRIGGER_RENEW_KEY,
        SMS_TRIGGER_DEPLETION_KEY, SMS_DEPLETION_EXPIRY_DAYS_KEY,
        SMS_DEPLETION_VOLUME_GB_KEY, SMS_DEPLETION_COOLDOWN_DAYS_KEY,
        SMS_COOLDOWN_HOURS_NEAR_EXPIRY_KEY, SMS_COOLDOWN_HOURS_LOW_VOLUME_KEY,
        SMS_COOLDOWN_HOURS_EXPIRED_KEY, SMS_COOLDOWN_HOURS_ENDED_KEY,
        SMS_EXPIRED_MAX_AGE_DAYS_KEY, SMS_ENDED_MAX_AGE_DAYS_KEY,
        SMS_MIN_INTERVAL_SECONDS_KEY, SMS_DAILY_LIMIT_KEY,
        SMS_HOURLY_LIMIT_KEY,
        SMS_ANNOUNCEMENT_DAILY_LIMIT_KEY,
        SMS_SEND_PACE_SECONDS_KEY,
        SMS_TRIGGER_NEAR_EXPIRY_KEY, SMS_TRIGGER_LOW_VOLUME_KEY,
        SMS_TRIGGER_EXPIRED_KEY, SMS_TRIGGER_ENDED_KEY,
        SMS_QUIET_ENABLED_KEY, SMS_QUIET_START_KEY, SMS_QUIET_END_KEY,
        SMS_SKIP_UNLIMITED_KEY,
        SMS_TRIGGER_ROYALTY_KEY, SMS_ROYALTY_DAYS_KEY, SMS_ROYALTY_COOLDOWN_DAYS_KEY,
    ]
    c = _get_system_configs_batch(keys)

    def _txt(k, d=''):
        v = c.get(k)
        return str(v) if v is not None else d

    def _bool(k, d=False):
        return _parse_bool(_txt(k, 'true' if d else 'false'))

    def _int(k, d, lo=None, hi=None):
        return _parse_int(_txt(k, str(d)), d, min_value=lo, max_value=hi)

    def _float(k, d, lo=None, hi=None):
        try:
            v = float(_txt(k, str(d)))
        except (TypeError, ValueError):
            v = float(d)
        if lo is not None:
            v = max(v, lo)
        if hi is not None:
            v = min(v, hi)
        return v

    provider = _txt(SMS_PROVIDER_KEY, 'gmweb').strip().lower()
    if provider not in ('gmweb', 'custom_http'):
        provider = 'gmweb'
    gmweb = {
        'base_url': _txt(SMS_GMWEB_BASE_URL_KEY, '').strip().rstrip('/'),
        'api_key': _txt(SMS_GMWEB_API_KEY_KEY, '').strip(),
        'timeout_seconds': _int(SMS_GMWEB_TIMEOUT_KEY, 15, lo=3, hi=90),
    }
    custom_http = {
        'base_url': _txt(SMS_CUSTOM_BASE_URL_KEY, '').strip().rstrip('/'),
        'api_key': _txt(SMS_CUSTOM_API_KEY_KEY, '').strip(),
        'timeout_seconds': _int(SMS_CUSTOM_TIMEOUT_KEY, 15, lo=3, hi=90),
    }
    selected = gmweb if provider == 'gmweb' else custom_http
    return {
        'enabled': _bool(SMS_AUTOMATION_ENABLED_KEY, False),
        'provider': provider,
        'providers': {'gmweb': gmweb, 'custom_http': custom_http},
        **selected,
        'trigger_created': _bool(SMS_TRIGGER_CREATED_KEY, True),
        'trigger_renew': _bool(SMS_TRIGGER_RENEW_KEY, True),
        'trigger_depletion': _bool(SMS_TRIGGER_DEPLETION_KEY, False),
        # Granular state triggers. Back-compat: when none of the new keys are
        # present yet, fall back to the legacy combined depletion flag for the
        # two "near" states so existing installs keep behaving the same.
        'trigger_near_expiry': _bool(SMS_TRIGGER_NEAR_EXPIRY_KEY, _bool(SMS_TRIGGER_DEPLETION_KEY, False)),
        'trigger_low_volume': _bool(SMS_TRIGGER_LOW_VOLUME_KEY, _bool(SMS_TRIGGER_DEPLETION_KEY, False)),
        'trigger_expired': _bool(SMS_TRIGGER_EXPIRED_KEY, False),
        'trigger_ended': _bool(SMS_TRIGGER_ENDED_KEY, False),
        'depletion_expiry_days': _int(SMS_DEPLETION_EXPIRY_DAYS_KEY, 3, lo=0, hi=60),
        'depletion_volume_gb': _float(SMS_DEPLETION_VOLUME_GB_KEY, 2.0, lo=0.0, hi=1000.0),
        'depletion_cooldown_days': _int(SMS_DEPLETION_COOLDOWN_DAYS_KEY, 7, lo=1, hi=120),
        # Per-state resend gap in hours (shared SMS+WhatsApp). Default = legacy
        # cooldown-days × 24 so existing installs keep the same spacing.
        'cooldown_hours': {
            'near_expiry': _int(SMS_COOLDOWN_HOURS_NEAR_EXPIRY_KEY, 24, lo=1, hi=8760),
            'low_volume':  _int(SMS_COOLDOWN_HOURS_LOW_VOLUME_KEY, 24, lo=1, hi=8760),
            'expired':     _int(SMS_COOLDOWN_HOURS_EXPIRED_KEY, 48, lo=1, hi=8760),
            'ended':       _int(SMS_COOLDOWN_HOURS_ENDED_KEY, 24, lo=1, hi=8760),
        },
        'expired_max_age_days': _int(SMS_EXPIRED_MAX_AGE_DAYS_KEY, 30, lo=0, hi=3650),
        'ended_max_age_days': _int(SMS_ENDED_MAX_AGE_DAYS_KEY, 0, lo=0, hi=3650),
        'min_interval_seconds': _int(SMS_MIN_INTERVAL_SECONDS_KEY, 30, lo=0, hi=3600),
        'daily_limit': _int(SMS_DAILY_LIMIT_KEY, 200, lo=1, hi=100000),
        # Hourly throttle for bulk lanes (near_expiry/low_volume/expired/ended/
        # royalty/announcement). 0 = unlimited. Transactional create/renew
        # (critical lane) never counts against this gate.
        'hourly_limit': _int(SMS_HOURLY_LIMIT_KEY, 0, lo=0, hi=100000),
        'announcement_daily_limit': _int(SMS_ANNOUNCEMENT_DAILY_LIMIT_KEY, 500, lo=1, hi=100000),
        'send_pace_seconds': _float(SMS_SEND_PACE_SECONDS_KEY, 3.0, lo=0.0, hi=60.0),
        'quiet_enabled': _bool(SMS_QUIET_ENABLED_KEY, False),
        'quiet_start': _int(SMS_QUIET_START_KEY, 2, lo=0, hi=23),
        'quiet_end': _int(SMS_QUIET_END_KEY, 8, lo=0, hi=23),
        'skip_unlimited': _bool(SMS_SKIP_UNLIMITED_KEY, False),
        'trigger_royalty': _bool(SMS_TRIGGER_ROYALTY_KEY, False),
        'royalty_days': _int(SMS_ROYALTY_DAYS_KEY, 3, lo=1, hi=365),
        'royalty_cooldown_days': _int(SMS_ROYALTY_COOLDOWN_DAYS_KEY, 30, lo=1, hi=365),
    }


def _get_sms_provider_settings(provider: str | None = None, cfg: dict | None = None) -> dict:
    """Return connection settings for one provider while retaining legacy cfg callers."""
    runtime = cfg or _get_sms_runtime_settings()
    provider_name = str(provider or runtime.get('provider') or 'gmweb').strip().lower()
    if provider_name not in ('gmweb', 'custom_http'):
        provider_name = 'gmweb'
    providers = runtime.get('providers') if isinstance(runtime.get('providers'), dict) else {}
    connection = providers.get(provider_name) if isinstance(providers.get(provider_name), dict) else runtime
    return {**runtime, **connection, 'provider': provider_name}


def _tehran_hour(now_utc=None) -> int:
    """Current hour (0-23) in Asia/Tehran. Uses the real IANA zone so it stays
    correct regardless of the panel's configured timezone (and any future DST
    change); falls back to a fixed +3:30 offset only if zoneinfo is unavailable."""
    base = now_utc or datetime.utcnow()
    if ZoneInfo is not None:
        try:
            return base.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Asia/Tehran')).hour
        except Exception:
            pass
    return (base + timedelta(hours=3, minutes=30)).hour


def _sms_in_quiet_hours(cfg: dict, now_utc=None) -> bool:
    """True when the current Asia/Tehran time falls inside the configured quiet
    window, during which no automated SMS is sent. Handles windows that wrap past
    midnight (e.g. 22 → 6). A zero-length window (start == end) means 'disabled'."""
    if not cfg.get('quiet_enabled'):
        return False
    start = int(cfg.get('quiet_start', 0)) % 24
    end = int(cfg.get('quiet_end', 0)) % 24
    if start == end:
        return False
    h = _tehran_hour(now_utc)
    if start < end:
        return start <= h < end
    return h >= start or h < end  # wraps midnight


def _account_has_reseller_owner(server_id, email) -> bool:
    """True only when the account's owner is an actual reseller — SMS automation
    then skips it. A ClientOwnership row is written for EVERY creator (admin /
    superadmin / reseller) for billing/tracking, so the row's mere existence is not
    enough: admin and superadmin are system accounts and must stay eligible. We
    flag the account only if at least one owning Admin has role == 'reseller'."""
    try:
        email_l = (email or '').strip().lower()
        if not email_l:
            return False
        try:
            sid = int(server_id)
        except (TypeError, ValueError):
            sid = server_id
        row = (db.session.query(ClientOwnership.id)
               .join(Admin, Admin.id == ClientOwnership.reseller_id)
               .filter(ClientOwnership.server_id == sid,
                       db.func.lower(ClientOwnership.client_email) == email_l,
                       Admin.role == 'reseller')
               .first())
        return row is not None
    except Exception:
        return False


def _recent_bot_message_within(email_l: str, sid_norm, hours: int) -> bool:
    """True if ANY automated message (SMS or WhatsApp, any state) went to this
    (account, server) within the last `hours`. Event-agnostic on purpose so the
    cooldown is shared across both channels. Reads the persisted WhatsappBotLog,
    so it stays correct across crashes/restarts."""
    try:
        cut = datetime.utcnow() - timedelta(hours=max(int(hours or 0), 0))
        return WhatsappBotLog.query.filter(
            WhatsappBotLog.email == email_l,
            WhatsappBotLog.server_id == (sid_norm or 0),
            WhatsappBotLog.sent_at >= cut,
        ).first() is not None
    except Exception:
        return False


def _ended_first_contact(email_l: str, sid_norm) -> datetime | None:
    """When this (account, server) was first messaged for the 'ended' state, read
    from the persisted dedup log. Used to stop nagging a volume-ended account that
    has no expiry date forever. Reset on renewal (the log is cleared), so the clock
    restarts after each renew."""
    try:
        row = (WhatsappBotLog.query
               .filter(WhatsappBotLog.email == email_l,
                       WhatsappBotLog.server_id == (sid_norm or 0),
                       WhatsappBotLog.event.ilike('%ended%'))
               .order_by(WhatsappBotLog.sent_at.asc()).first())
        return row.sent_at if row else None
    except Exception:
        return None


def _comment_opted_out(comment: str | None, *tags: str) -> bool:
    """True if the client comment contains an opt-out hashtag (e.g. #nosms / #nopm)
    anywhere in the text, even when surrounded by other notes. Matching is
    deliberately substring-based and case-insensitive: once the literal tag is
    present, automation must fail closed. '# nosms' is accepted too."""
    s = (comment or '').lower()
    if '#' not in s:
        return False
    for t in tags:
        core = re.escape(t.lstrip('#').lower())
        if re.search(r'#\s*' + core, s):
            return True
    return False


SMS_COMMENT_OPTOUT_TAGS = ('nosms', 'nopm')


def _sms_comment_opted_out(comment: str | None) -> bool:
    """Both tags suppress SMS automation; #nopm is a global private-message opt-out."""
    return _comment_opted_out(comment, *SMS_COMMENT_OPTOUT_TAGS)


def _sms_account_opted_out(server_id, email: str, fallback_comment: str = '',
                           refresh_shared: bool = False) -> bool:
    """Re-check the newest cached comment before handing an SMS to GMweb.

    Queue candidates carry a comment snapshot which can become stale while they
    wait behind pacing/rate limits. In Redis mode, pull any newer server block,
    then inspect every matching copy of the account. If the account is not in the
    cache yet (e.g. a just-created client), fall back to the event comment.
    """
    if refresh_shared:
        try:
            load_snapshot_from_redis()
        except Exception:
            pass
    try:
        sid = int(server_id)
    except (TypeError, ValueError):
        sid = server_id
    email_l = (email or '').strip().lower()
    found = False
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id', -1)) != int(sid):
                continue
        except (TypeError, ValueError):
            if inbound.get('server_id') != sid:
                continue
        for client in (inbound.get('clients') or []):
            if (client.get('email') or '').strip().lower() != email_l:
                continue
            found = True
            if _sms_comment_opted_out(client.get('comment')):
                return True
    return _sms_comment_opted_out(fallback_comment) if not found else False


def _toggle_optout_tags(comment: str | None, add: bool) -> str:
    """Add or remove the #nosms #nopm opt-out tags in a client comment, preserving
    any phone number / other notes already there. Idempotent (existing tags are
    stripped first, then re-added only when add=True)."""
    cleaned = re.sub(r'\s*#\s*(?:nosms|nopm)(?![a-z0-9])', '', comment or '',
                     flags=re.IGNORECASE).strip()
    if add:
        return (cleaned + ' #nosms #nopm').strip()
    return cleaned


def _clear_message_cooldown(email: str, server_id) -> None:
    """Reset all message counters/cooldowns for an account after a renewal so the
    automation can message it again from scratch. Clears both the manual Monitor
    send-counter (MonitorMessageLog) and the automation dedup log (WhatsappBotLog).
    Best-effort; commits on its own."""
    email_l = (email or '').strip().lower()
    if not email_l:
        return
    try:
        sid = int(server_id)
    except (TypeError, ValueError):
        sid = server_id
    try:
        MonitorMessageLog.query.filter_by(email=email_l, server_id=sid).delete()
        WhatsappBotLog.query.filter_by(email=email_l, server_id=sid).delete()
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _sms_take_send_slot(recipient: str, cfg: dict, segments: int = 1,
                        daily_limit: int | None = None,
                        used_today: int | None = None,
                        priority: str | None = None,
                        used_this_hour: int | None = None) -> tuple[bool, str | None]:
    now_ts = time.time()
    min_interval = int(cfg.get('min_interval_seconds') or 0)
    if daily_limit is None:
        daily_limit = int(cfg.get('daily_limit') or 200)
    # Transactional create/renew (critical lane) is always exempt from the
    # hourly throttle — a paying customer's confirmation must never wait for
    # a bulk window to reopen.
    if not priority:
        priority = str(cfg.get('priority') or '')
    hourly_exempt = str(priority).strip().lower() in ('created', 'renew', 'critical')
    with SMS_SEND_TRACKER_LOCK:
        per = SMS_SEND_TRACKER.get('per_recipient') or {}
        last = float(per.get(recipient) or 0.0)
        if min_interval > 0 and last > 0 and (now_ts - last) < min_interval:
            return False, 'recipient_rate_limited'
        if not hourly_exempt:
            hourly_limit = int(cfg.get('hourly_limit') or 0)
            if hourly_limit > 0:
                hour_used = _sms_db_segments_used_this_hour() if used_this_hour is None \
                    else max(0, int(used_this_hour))
                if (hour_used + segments) > hourly_limit:
                    return False, 'hourly_limit_reached'
        if not _sms_reserve_daily_segments(segments, daily_limit, used_today=used_today):
            return False, 'daily_limit_reached'
        per[recipient] = now_ts
        SMS_SEND_TRACKER['per_recipient'] = per
    return True, None


def _sms_gmweb_error_reason(resp) -> str:
    """Build a useful operator-facing reason from a non-2xx GMweb response."""
    status_code = getattr(resp, 'status_code', None)
    fallback = f"http_{status_code}" if status_code else "gateway_http_error"
    body = {}
    try:
        body = resp.json() if getattr(resp, 'content', None) else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return fallback

    parts = []
    error = body.get('error') or body.get('message')
    reason = body.get('reason')
    if error:
        parts.append(str(error))
    if reason and reason != error:
        parts.append(str(reason))

    limits = body.get('limits') if isinstance(body.get('limits'), dict) else {}
    used = body.get('used') if isinstance(body.get('used'), dict) else {}
    rate_bits = []
    if 'minute' in limits or 'minute' in used:
        rate_bits.append(f"minute {used.get('minute', '?')}/{limits.get('minute', '?')}")
    if 'hour' in limits or 'hour' in used:
        rate_bits.append(f"hour {used.get('hour', '?')}/{limits.get('hour', '?')}")
    if rate_bits:
        parts.append(", ".join(rate_bits))

    extra = {
        key: value for key, value in body.items()
        if key not in {'error', 'message', 'reason', 'limits', 'used'}
    }
    if extra:
        try:
            parts.append('response=' + json.dumps(
                extra, ensure_ascii=False, separators=(',', ':')))
        except (TypeError, ValueError):
            parts.append('response=' + str(extra))

    retry_after = None
    try:
        retry_after = resp.headers.get('retry-after') or resp.headers.get('Retry-After')
    except Exception:
        retry_after = None
    if retry_after:
        parts.append(f"retry_after={retry_after}s")

    return (f"{fallback}: {'; '.join(parts)}" if parts else fallback)[:500]


GMWEB_SMS_PRIORITY_LEVELS = {
    'critical': 1,
    'expired': 3,
    'expiring': 6,
    'announcement': 10,
}


def _gmweb_sms_priority(message_kind: str) -> str:
    """Map every Eve SMS kind to one canonical GMweb 0.3.30 priority lane."""
    kind = str(message_kind or '').strip().lower().replace('-', '_')
    if kind in ('purchase', 'created', 'create', 'renew', 'renewal', 'test'):
        return 'critical'
    if kind in ('expired', 'ended', 'time_expired', 'volume_expired'):
        return 'expired'
    if kind in ('near_expiry', 'low_volume', 'time_expiring', 'volume_expiring'):
        return 'expiring'
    if kind in ('announcement', 'royalty'):
        return 'announcement'
    raise ValueError(f'unmapped_sms_message_kind:{kind or "empty"}')


# A gateway misconfiguration should be visible once, not on every send.
_GMWEB_WARNED = set()


def _gmweb_warn(reason: str) -> None:
    text = str(reason or '').strip()
    if not text or text in _GMWEB_WARNED:
        return
    _GMWEB_WARNED.add(text)
    try:
        from app import app  # deferred: app-level object
        app.logger.warning('[GMweb] %s', text)
    except Exception:
        pass


def _gmweb_base(cfg: dict) -> tuple:
    """(base_url, reason) for the configured gateway, contract-validated.

    The URL is checked once here instead of trusting whatever was saved: a
    non-http(s) scheme, an embedded credential or a malformed host must never
    receive the bearer key.
    """
    result = gmweb_contract.validate_base_url(cfg.get('base_url'))
    if result.get('warning'):
        _gmweb_warn(result['warning'])
    return result.get('base'), result.get('reason')


def _get_gmweb_send_capacity(cfg: dict | None = None) -> dict:
    """Read selected gateway lane occupancy and free announcement capacity."""
    cfg = _get_sms_provider_settings(cfg=cfg)
    out = {
        'ok': False, 'status_code': None, 'reason': None,
        'provider': cfg.get('provider', 'gmweb'),
        'priorities': {}, 'announcement': {},
    }
    base, base_reason = _gmweb_base(cfg)
    api_key = (cfg.get('api_key') or '').strip()
    if not base or not api_key:
        out['reason'] = base_reason or 'gateway_not_configured'
        return out
    try:
        resp = requests.get(
            f"{base}{gmweb_contract.endpoint_path('send_capacity')}",
            headers=gmweb_contract.request_headers(api_key),
            timeout=min(int(cfg.get('timeout_seconds') or 15), 5),
        )
        out['status_code'] = resp.status_code
        if resp.status_code != 200:
            out['reason'] = _sms_gmweb_error_reason(resp)
            return out
        body = resp.json() if resp.content else {}
        if not isinstance(body, dict) or not isinstance(body.get('announcement'), dict):
            out['reason'] = 'invalid_capacity_response'
            return out
        announcement = body['announcement']
        out['priorities'] = {
            name: max(0, int((body.get('priorities') or {}).get(name) or 0))
            for name in GMWEB_SMS_PRIORITY_LEVELS
        }
        out['announcement'] = {
            'limit': max(0, int(announcement.get('limit') or 0)),
            'pending': max(0, int(announcement.get('pending') or 0)),
            'available': max(0, int(announcement.get('available') or 0)),
            'recommended_batch_size': max(
                1, int(announcement.get('recommendedBatchSize') or 1)),
        }
        out['ok'] = True
    except Exception as exc:
        out['reason'] = f'gateway_capacity_failed: {exc}'
    return out


def _send_sms_via_gmweb(to: str, text: str, cfg: dict | None = None, priority: str | None = None,
                        idempotency_key: str | None = None, meta: dict | None = None) -> dict:
    """POST a single SMS to the GMweb-API gateway. The gateway queues it and
    returns 200/202 with a stable requestId. Delivery is tracked separately by
    the SMS status worker; legacy gateways without requestId remain supported.

    ``priority`` must be one of Eve's four canonical lanes. ``idempotency_key``,
    when reused across a retry of the SAME logical message, lets GMweb return the
    original request safely after a lost response.

    ``meta`` is the notification identity the gateway stores with the queued
    message: ``{source, serviceKey, notificationKind, generation, correlationId,
    requiresValidation}``. It is what lets a later lifecycle invalidation find and
    revoke exactly this reminder, for exactly this service."""
    cfg = _get_sms_provider_settings(cfg=cfg)
    out = {
        'sent': False, 'reason': None, 'status_code': None,
        'request_id': None, 'job_id': None, 'status_url': None,
        'status': None, 'accepted': False, 'terminal': None, 'successful': None,
        'manual_review': False, 'error_code': None, 'retry_after_seconds': None,
        'priority': None, 'priority_level': None, 'queue_position': None,
        'provider': cfg.get('provider', 'gmweb'),
        'submitted_once': None, 'verification_status': None,
        'verification_attempts': None, 'requested_to': None, 'sent_to': None,
        'recipient_evidence': None, 'conversation_url': None,
        **_sms_segment_info(text),
    }
    base, base_reason = _gmweb_base(cfg)
    api_key = (cfg.get('api_key') or '').strip()
    if not base or not api_key:
        out['reason'] = base_reason or 'gateway_not_configured'
        return out
    canonical_priority = str(priority or '').strip().lower()
    if canonical_priority not in GMWEB_SMS_PRIORITY_LEVELS:
        out['reason'] = f'invalid_sms_priority:{canonical_priority or "missing"}'
        return out
    out['priority'] = canonical_priority
    out['priority_level'] = GMWEB_SMS_PRIORITY_LEVELS[canonical_priority]
    payload = {'to': to, 'text': text, 'priority': canonical_priority}
    notification_meta = _notification_meta(meta)
    if notification_meta:
        # Local contract assertion: never hand the gateway a meta block its own
        # validation will refuse (invalid_meta / meta_generation_required).
        meta_error = lifecycle_service.validate_send_meta(notification_meta)
        if meta_error:
            out['reason'] = meta_error
            return out
        payload['meta'] = notification_meta
    headers = gmweb_contract.request_headers(
        api_key, json_body=True, idempotency_key=idempotency_key)
    for network_attempt in range(2 if idempotency_key else 1):
        try:
            resp = requests.post(
                f"{base}{gmweb_contract.endpoint_path('send')}",
                json=payload,
                headers=headers,
                timeout=int(cfg.get('timeout_seconds') or 15),
            )
            out['status_code'] = resp.status_code
            try:
                body = resp.json() if resp.content else {}
            except (TypeError, ValueError):
                body = {}
            if isinstance(body, dict):
                response_data = dict(body)
                if isinstance(body.get('result'), dict):
                    response_data.update(body['result'])
                out['request_id'] = body.get('requestId')
                out['job_id'] = str(body.get('jobId')) if body.get('jobId') is not None else None
                out['status_url'] = body.get('statusUrl')
                out['status'] = body.get('status')
                out['error_code'] = body.get('error')
                out['priority'] = body.get('priority') or out['priority']
                out['priority_level'] = body.get('priorityLevel') or out['priority_level']
                out['queue_position'] = body.get('queuePosition')
                out['submitted_once'] = response_data.get('submittedOnce')
                out['verification_status'] = response_data.get('verificationStatus')
                out['verification_attempts'] = response_data.get('verificationAttempts')
                out['requested_to'] = response_data.get('requestedTo') or to
                out['sent_to'] = response_data.get('sentTo')
                out['recipient_evidence'] = response_data.get('recipientEvidence')
                out['conversation_url'] = response_data.get('conversationUrl')
                if response_data.get('terminal') is not None:
                    out['terminal'] = bool(response_data.get('terminal'))
                if response_data.get('successful') is not None:
                    out['successful'] = bool(response_data.get('successful'))
                retry_after = body.get('retryAfterSeconds')
                if retry_after is not None:
                    try:
                        out['retry_after_seconds'] = max(1, int(retry_after))
                    except (TypeError, ValueError):
                        pass
            status = str(out.get('status') or '').strip().lower()
            if status in ('sent', 'completed'):
                out['terminal'], out['successful'] = True, True
            elif status in ('unverified', 'failed', 'cancelled'):
                out['terminal'], out['successful'] = True, False
            out['manual_review'] = (
                status == 'unverified'
                or out.get('verification_status') == 'manual_review_required'
            )
            if resp.status_code in (200, 202):
                out['accepted'] = bool(
                    out.get('request_id') or out.get('job_id')
                    or status in ('sent', 'completed', 'duplicate_suppressed'))
                out['sent'] = bool(out['accepted'] and not out['manual_review']
                                   and status not in ('failed', 'cancelled'))
                if out['request_id'] and not out['status_url']:
                    out['status_url'] = f"/send/status/{out['request_id']}"
            else:
                out['reason'] = _sms_gmweb_error_reason(resp)
                if out.get('retry_after_seconds') is None:
                    try:
                        raw_retry = resp.headers.get('Retry-After')
                        out['retry_after_seconds'] = max(1, int(raw_retry)) if raw_retry else None
                    except (TypeError, ValueError):
                        pass
                if resp.status_code >= 500 and network_attempt == 0 and idempotency_key:
                    # A 5xx is ambiguous: the gateway may have queued the send
                    # before failing. The contract allows one retry when an
                    # idempotency key is present, which makes it converge.
                    out['reason'] = None
                    continue
            return out
        except Exception as exc:
            out['reason'] = f'gateway_error: {exc}'
            if network_attempt == 0 and idempotency_key:
                continue
            return out
    return out




def _notification_meta(meta: dict | None) -> dict:
    """Whitelist + bound the notification identity sent to the gateway.

    The gateway stores this with the queued message and matches lifecycle
    invalidations against it, so only the declared contract fields may cross the
    boundary -- never a free-form dict and never message content. Nothing here
    is trusted from an inbound request: every value is produced by EVE from its
    own durable state."""
    if not isinstance(meta, dict):
        return {}
    out = {}
    source = str(meta.get('source') or 'eve').strip()
    if source:
        out['source'] = source[:32]
    service_key = str(meta.get('serviceKey') or '').strip()
    if service_key:
        out['serviceKey'] = service_key[:200]
    kind = str(meta.get('notificationKind') or '').strip()
    if kind:
        out['notificationKind'] = kind[:48]
    generation = meta.get('generation')
    if generation is not None:
        try:
            out['generation'] = max(0, int(generation))
        except (TypeError, ValueError):
            pass
    correlation = str(meta.get('correlationId') or '').strip()
    if correlation:
        out['correlationId'] = correlation[:64]
    out['requiresValidation'] = bool(meta.get('requiresValidation', True))
    return out


def _service_uuid_for(server_id, email):
    """The panel client UUID for one (server, email), or None.

    Used only to stamp the durable baseline row so it records the real identity;
    identity RESOLUTION goes through `lifecycle_service.resolve_canonical_service_key`
    so there is exactly one implementation of the rule."""
    return lifecycle_service._uuid_from_snapshot(server_id, email)


def _log_warning(message: str, *args, **kwargs) -> None:
    """Best-effort module warning: never let logging break a scan."""
    try:
        logging.getLogger(__name__).warning(message, *args, **kwargs)
    except Exception:
        pass


def _sms_idempotency_key(service_key, generation, notification_kind, cd_hours) -> str:
    """Stable Idempotency-Key for one depletion reminder (window-scoped)."""
    try:
        window = int(cd_hours or 24) * 3600
    except (TypeError, ValueError):
        window = 86400
    return _stable_sms_idempotency_key(service_key, generation, notification_kind, window)


def _stable_sms_idempotency_key(service_key: str, generation, notification_kind: str,
                                window_seconds: int) -> str:
    """Deterministic Idempotency-Key for one depletion reminder.

    The key must be identical across every retry of the SAME logical message,
    otherwise a network blip texts the customer twice. It must also differ
    between two legitimate reminders, otherwise a service would be silenced
    for ever after its first notification. Both hold here because the key
    carries identity + generation + kind + the cooldown window bucket:

        eve:<serviceKey>:<generation>:<notificationKind>:<window>

    The window bucket follows the existing per-state cooldown rule, so the
    next allowed reminder lands in a different bucket and is not deduped."""
    try:
        window = int(window_seconds) or 0
    except (TypeError, ValueError):
        window = 0
    if window <= 0:
        window = 86400
    bucket = int(time.time() // window)
    try:
        gen = int(generation or 0)
    except (TypeError, ValueError):
        gen = 0
    kind = str(notification_kind or 'unknown').strip().lower() or 'unknown'
    raw = f"eve:{service_key}:{gen}:{kind}:{bucket}"
    if len(raw) <= 200:
        return raw
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]
    return f"eve:{kind}:{gen}:{digest}"[:200]


def _invalidation_provider(provider: str | None, cfg: dict) -> str:
    """Which gateway connection can carry a lifecycle invalidation.

    Only the GMweb gateway implements ``POST /send/invalidate``; a
    ``custom_http`` provider is a generic SMS relay with no notification
    ledger, so an invalidation sent there would be a guaranteed 404. Route it to
    the GMweb connection instead, and fall back to the caller's provider only
    when GMweb is not configured -- the outbox then records
    ``gateway_not_configured`` and keeps retrying rather than dropping the
    invalidation."""
    providers = cfg.get('providers') if isinstance(cfg.get('providers'), dict) else {}
    gmweb = providers.get('gmweb') if isinstance(providers.get('gmweb'), dict) else {}
    if (gmweb.get('base_url') or '').strip() and (gmweb.get('api_key') or '').strip():
        return 'gmweb'
    name = str(provider or '').strip().lower()
    return name if name in ('gmweb', 'custom_http') else 'gmweb'


def _invalidate_notifications_via_gmweb(payload: dict, cfg: dict | None = None) -> dict:
    """POST one lifecycle invalidation to the GMweb gateway.

    Contract: ``POST /send/invalidate`` (see ``shared/eve-gmweb-contract-v1.json``).
    EVE asks the gateway to revoke every not-yet-started reminder that carries the
    same ``meta.serviceKey`` while the service generation is still older than
    ``currentGeneration``. The request is idempotent on ``eventId``.

    This function only moves bytes and classifies the outcome: every non-2xx,
    timeout, malformed body and transport error is returned as
    ``{'ok': False, 'reason': ...}`` so the durable outbox keeps the event and
    retries it. It never raises and never decides to drop an invalidation.
    """
    cfg = _get_sms_provider_settings(cfg=cfg)
    out = {'ok': False, 'reason': None, 'status_code': None, 'body': None,
           'retry_after_seconds': None}
    base, base_reason = _gmweb_base(cfg)
    api_key = (cfg.get('api_key') or '').strip()
    if not base or not api_key:
        out['reason'] = base_reason or 'gateway_not_configured'
        return out
    if not isinstance(payload, dict):
        out['reason'] = 'invalid_invalidate_payload'
        return out
    try:
        resp = requests.post(
            f"{base}{gmweb_contract.endpoint_path('post_invalidate')}",
            json=payload,
            headers=gmweb_contract.request_headers(api_key, json_body=True),
            timeout=int(cfg.get('timeout_seconds') or 15),
        )
        out['status_code'] = resp.status_code
        try:
            body = resp.json() if resp.content else {}
        except (TypeError, ValueError):
            body = {}
        out['body'] = body if isinstance(body, dict) else {}
        if resp.status_code in (200, 202):
            out['ok'] = True
            return out
        out['reason'] = _sms_gmweb_error_reason(resp)
        if resp.status_code == 409 and isinstance(body, dict) and body.get('error'):
            # A stale generation is a lost race, not a failure: the newer
            # lifecycle already owns the service, so this invalidation is moot.
            out['reason'] = str(body.get('error'))[:120]
        try:
            raw_retry = resp.headers.get('Retry-After')
            out['retry_after_seconds'] = max(1, int(raw_retry)) if raw_retry else None
        except (TypeError, ValueError):
            pass
        return out
    except Exception as exc:
        out['reason'] = f'gateway_error: {exc}'[:255]
        return out


def _cancel_sms_via_gmweb(reference: str, cfg: dict | None = None) -> dict:
    """Cancel one queued GMweb SMS by requestId/job reference.

    GMweb contract (0.3.23+): POST /send/cancel/:reference. The gateway only
    cancels jobs that have not started yet (waiting/paused/delayed). Active or
    already-completed messages return ok=false/not_cancellable and must continue
    to be reconciled by the normal status poller.
    """
    cfg = _get_sms_provider_settings(cfg=cfg)
    out = {
        'ok': False, 'cancelled': False, 'reason': None, 'status_code': None,
        'status': None, 'state': None, 'terminal': None, 'successful': None,
    }
    base, base_reason = _gmweb_base(cfg)
    api_key = (cfg.get('api_key') or '').strip()
    ref = (reference or '').strip()
    if not base or not api_key:
        out['reason'] = base_reason or 'gateway_not_configured'
        return out
    if not ref:
        out['reason'] = 'missing_reference'
        return out
    try:
        resp = requests.post(
            f"{base}{gmweb_contract.endpoint_path('send_cancel', requestId=ref)}",
            headers=gmweb_contract.request_headers(api_key),
            timeout=min(int(cfg.get('timeout_seconds') or 15), 5),
        )
        out['status_code'] = resp.status_code
        try:
            body = resp.json() if resp.content else {}
        except Exception:
            body = {}
        if isinstance(body, dict):
            out['status'] = body.get('status')
            out['state'] = body.get('state')
            if body.get('terminal') is not None:
                out['terminal'] = bool(body.get('terminal'))
            if body.get('successful') is not None:
                out['successful'] = bool(body.get('successful'))
            out['reason'] = body.get('reason') or body.get('error') or body.get('message')
            ok_value = bool(body.get('ok') if body.get('ok') is not None else body.get('success'))
            status_value = str(body.get('status') or body.get('state') or '').strip().lower()
            out['ok'] = ok_value
            out['cancelled'] = (
                resp.status_code in (200, 202)
                and ok_value
                and status_value == 'cancelled'
            )
        elif resp.status_code in (200, 202):
            out['ok'] = True
            out['cancelled'] = True
            out['status'] = 'cancelled'
            out['state'] = 'cancelled'
            out['terminal'] = True
    except Exception as exc:
        out['reason'] = f'gateway_error: {exc}'
    if not out.get('reason') and not out.get('cancelled') and out.get('status_code') not in (200, 202):
        out['reason'] = f"http_{out.get('status_code')}"
    return out


STALE_ACCOUNT_SMS_STATES = ('near_expiry', 'low_volume', 'expired', 'ended', 'royalty')


def _cancel_pending_sms_for_account(server_id, email: str, *, reason: str = 'client_disabled',
                                    states: tuple[str, ...] | list[str] | set[str] | None = None) -> dict:
    """Best-effort SMS queue cleanup when an operator disables an account.

    Removes Eve-local delayed SMS rows and asks GMweb to cancel every non-terminal
    accepted send for the same (server_id, email). This is intentionally
    best-effort: disabling the x-ui account must not fail just because the SMS
    gateway is down or a queued SMS already became active.
    """
    from app import app  # deferred: app-level helper, avoids circular import
    email_l = (email or '').strip().lower()
    if not email_l:
        return {'local_cancelled': 0, 'gateway_cancelled': 0, 'not_cancellable': 0, 'failed': 0}
    try:
        sid_norm = int(server_id or 0)
    except (TypeError, ValueError):
        sid_norm = 0

    result = {'local_cancelled': 0, 'gateway_cancelled': 0, 'not_cancellable': 0, 'failed': 0}
    state_filter = None
    if states is not None:
        state_filter = {
            str(s or '').strip().lower()
            for s in states
            if str(s or '').strip()
        }

    # Eve-local queue: messages parked during quiet hours have not reached GMweb
    # yet, so deleting them is a true cancel.
    try:
        local_q = PendingSms.query.filter(
            PendingSms.server_id == sid_norm,
            func.lower(PendingSms.email) == email_l,
        )
        if state_filter:
            local_q = local_q.filter(func.lower(PendingSms.event_name).in_(state_filter))
        local_rows = local_q.all()
        for row in local_rows:
            _sms_log_row(None, email_l, sid_norm, row.server_name, row.event_name,
                         row.recipient, 'cancelled', f'{reason}: local_pending')
            db.session.delete(row)
            result['local_cancelled'] += 1
        if local_rows:
            db.session.commit()
    except Exception as exc:
        db.session.rollback()
        result['failed'] += 1
        try:
            app.logger.warning('[sms-cancel] local pending cleanup failed for %s/%s: %s',
                               sid_norm, email_l, exc)
        except Exception:
            pass

    cfg = _get_sms_runtime_settings()
    if not (cfg.get('base_url') and cfg.get('api_key')):
        return result

    try:
        q = SmsSendLog.query.filter(
            SmsSendLog.server_id == sid_norm,
            func.lower(SmsSendLog.email) == email_l,
            SmsSendLog.request_id.isnot(None),
            or_(SmsSendLog.terminal.is_(False), SmsSendLog.terminal.is_(None)),
            ~SmsSendLog.status.in_(
                ('failed', 'skipped', 'cancelled', 'delivered', 'completed',
                 'superseded', 'revoked', 'suppressed')),
        )
        if state_filter:
            q = q.filter(func.lower(SmsSendLog.state).in_(state_filter))
        rows = q.order_by(SmsSendLog.created_at.asc()).limit(100).all()
    except Exception as exc:
        result['failed'] += 1
        try:
            app.logger.warning('[sms-cancel] pending lookup failed for %s/%s: %s',
                               sid_norm, email_l, exc)
        except Exception:
            pass
        return result

    for row in rows:
        reference = (row.request_id or row.gateway_job_id or '').strip()
        if not reference:
            continue
        res = _cancel_sms_via_gmweb(
            reference, _get_sms_provider_settings(row.gateway_provider, cfg))
        now = datetime.utcnow()
        try:
            if res.get('cancelled'):
                row.status = 'cancelled'
                row.gateway_state = str(res.get('state') or 'cancelled')[:32]
                row.stage = 'cancelled_by_eve'
                row.terminal = True
                row.successful = False
                row.reason = str(reason)[:255]
                row.updated_at = now
                result['gateway_cancelled'] += 1
                try:
                    _sms_refund_daily_segments(int(row.segment_count or 1))
                except Exception:
                    pass
            else:
                row.reason = str(f"cancel_not_cancellable: {res.get('reason') or 'unknown'}")[:255]
                row.updated_at = now
                if (res.get('reason') or '').lower() in ('not_cancellable', 'already_active'):
                    result['not_cancellable'] += 1
                else:
                    result['failed'] += 1
        except Exception:
            result['failed'] += 1

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        result['failed'] += 1
        try:
            app.logger.warning('[sms-cancel] commit failed for %s/%s: %s',
                               sid_norm, email_l, exc)
        except Exception:
            pass
    return result


def _cancel_stale_account_sms(server_id, email: str, *, reason: str) -> dict:
    """Cancel obsolete account-state SMS after a successful create/renew.

    A renewal or same-email recreation makes old depletion/royalty alerts wrong,
    but must not cancel the fresh transactional `renew`/`created` confirmation
    that is about to be queued.
    """
    return _cancel_pending_sms_for_account(
        server_id, email, reason=reason, states=STALE_ACCOUNT_SMS_STATES,
    )


def _fire_cancel_stale_account_sms(server_id, email: str, *, reason: str,
                                   client_uuid=None, event_type='renewal',
                                   operation_id=None, correlation_id=None,
                                   client=None) -> None:
    """Mirror a successful renewal into the notification lifecycle. Async.

    This is the single chokepoint every successful renewal path reaches, and it
    does the whole post-renewal notification job:

    1. advance the DURABLE service generation (the cross-worker barrier that stops
       another worker from submitting a reminder classified before the renewal);
    2. record the invalidation in the durable outbox and commit it with the
       generation, so a gateway outage or a process restart cannot lose it;
    3. ask the gateway immediately to revoke already-queued old reminders, in
       parallel with the best-effort per-message cancel below;
    4. best-effort local cancel of the specific rows EVE still owns.

    It runs off the request path on purpose: the customer's renewal must never
    depend on the SMS gateway being reachable, and gateway cancellation costs one
    HTTP request per queued message. Note the ordering guarantee -- this is only
    ever called AFTER the panel write was verified, so a failed renewal can never
    suppress a legitimate depletion reminder.
    """
    from app import app  # deferred: Flask instance lives in app.py

    identity = client_uuid
    if not identity and isinstance(client, dict):
        identity = lifecycle_service.resolve_client_uuid(client)
    if not identity:
        # Fallback identity when a path only knows the email (bulk repair jobs).
        # The email is a weak identity, which is exactly why it is the *fallback*
        # and not the primary key.
        identity = lifecycle_service.client_uuid_from_email(email)

    def _worker():
        with app.app_context():
            try:
                lifecycle_result = lifecycle_service.handle_successful_service_lifecycle_change(
                    server_id=server_id,
                    client_uuid=identity,
                    client_email=email,
                    event_type=event_type,
                    operation_id=operation_id,
                    correlation_id=correlation_id,
                    reason=reason,
                )
                if lifecycle_result.get('error'):
                    app.logger.error(
                        '[lifecycle] generation advance failed for %s: %s',
                        lifecycle_result.get('service_key'),
                        lifecycle_result.get('error'),
                    )
            except Exception:
                app.logger.exception(
                    '[lifecycle] asynchronous lifecycle change failed for %s/%s',
                    server_id, email,
                )
            try:
                _cancel_stale_account_sms(server_id, email, reason=reason)
            except Exception:
                app.logger.exception(
                    '[sms-cancel] asynchronous stale cleanup failed for %s/%s',
                    server_id, email,
                )

    threading.Thread(target=_worker, daemon=True,
                     name='eve-renew-lifecycle').start()


def _sms_accepted_status(send_result: dict) -> str:
    """A new gateway acceptance is queued; old gateways had no status API."""
    if send_result.get('manual_review'):
        return 'manual_review'
    if send_result.get('terminal') and send_result.get('successful'):
        return 'sent'
    if send_result.get('request_id'):
        status = str(send_result.get('status') or 'queued').strip().lower()
        return ('queued' if status == 'deferred' else status)[:16]
    return 'sent'


def _sms_has_manual_review(email: str, server_id, state: str) -> bool:
    """Never auto-resend a logical SMS whose prior GMweb result was unverified."""
    try:
        return SmsSendLog.query.filter_by(
            email=str(email or '').strip().lower(),
            server_id=int(server_id or 0),
            state=str(state or '').strip().lower(),
            status='manual_review',
        ).first() is not None
    except Exception:
        return False


def _sms_should_send(event_name: str, server_id, email: str, cfg: dict | None = None) -> tuple[bool, str | None]:
    cfg = cfg or _get_sms_runtime_settings()
    if not cfg.get('enabled'):
        return False, 'feature_disabled'
    if cfg.get(f'trigger_{event_name}') is False:
        return False, 'trigger_disabled'
    if not (cfg.get('base_url') and cfg.get('api_key')):
        return False, 'gateway_not_configured'
    # The core rule: never SMS a reseller-owned account from the system number.
    if _account_has_reseller_owner(server_id, email):
        return False, 'reseller_owned'
    return True, None


def _get_sms_template_content(template_type: str, default_text: str) -> str:
    try:
        t = NotificationTemplate.query.filter_by(type=template_type, is_active=True).first()
        if t and (t.content or '').strip():
            return t.content
    except Exception:
        pass
    return default_text


def _fire_automation_sms(event_name: str, server_id, email: str, template_type: str,
                         default_template: str, tpl_vars: dict, recipient_comment: str = '',
                         server_name: str = '') -> None:
    """Render the SMS template and send it via GMweb in a background thread, so
    the create/renew request is never blocked. Gated to non-reseller accounts.
    Every outcome (sent / failed / skipped+reason) is written to the SMS send log
    so transactional create/renew messages are visible there, not just the scan."""
    from app import _render_text_template, _send_sms_via_gmweb, app  # deferred: app-level helper, avoids circular import
    def _worker():
        with app.app_context():
            try:
                sid_norm = None
                try:
                    sid_norm = int(server_id)
                except (TypeError, ValueError):
                    sid_norm = None
                email_l = (email or '').strip().lower()

                def _log(recipient, status, reason, gateway_result=None, lifecycle=None):
                    _sms_log_row(None, email_l, sid_norm, server_name, event_name,
                                 recipient, status, reason, gateway_result, lifecycle)

                # The notification identity this confirmation belongs to. The
                # generation is read (not advanced) here: the renewal itself already
                # advanced it on the request path.
                #
                # The serviceKey MUST be the canonical one: resolving it from the
                # email while a UUID-based lifecycle row exists reads a different (or
                # missing) generation, which is how the payload used to go out with
                # `generation: null` and the gateway answered HTTP 400
                # invalid_meta/meta_generation_required.
                service_key = lifecycle_service.resolve_canonical_service_key(
                    sid_norm, email=email)
                generation_state = lifecycle_service.read_or_create_generation(
                    service_key, server_id=sid_norm, client_email=email_l,
                    client_uuid=_service_uuid_for(sid_norm, email),
                    reason='%s_confirmation' % event_name)
                generation = generation_state.get('generation')
                if not generation_state.get('established'):
                    # Fail closed: a database problem is not generation 0. A missing
                    # confirmation is recoverable; the wrong lifecycle on a real
                    # message is not.
                    _log('', 'skipped', 'generation_unavailable:%s'
                         % (generation_state.get('reason') or 'unknown'))
                    return
                audit = _lifecycle_audit(
                    service_key, generation,
                    lifecycle_service.new_correlation_id(), None,
                    generation_state.get('last_lifecycle_change_at'))

                cfg = _get_sms_runtime_settings()
                ok, reason = _sms_should_send(event_name, server_id, email, cfg)
                if not ok:
                    _log('', 'skipped', reason)
                    return
                if _sms_comment_opted_out(recipient_comment):
                    _log('', 'skipped', 'opted_out')
                    return
                recipient = _extract_iran_mobile_from_text(email, recipient_comment or None)
                if not recipient:
                    _log('', 'skipped', 'no_recipient')
                    return
                if _sms_has_manual_review(email_l, sid_norm, event_name):
                    _log(recipient, 'skipped', 'manual_review_pending')
                    return
                # Dedup: v3.4 node panels can take 10-18s to apply a client update,
                # so a slow/timed-out request gets retried by the operator while the
                # first renew already succeeded — which used to text the customer
                # 4-5 times. Skip if the SAME transactional SMS was already sent to
                # this account in the last few minutes.
                try:
                    _dup = SmsSendLog.query.filter(
                        SmsSendLog.email == email_l,
                        SmsSendLog.state == event_name,
                        SmsSendLog.status == 'sent',
                        SmsSendLog.created_at >= (datetime.utcnow() - timedelta(seconds=300)),
                    ).first()
                    if _dup:
                        _log(recipient, 'skipped', 'duplicate_suppressed')
                        return
                except Exception:
                    pass
                content = _get_sms_template_content(template_type, default_template)
                if _template_wants_recommendation(content):
                    tpl_vars.update(_recommendation_template_vars(
                        sid_norm, tpl_vars.get('_sub_id'), email,
                        terminal=event_name in ('expired', 'ended'),
                    ))
                text = _render_text_template(content, tpl_vars)
                if not (text or '').strip():
                    _log(recipient, 'skipped', 'empty_message')
                    return
                # NOTE: quiet hours intentionally do NOT apply here. Create/renew are
                # user-triggered confirmations and must go out immediately, even at
                # night. Only the bulk depletion scan honours the quiet window.
                # Final fail-closed check after queueing/rate gates. The comment
                # may have changed since this background thread was created.
                if _sms_account_opted_out(server_id, email, recipient_comment, refresh_shared=True):
                    _log(recipient, 'skipped', 'opted_out_recheck')
                    return
                segment_info = _sms_segment_info(text)
                segments = segment_info['sms_segments']
                slot_ok, slot_reason = _sms_take_send_slot(
                    recipient, cfg, segments, priority=event_name)
                if not slot_ok:
                    _log(recipient, 'skipped', slot_reason, segment_info)
                    return
                # Transactional create/renew: CRITICAL so the customer who just
                # paid gets their confirmation next, ahead of any running bulk scan.
                # Stable idempotency key per fire so a network-retry can't double-send.
                idem = f"tx-{event_name}-{sid_norm}-{email_l}-{int(time.time())}"
                # requiresValidation=False: a create/renew confirmation is
                # transactional and is NEVER revoked by a lifecycle invalidation.
                tx_meta = _transactional_notification_meta(
                    service_key, event_name, generation=generation,
                    correlation_id=audit.get('correlationId'))
                res = _send_sms_via_gmweb(
                    recipient, text, cfg,
                    priority=_gmweb_sms_priority(event_name), idempotency_key=idem,
                    meta=tx_meta)
                audit = _lifecycle_audit(
                    service_key, audit.get('generation'),
                    tx_meta.get('correlationId'), None, audit.get('lastLifecycleChangeAt'),
                    idempotency_key=idem)
                if res.get('sent'):
                    _log(recipient, _sms_accepted_status(res), None, res, audit)
                elif res.get('manual_review'):
                    _log(recipient, 'manual_review', 'unverified_manual_review', res, audit)
                else:
                    _sms_refund_daily_segments(segments)
                    _log(recipient, 'failed', res.get('reason'), res, audit)
            except Exception:
                app.logger.exception('[sms-automation] send failed')
    threading.Thread(target=_worker, daemon=True).start()


# Monitor service-state tags ↔ granular SMS trigger/state names. The tag side
# matches get_monitor_alerts() and the template-key side matches the textareas
# in Monitor → Message Templates, so the SMS uses the operator's own wording.
SMS_MONITOR_TAG_TO_STATE = {'soon': 'near_expiry', 'low': 'low_volume', 'expired': 'expired', 'ended': 'ended'}
SMS_STATE_TO_MONITOR_TPL = {'near_expiry': 'soon', 'low_volume': 'low', 'expired': 'expired', 'ended': 'ended'}
# Send priority across automated SMS. Manual "Start now" can override this order.
# Default: volume running out → volume ended → time ended → time running out.
SMS_STATE_PRIORITY = {'low_volume': 0, 'ended': 1, 'expired': 2, 'near_expiry': 3}
SMS_SCAN_STATES = ('near_expiry', 'low_volume', 'expired', 'ended')
SMS_MANUAL_DEFAULT_STATES = ('low_volume', 'ended', 'expired', 'near_expiry')


def _normalize_sms_scan_states(raw_states) -> list[str]:
    valid = set(SMS_SCAN_STATES)
    out = []
    if isinstance(raw_states, str):
        raw_states = [s.strip() for s in raw_states.split(',')]
    if not isinstance(raw_states, (list, tuple, set)):
        return out
    for state in raw_states:
        s = str(state or '').strip().lower().replace('-', '_')
        if s in valid and s not in out:
            out.append(s)
    return out


def _sms_gateway_ready(cfg: dict | None = None) -> tuple[bool, str | None, int | None]:
    """Return whether GMweb is paired/ready before draining rate-limit budget."""
    cfg = cfg or _get_sms_runtime_settings()
    base = (cfg.get('base_url') or '').strip().rstrip('/')
    api_key = (cfg.get('api_key') or '').strip()
    if not base or not api_key:
        return False, 'gateway_not_configured', None
    try:
        resp = requests.get(
            f"{base}/ready",
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=min(int(cfg.get('timeout_seconds') or 15), 5),
        )
    except Exception as exc:
        return False, f'gateway_ready_failed: {exc}', None
    if resp.status_code == 200:
        return True, None, resp.status_code
    if resp.status_code == 401:
        return False, 'gateway_auth_failed', resp.status_code
    if resp.status_code == 503:
        return False, 'gateway_not_paired', resp.status_code
    return False, f'gateway_not_ready_http_{resp.status_code}', resp.status_code


def _mask_mobile(num: str | None) -> str:
    digits = re.sub(r'\D', '', str(num or ''))
    if len(digits) >= 7:
        return f"{digits[:4]}***{digits[-3:]}"
    return str(num or '')


def _render_monitor_state_template(tpl: str | None, mvars: dict) -> str:
    """Render a Monitor state template with shared conditional semantics."""
    from app import _render_text_template  # deferred: app-level helper, avoids circular import
    values = dict(mvars or {})
    for key in ('user', 'rem', 'time', 'date', 'server'):
        if values.get(key) in (None, ''):
            values[key] = '-'
    return _render_text_template(tpl, values)


def _sms_scan_persist_locked():
    """Mirror the in-memory scan job to Redis so other workers can read live
    progress. Caller holds SMS_SCAN_JOB_LOCK. Best-effort, no-op without Redis."""
    client = get_redis()
    if client is None:
        return
    try:
        client.set(SMS_SCAN_REDIS_KEY,
                   json.dumps(SMS_SCAN_JOB, default=str).encode('utf-8'),
                   ex=SMS_SCAN_REDIS_TTL)
    except Exception:
        pass


def _sms_scan_snapshot() -> dict:
    """Current scan job as seen across all workers: Redis copy if present, else
    this worker's in-memory dict."""
    client = get_redis()
    if client is not None:
        try:
            blob = client.get(SMS_SCAN_REDIS_KEY)
            if blob:
                return json.loads(blob)
        except Exception:
            pass
    with SMS_SCAN_JOB_LOCK:
        return dict(SMS_SCAN_JOB)


def _sms_scan_set(**patch):
    with SMS_SCAN_JOB_LOCK:
        SMS_SCAN_JOB.update(patch)
        _sms_scan_persist_locked()


def _sms_scan_inc(field: str, n: int = 1):
    with SMS_SCAN_JOB_LOCK:
        SMS_SCAN_JOB[field] = int(SMS_SCAN_JOB.get(field, 0) or 0) + n
        _sms_scan_persist_locked()




def _sms_scan_cancel_clear():
    SMS_SCAN_CANCEL.clear()
    client = get_redis()
    if client is not None:
        try:
            client.delete(SMS_SCAN_CANCEL_REDIS_KEY)
        except Exception:
            pass


def _sms_scan_cancelled() -> bool:
    if SMS_SCAN_CANCEL.is_set():
        return True
    client = get_redis()
    if client is not None:
        try:
            return client.get(SMS_SCAN_CANCEL_REDIS_KEY) is not None
        except Exception:
            return False
    return False


def _classify_monitor_status(*, enabled: bool, total_bytes: int, remaining_bytes, remaining_gb,
                             expiry_ts: int, expiry_info: dict, warning_days: int, warning_gb: float) -> str | None:
    """Return the Monitor status tag ('ended'|'low'|'expired'|'soon'|'disabled'|None)
    using the SAME precedence as get_monitor_alerts(). Keep in sync with it."""
    status = None
    status_rank = -1
    if total_bytes > 0 and remaining_bytes is not None:
        if remaining_bytes <= 0:
            status, status_rank = 'ended', 3
        elif remaining_gb is not None and remaining_gb < warning_gb:
            status, status_rank = 'low', 2
    if expiry_ts and expiry_info.get('type') == 'expired':
        if status_rank < 4:
            status, status_rank = 'expired', 4
    elif expiry_ts and expiry_info.get('type') in ('today', 'soon'):
        if int(expiry_info.get('days') or 0) <= warning_days and status_rank < 1:
            status, status_rank = 'soon', 1
    if not enabled and status in (None, 'low', 'soon'):
        status = 'disabled'
    return status


def _client_snapshot_times(client: dict) -> tuple:
    """(config_updated_at, telemetry_updated_at) for one cached client row.

    The refresh pipeline stamps both when it writes a row (see
    ``panel/jobs/refresh.py``); a renewal patch stamps only the config side, a
    traffic poll only the telemetry side. Either timestamp is a lower bound for
    \"when did EVE last see this service at all\", which is what the stale-
    snapshot guard needs."""
    config_at = telemetry_at = None
    if not isinstance(client, dict):
        return config_at, telemetry_at
    for key, target in (('config_updated_at', 'config'), ('telemetry_updated_at', 'telemetry')):
        raw = client.get(key)
        if not raw:
            continue
        try:
            moment = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            moment = None
        if moment is None:
            continue
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        if target == 'config':
            config_at = moment
        else:
            telemetry_at = moment
    return config_at, telemetry_at


def _snapshot_observation_time(client: dict):
    """The freshest moment EVE observed this client, or None when unknown."""
    config_at, telemetry_at = _client_snapshot_times(client)
    candidates = [t for t in (config_at, telemetry_at) if t is not None]
    if not candidates:
        return None
    return max(candidates)


def _cached_snapshot_clients(server_id, email: str):
    """Every cached copy of (server_id, email) with its observation time.

    A v3 client attached to several inbounds appears more than once on purpose:
    the existing fail-closed recheck already treats disagreeing copies as
    \"cannot prove the state\"."""
    try:
        sid = int(server_id)
    except (TypeError, ValueError):
        sid = server_id
    email_l = (email or '').strip().lower()
    found = []
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id', -1)) != int(sid):
                continue
        except (TypeError, ValueError):
            if inbound.get('server_id') != sid:
                continue
        for client in (inbound.get('clients') or []):
            if (client.get('email') or '').strip().lower() != email_l:
                continue
            found.append(client)
    return found


def _snapshot_is_fresh_for_candidate(server_id, email: str, last_change_at):
    """Can this snapshot still prove the service is depleted?

    A snapshot observed at or before the last successful lifecycle change simply
    cannot: it was taken when the pre-renewal state was true. This is the RACE B
    guard -- even with every queued job cancelled correctly, a worker reading a
    stale cache would otherwise invent a brand-new \'volume ended\' reminder for a
    service the customer already renewed.

    Returns (fresh, observed_at, reason). Unknown timestamps fail closed.
    """
    if last_change_at is None:
        clients = _cached_snapshot_clients(server_id, email)
        observed = None
        for client in clients:
            moment = _snapshot_observation_time(client)
            if moment is not None and (observed is None or moment > observed):
                observed = moment
        return True, observed, ''
    clients = _cached_snapshot_clients(server_id, email)
    if not clients:
        return False, None, 'snapshot_missing_recheck'
    newest = None
    for client in clients:
        moment = _snapshot_observation_time(client)
        if moment is None:
            # No observation stamp at all: the row predates the stamping or came
            # from an un-refreshed cache. It cannot prove a post-renewal state.
            return False, None, 'snapshot_time_unknown_recheck'
        if newest is None or moment > newest:
            newest = moment
    if newest is None or newest <= last_change_at:
        return False, newest, 'snapshot_predates_lifecycle_change'
    return True, newest, ''


def _sms_depletion_state_still_valid(server_id, email: str, expected_state: str,
                                     cfg: dict, *, service_key: str | None = None,
                                     expected_generation=None,
                                     last_lifecycle_change_at=None) -> tuple[bool, str]:
    """Fail closed when a queued depletion candidate no longer matches live state.

    A scan may spend minutes behind pacing or the GMweb queue, and during that
    wait the customer can renew. The real body of this function lives further down
    this module (next to the classifier helpers it uses); this stub exists only so
    the helpers above can be defined in a readable order.
    """


_SNAPSHOT_REFRESHABLE_REASONS = (
    'snapshot_predates_lifecycle_change',
    'snapshot_time_unknown_recheck',
    'snapshot_missing_recheck',
)


def _lifecycle_audit(service_key, generation, correlation_id, observed_at,
                     last_change_at, idempotency_key=None, gateway_outcome=None,
                     revocation_reason=None, revoked_at=None) -> dict:
    """The audit bundle written next to every scan attempt.

    Deliberately carries no SMS body: the operator needs to know WHICH lifecycle
    a message belonged to, not what it said."""
    return {
        'serviceKey': service_key,
        'generation': generation,
        'correlationId': correlation_id,
        'observedAt': observed_at if isinstance(observed_at, datetime) else None,
        'lastLifecycleChangeAt': (
            last_change_at if isinstance(last_change_at, datetime) else None),
        'idempotencyKey': idempotency_key,
        # Gateway verdict for a revoked reminder (see _refresh_pending_sms_statuses).
        'gatewayOutcome': gateway_outcome,
        'revocationReason': revocation_reason,
        'revokedAt': revoked_at,
    }


def _depletion_notification_meta(service_key, generation, notification_kind, *,
                                 correlation_id=None, last_change_at=None) -> dict:
    """The contract meta block for an automated depletion reminder.

    ``generation`` must be a real durable integer: this helper is never called with
    None, and a caller that cannot establish one must skip the reminder instead of
    posting meta the gateway will reject (invalid_meta/meta_generation_required).

    ``requiresValidation`` is True: this message is a claim about live state and
    may be revoked by a renewal. Transactional created/renew confirmations set it
    to False so an invalidation can never cancel them."""
    if generation is None:
        raise ValueError('depletion_notification_meta_requires_a_durable_generation')
    return {
        'source': 'eve',
        'serviceKey': service_key,
        'notificationKind': notification_kind,
        'generation': int(generation),
        'correlationId': correlation_id or lifecycle_service.new_correlation_id(),
        'requiresValidation': True,
    }


def _transactional_notification_meta(service_key, event_name, generation,
                                      correlation_id=None) -> dict:
    """The contract meta block for a create/renew confirmation.

    ``generation`` is REQUIRED and has no default: the gateway refuses a
    meta-aware send that omits it (HTTP 400 invalid_meta /
    meta_generation_required), and a default of None is exactly how that payload
    used to be produced. Callers resolve it from the durable lifecycle row (a
    baseline 0 is created through the lifecycle helper when the service predates
    generations) and must skip the send if they cannot.

    ``requiresValidation`` is False and the kind is ``created``/``renew``, which
    keeps the message OUT of every lifecycle invalidation: a customer who just
    paid must still receive the confirmation that their renewal worked."""
    if generation is None:
        raise ValueError('transactional_notification_meta_requires_a_generation')
    return {
        'source': 'eve',
        'serviceKey': service_key,
        'notificationKind': lifecycle_service.sms_notification_kind(event_name),
        'generation': int(generation),
        'correlationId': correlation_id or lifecycle_service.new_correlation_id(),
        'requiresValidation': False,
    }


def _targeted_candidate_refresh(server_id, email: str) -> tuple[bool, object]:
    """One read-only panel read for ONE client, when the cache is too old.

    The depletion scan classifies thousands of rows from the shared snapshot;
    refreshing the whole panel for each of them is not affordable. This runs only
    for a candidate that is about to be sent and whose cached copy cannot prove a
    post-renewal state, and it touches a single server.

    Returns (refreshed, observed_at). A failure returns (False, None) and the
    caller then skips the message -- a missed reminder beats a wrong one."""
    try:
        from app import fetch_and_update_global_data  # deferred: app-level helper
    except Exception:
        return False, None
    try:
        fetched = fetch_and_update_global_data(force=True, server_ids=[int(server_id)])
    except Exception:
        _log_warning('[sms-scan] targeted refresh failed for %s', server_id,
                     exc_info=True)
        return False, None
    if not fetched:
        return False, None
    observed = None
    for client in _cached_snapshot_clients(server_id, email):
        moment = _snapshot_observation_time(client)
        if moment is not None and (observed is None or moment > observed):
            observed = moment
    return (observed is not None), observed


def _classify_cached_client_state(client: dict, cfg: dict):
    """Monitor status tag for one cached client row -> SMS state (or None).

    One implementation shared by the scan and the pre-send recheck so the two
    can never disagree about what \"depleted\" means."""
    from app import format_remaining_days  # deferred compatibility export
    total_bytes = int(client.get('totalGB') or 0)
    try:
        used = int(client.get('up') or 0) + int(client.get('down') or 0)
    except (TypeError, ValueError):
        used = 0
    remaining_bytes = client.get('remaining_bytes')
    if remaining_bytes is None or remaining_bytes == -1:
        remaining_bytes = max(total_bytes - used, 0) if total_bytes > 0 else None
    remaining_gb = (
        float(remaining_bytes) / (1024 ** 3)
        if remaining_bytes is not None else None
    )
    expiry_ts = int(client.get('expiryTimestamp') or 0)
    status = _classify_monitor_status(
        enabled=bool(client.get('enable', True)),
        total_bytes=total_bytes,
        remaining_bytes=remaining_bytes,
        remaining_gb=remaining_gb,
        expiry_ts=expiry_ts,
        expiry_info=format_remaining_days(expiry_ts),
        warning_days=int(cfg.get('depletion_expiry_days', 3)),
        warning_gb=float(cfg.get('depletion_volume_gb', 2.0)),
    )
    return SMS_MONITOR_TAG_TO_STATE.get(status or '')


def _sms_depletion_state_still_valid(server_id, email: str, expected_state: str,
                                     cfg: dict, *, service_key: str | None = None,
                                     expected_generation=None,
                                     last_lifecycle_change_at=None) -> tuple[bool, str]:
    """Fail closed when a queued depletion candidate no longer matches live state.

    A scan may spend minutes behind pacing or the GMweb queue, and during that
    wait the customer can renew. Three independent barriers run here, in order:

    1. GENERATION BARRIER (durable, cross-worker). Re-read the service's
       generation straight from the database. If it moved since the candidate
       was classified, the candidate describes a lifecycle that no longer
       exists -- drop it. This holds even when a DIFFERENT process performed the
       renewal, which is exactly the gunicorn-worker-A/B case.
    2. SNAPSHOT-AGE BARRIER. A snapshot observed at or before the last
       successful lifecycle change cannot prove the service is depleted, so it
       is never accepted as evidence.
    3. STATE RECHECK. Reload the shared snapshot and recompute the exact state;
       if the account disappeared or duplicate cached copies disagree, suppress.

    Returns (ok, reason). The caller decides what to do when only the snapshot
    is too old (it may retry with a targeted refresh); this never sends on doubt."""
    try:
        load_snapshot_from_redis()
    except Exception:
        pass

    if service_key:
        try:
            from panel.services import lifecycle as lifecycle_service
            current = lifecycle_service.generation_state(service_key)
        except Exception:
            current = None
        if current is not None:
            live_generation = current.get('generation')
            if expected_generation is None and live_generation is not None:
                return False, 'generation_unknown_recheck'
            if (expected_generation is not None and live_generation is not None
                    and int(live_generation) != int(expected_generation)):
                return False, 'generation_changed_recheck'
            changed_at = (current.get('last_lifecycle_change_at')
                          or last_lifecycle_change_at)
            fresh, _observed, fresh_reason = _snapshot_is_fresh_for_candidate(
                server_id, email, changed_at)
            if not fresh:
                return False, fresh_reason

    observed_states = []
    for client in _cached_snapshot_clients(server_id, email):
        observed_states.append(_classify_cached_client_state(client, cfg))

    if not observed_states:
        return False, 'account_missing_recheck'
    if any(state != expected_state for state in observed_states):
        return False, 'state_changed_recheck'
    return True, ''


_DEPLETION_TRIGGER_KEYS = {
    'near_expiry': 'trigger_near_expiry',
    'low_volume': 'trigger_low_volume',
    'expired': 'trigger_expired',
    'ended': 'trigger_ended',
}


def _server_display_name(server_id) -> str:
    """The server label the templates render, read from the shared snapshot."""
    try:
        for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
            if int(inbound.get('server_id') or -1) == int(server_id):
                return inbound.get('server_name') or ''
    except Exception:
        pass
    return ''


def _sms_quiet_seconds_remaining(cfg: dict, now_utc=None) -> int:
    """Seconds until the quiet window closes, so the event is DEFERRED not lost."""
    if not _sms_in_quiet_hours(cfg, now_utc):
        return 0
    start = int(cfg.get('quiet_start', 0)) % 24
    end = int(cfg.get('quiet_end', 0)) % 24
    hour = _tehran_hour(now_utc)
    if start < end:
        remaining_hours = end - hour
    elif hour >= start:
        remaining_hours = 24 - hour + end
    else:
        remaining_hours = end - hour
    return max(60, int(remaining_hours * 3600))


def _sms_budget_deferral_seconds(reason: str) -> int:
    """Seconds until the hourly/daily budget that refused this send is refilled."""
    now = datetime.utcnow()
    if reason == 'hourly_limit_reached':
        return max(60, 3660 - ((now.minute * 60) + now.second))
    return max(60, 86700 - ((now.hour * 3600) + (now.minute * 60) + now.second))


def _depletion_event_mvars(event, row: dict, cfg: dict) -> dict:
    """Template variables rebuilt from the LIVE cached row, never from the event.

    The event carries identity and a state version -- not a message. Rebuilding the
    message at delivery time from the row the dashboard is rendering is what keeps
    "what the customer was told" and "what the panel shows" the same claim."""
    from app import format_jalali  # deferred compatibility export
    expiry_ts = int(row.get('expiryTimestamp') or 0)
    expiry_date = None
    if expiry_ts and expiry_ts > 0:
        try:
            expiry_date = format_jalali(datetime.utcfromtimestamp(expiry_ts / 1000))
        except Exception:
            expiry_date = None
    return {
        'user': event.client_email or (row.get('email') or ''),
        'rem': row.get('remaining_formatted') or 'Unlimited',
        'time': row.get('expiryTime') or '-',
        'date': expiry_date or '-',
        'server': _server_display_name(event.server_id),
        '_sub_id': row.get('subId') or row.get('id') or '',
    }


def _deliver_depletion_event(event, *, cfg, templates, cooldown_hours, job_id, shadow):
    """Deliver ONE outbox event. Returns (outcome, stop_draining).

    Every gate the scan used is applied here, in the same order, and for the same
    reason: the pipeline changes WHO detects a transition and WHEN it is sent, not
    what is allowed to be sent. Nothing is posted on doubt -- a state that cannot be
    proven live is deferred or superseded, never guessed.
    """
    from app import format_remaining_days, _utc_iso_now  # deferred compatibility exports
    sid = int(event.server_id or 0)
    email = (event.client_email or '').strip()
    email_l = email.lower()
    # The event stores the CANONICAL state; every gate below (per-state trigger,
    # cooldown hours, template, manual review, priority) is configured in the SMS
    # monitor's vocabulary. Translating here -- once -- is what keeps the operator's
    # settings meaningful for a transition the pipeline detected.
    state = telemetry_state.sms_state_for(event.state) or str(event.state or '')
    jid = job_id or ('evt-%s' % event.event_id[:24])
    if not email_l:
        telemetry_state.mark_skipped(event, 'no_identity_email')
        return 'skipped', False
    if not cfg.get('enabled'):
        telemetry_state.mark_skipped(event, 'sms_disabled', retry_in=600)
        return 'deferred', False
    trigger_key = _DEPLETION_TRIGGER_KEYS.get(state)
    if trigger_key and not cfg.get(trigger_key):
        telemetry_state.mark_skipped(event, 'trigger_disabled:%s' % state, retry_in=900)
        return 'deferred', False
    if _account_has_reseller_owner(sid, email_l):
        telemetry_state.mark_skipped(event, 'reseller_owned')
        return 'skipped', False

    # FENCE 1 -- the lifecycle generation. A renewal that landed while this event
    # sat in the queue invalidates the claim it makes.
    current = {}
    try:
        current = lifecycle_service.generation_state(event.service_key) or {}
    except Exception:
        current = {}
    live_generation = current.get('generation')
    generation = int(event.lifecycle_generation or 0)
    if live_generation is not None and generation and int(live_generation) != generation:
        telemetry_state.mark_superseded(event, 'lifecycle_generation_advanced')
        return 'superseded', False
    last_change_at = current.get('last_lifecycle_change_at')
    if not generation:
        established = lifecycle_service.read_or_create_generation(
            event.service_key, server_id=sid, client_email=email_l,
            client_uuid=event.client_uuid or _service_uuid_for(sid, email_l),
            reason='depletion_event_baseline')
        if not established.get('established'):
            telemetry_state.mark_skipped(
                event, 'generation_unavailable:%s' % (established.get('reason') or 'unknown'),
                retry_in=300)
            return 'deferred', False
        generation = int(established.get('generation') or 0)
        event.lifecycle_generation = generation
        last_change_at = established.get('last_lifecycle_change_at') or last_change_at

    # FENCE 2 -- live state, recomputed from the shared snapshot with the ONE
    # classifier both sides use. Duplicate cached copies that disagree suppress.
    rows = _cached_snapshot_clients(sid, email_l)
    if not rows:
        telemetry_state.mark_skipped(event, 'account_missing')
        return 'skipped', False
    if any(_classify_cached_client_state(row, cfg) != state for row in rows):
        telemetry_state.mark_superseded(event, 'state_changed_before_send')
        return 'superseded', False
    valid, reason = _sms_depletion_state_still_valid(
        sid, email_l, state, cfg, service_key=event.service_key,
        expected_generation=generation, last_lifecycle_change_at=last_change_at)
    if not valid and reason in _SNAPSHOT_REFRESHABLE_REASONS:
        # The cached copy is merely too old to prove anything: one targeted,
        # read-only panel read for THIS client, then re-check. Never a fan-out.
        refreshed, _observed_at = _targeted_candidate_refresh(sid, email_l)
        if refreshed:
            valid, reason = _sms_depletion_state_still_valid(
                sid, email_l, state, cfg, service_key=event.service_key,
                expected_generation=generation, last_lifecycle_change_at=last_change_at)
    if not valid:
        if reason in _SNAPSHOT_REFRESHABLE_REASONS:
            # Deferred, not dropped: the cached copy is merely too old to PROVE the
            # state, so the event waits for a fresher read instead of being decided
            # on doubt. The audit row is what answers 'why did no SMS go out?'.
            telemetry_state.mark_skipped(event, reason, retry_in=180)
            _sms_log_row(jid, email_l, sid, _server_display_name(sid), state,
                         recipient, 'deferred', reason)
            return 'deferred', False
        telemetry_state.mark_superseded(event, reason)
        return 'superseded', False

    row = rows[0]
    comment = row.get('comment') or ''
    if _sms_account_opted_out(sid, email_l, comment, refresh_shared=True):
        telemetry_state.mark_skipped(event, 'opted_out_recheck')
        return 'skipped', False
    if _sms_has_manual_review(email_l, sid, state):
        telemetry_state.mark_skipped(event, 'manual_review_pending', retry_in=1800)
        return 'deferred', False
    cd_hours = int(cooldown_hours.get(state, 24) or 24)
    if _recent_bot_message_within(email_l, sid, cd_hours):
        telemetry_state.mark_skipped(event, 'cooldown_active')
        return 'skipped', False
    recipient = _extract_iran_mobile_from_text(email, comment)
    if not recipient:
        telemetry_state.mark_skipped(event, 'no_recipient')
        return 'skipped', False
    total_bytes = int(row.get('totalGB') or 0)
    expiry_ts = int(row.get('expiryTimestamp') or 0)
    if cfg.get('skip_unlimited') and (total_bytes <= 0 or expiry_ts <= 0):
        telemetry_state.mark_skipped(event, 'unlimited_skipped')
        return 'skipped', False
    hide_days = 7
    mon = _get_monitor_settings_cached(cfg)
    expired_max_age = int(cfg.get('expired_max_age_days') or 0) or hide_days
    ended_max_age = int(cfg.get('ended_max_age_days') or 0)
    if state == 'expired' and expired_max_age:
        try:
            exp = format_remaining_days(expiry_ts)
            days_ago = abs(int(exp.get('days') or 0))
        except Exception:
            days_ago = 0
        if days_ago > expired_max_age:
            telemetry_state.mark_skipped(event, 'expired_too_old')
            return 'skipped', False
    if state == 'ended' and ended_max_age:
        first = _ended_first_contact(email_l, sid)
        if first and (datetime.utcnow() - first).days > ended_max_age:
            telemetry_state.mark_skipped(event, 'ended_too_old')
            return 'skipped', False
    if _sms_in_quiet_hours(cfg):
        telemetry_state.mark_skipped(event, 'quiet_hours',
                                    retry_in=_sms_quiet_seconds_remaining(cfg))
        return 'deferred', False
    ready, ready_reason, _gateway_status = _sms_gateway_ready(cfg)
    if not ready:
        telemetry_state.mark_skipped(event, ready_reason or 'gateway_not_ready',
                                    retry_in=300)
        return 'deferred', False

    tpl = (templates.get(SMS_STATE_TO_MONITOR_TPL.get(state)) or '').strip()
    if not tpl:
        telemetry_state.mark_skipped(event, 'no_template')
        return 'skipped', False
    mvars = _depletion_event_mvars(event, row, cfg)
    if _template_wants_recommendation(tpl):
        mvars.update(_recommendation_template_vars(
            sid, mvars.get('_sub_id'), email,
            terminal=state in ('expired', 'ended')))
    text_msg = _render_monitor_state_template(tpl, mvars)
    if not (text_msg or '').strip():
        telemetry_state.mark_skipped(event, 'empty_message')
        return 'skipped', False
    if shadow:
        # Shadow rollout: the pipeline explains what it WOULD have sent and the
        # legacy scan keeps sending. Exactly one path reaches the gateway.
        event.status = 'shadowed'
        event.last_error = 'shadow_mode'
        event.updated_at = datetime.utcnow()
        db.session.commit()
        _sms_log_row(jid, email_l, sid, _server_display_name(sid), state, recipient,
                     'shadowed', 'shadow_mode')
        return 'shadowed', False

    segment_info = _sms_segment_info(text_msg)
    segments = segment_info['sms_segments']
    slot_ok, slot_reason = _sms_take_send_slot(recipient, cfg, segments, priority=state)
    if not slot_ok:
        if slot_reason in ('daily_limit_reached', 'hourly_limit_reached'):
            telemetry_state.mark_skipped(event, slot_reason,
                                        retry_in=_sms_budget_deferral_seconds(slot_reason))
            return 'deferred', False
        telemetry_state.mark_skipped(event, slot_reason, retry_in=120)
        return 'deferred', False
    pace = float(cfg.get('send_pace_seconds') or 0)
    if pace > 0 and SMS_LAST_SEND_TS[0] > 0:
        gap = pace - (time.time() - SMS_LAST_SEND_TS[0])
        if gap > 0:
            time.sleep(min(gap, 30))
    notification_kind = lifecycle_service.sms_notification_kind(state)
    idem = telemetry_state.idempotency_key_for(event)
    meta = _depletion_notification_meta(
        event.service_key, generation, notification_kind, last_change_at=last_change_at)
    res = _send_sms_via_gmweb(recipient, text_msg, cfg,
                              priority=_gmweb_sms_priority(state),
                              idempotency_key=idem, meta=meta)
    SMS_LAST_SEND_TS[0] = time.time()
    if res.get('sent'):
        try:
            db.session.add(WhatsappBotLog(email=email_l, server_id=sid,
                                         event='sms_%s' % state))
            db.session.commit()
        except Exception:
            db.session.rollback()
        _sms_log_row(jid, email_l, sid, _server_display_name(sid), state, recipient,
                     _sms_accepted_status(res), None, res,
                     _lifecycle_audit(event.service_key, generation,
                                      meta.get('correlationId'), event.observed_at,
                                      last_change_at, idempotency_key=idem))
        telemetry_state.mark_sent(event, response=res,
                                  correlation_id=meta.get('correlationId'),
                                  gateway_request_id=res.get('request_id'))
        return 'sent', False
    _sms_refund_daily_segments(segments)
    reason = res.get('reason') or 'send_failed'
    _sms_log_row(jid, email_l, sid, _server_display_name(sid), state, recipient,
                 'failed', reason, res,
                 _lifecycle_audit(event.service_key, generation,
                                  meta.get('correlationId'), event.observed_at,
                                  last_change_at, idempotency_key=idem))
    delay = telemetry_state.mark_retry(event, reason)
    if res.get('status_code') == 429:
        # The gateway is rate limiting: stop draining this batch instead of
        # hammering it with the rest of the queue.
        return 'failed', True
    return 'failed', bool(delay == 0)


_MONITOR_SETTINGS_CACHE = {}


def _get_monitor_settings_cached(cfg) -> dict:
    """Monitor settings, read once per drain instead of once per event."""
    cached = _MONITOR_SETTINGS_CACHE.get('value')
    if cached is not None:
        return cached
    try:
        from app import _get_monitor_settings  # deferred compatibility export
        cached = _get_monitor_settings() or {}
    except Exception:
        cached = {}
    _MONITOR_SETTINGS_CACHE['value'] = cached
    return cached


def run_depletion_event_outbox(limit: int = 10, *, job_id: str | None = None,
                              triggered_by: str = 'event') -> dict:
    """Deliver due transition events. In 'on' mode this is the ONLY sender.

    The scan can spend half an hour between runs; a customer who ran out of traffic
    should not wait for it. This worker is the low-latency path: the fetch that
    observed the transition wrote the event, and delivery happens seconds later.
    """
    result = {'claimed': 0, 'sent': 0, 'skipped': 0, 'deferred': 0,
              'superseded': 0, 'failed': 0, 'shadowed': 0, 'stopped': None,
              'triggered_by': triggered_by}
    if not depletion_pipeline.detection_enabled():
        return result
    try:
        _MONITOR_SETTINGS_CACHE.pop('value', None)
        reclaimed = telemetry_state.reclaim_expired_leases()
        if reclaimed:
            _log_warning('[sms-events] reclaimed %s expired lease(s)', reclaimed)
        events = telemetry_state.claim_events(limit=limit)
        result['claimed'] = len(events)
        if not events:
            return result
        cfg = _get_sms_runtime_settings()
        mon = _get_monitor_settings_cached(cfg)
        templates = mon.get('templates', {}) if isinstance(mon, dict) else {}
        cooldown_hours = cfg.get('cooldown_hours') or {}
        shadow = depletion_pipeline.shadow_mode()
        for event in events:
            # Identify the row for the log line WITHOUT trusting the ORM instance. A
            # leased row can be deleted out of band (retention pruning, an operator
            # cleanup) and the attribute access that would reload it raises
            # ObjectDeletedError -- reading event.event_id inside the error handler is
            # what turned a HANDLED delivery failure into a traceback in the worker log.
            try:
                label = '%s (%s)' % (event.event_id, event.service_key)
            except Exception:
                label = '<event row unavailable>'
            try:
                outcome, stop = _deliver_depletion_event(
                    event, cfg=cfg, templates=templates,
                    cooldown_hours=cooldown_hours, job_id=job_id, shadow=shadow)
            except Exception:
                db.session.rollback()
                _log_warning('[sms-events] delivery failed for %s', label,
                             exc_info=True)
                outcome, stop = 'failed', False
            result[outcome] = int(result.get(outcome) or 0) + 1
            if stop:
                result['stopped'] = outcome
                break
    except Exception:
        db.session.rollback()
        _log_warning('[sms-events] drain failed', exc_info=True)
    return result


def depletion_event_worker(interval_seconds: int = 5) -> None:
    """Background loop that delivers transition events as they happen.

    A short interval is affordable precisely because this loop does no panel I/O and
    no full-install scan: it leases the few due rows (usually zero) and returns.
    """
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        try:
            with app.app_context():
                # Heartbeat FIRST, then the drain: a worker that dies mid-drain is
                # still detectable, and an idle worker (nothing due) must look alive.
                depletion_pipeline.note_worker_heartbeat()
                result = run_depletion_event_outbox(limit=10,
                                                    triggered_by='worker')
                if result.get('claimed'):
                    depletion_pipeline.note_delivery()
                if result.get('sent') or result.get('shadowed'):
                    app.logger.info(
                        '[sms-events] sent=%s shadowed=%s claimed=%s',
                        result.get('sent'), result.get('shadowed'),
                        result.get('claimed'))
        except Exception as exc:
            try:
                app.logger.warning('[sms-events] worker error: %s', exc)
            except Exception:
                pass
        time.sleep(max(2, int(interval_seconds)))

def _run_sms_depletion_scan(job_id: str | None = None, triggered_by: str = 'auto',
                            states: list[str] | tuple[str, ...] | None = None) -> dict:
    """State-based automated SMS scan. For each non-reseller-owned account, derive
    the Monitor service-state (near_expiry / low_volume / expired / ended), and if
    that state's trigger is enabled, send the Monitor per-state template via GMweb —
    once per cooldown window per (account, state). Updates SMS_SCAN_JOB so the UI can
    show how many users will be messaged and live progress."""
    from app import _get_monitor_settings, _send_sms_via_gmweb, _utc_iso_now, format_jalali, format_remaining_days  # deferred: app-level helper, avoids circular import
    cfg = _get_sms_runtime_settings()
    manual_states = _normalize_sms_scan_states(states)
    if states is not None:
        state_enabled = {s: (s in manual_states) for s in SMS_SCAN_STATES}
        priority_map = {s: i for i, s in enumerate(manual_states)}
    else:
        state_enabled = {
            'near_expiry': bool(cfg.get('trigger_near_expiry')),
            'low_volume': bool(cfg.get('trigger_low_volume')),
            'expired': bool(cfg.get('trigger_expired')),
            'ended': bool(cfg.get('trigger_ended')),
        }
        priority_map = SMS_STATE_PRIORITY
    now_iso = _utc_iso_now()

    # ── Reconciliation safety net (not the detector) ──────────────────────────
    # This scan used to BE the detector, and that was the bug: it asked "which
    # accounts look depleted right now", so a transition that happened between two
    # runs -- or while the snapshot was stale -- produced no event at all. It now
    # runs the opposite direction: record what the snapshot holds into the durable
    # ledger (so a transition nobody observed becomes an event instead of a silence)
    # and drain the outbox. In on mode the outbox is the only sender, which is what
    # makes it impossible for the two paths to text one customer twice.
    pipeline = {
        'mode': depletion_pipeline.mode(),
        'detection': depletion_pipeline.detection_enabled(),
    }
    if depletion_pipeline.detection_enabled():
        reconciled = depletion_pipeline.reconcile_snapshot(source='reconciliation')
        drained = {'sent': 0, 'claimed': 0}
        if cfg.get('enabled') and any(state_enabled.values()):
            drained = run_depletion_event_outbox(limit=25, job_id=job_id,
                                                 triggered_by=triggered_by)
        depletion_pipeline.note_reconciliation()
        pipeline['reconciled'] = reconciled
        pipeline['drained'] = drained
        if not depletion_pipeline.legacy_sender_active():
            _sms_scan_set(state='idle', reason='reconciled',
                          finished_at=_utc_iso_now(), pipeline=pipeline)
            return {
                'scanned': int(reconciled.get('observed') or 0),
                'sent': int(drained.get('sent') or 0),
                'reason': 'reconciled',
                'pipeline': pipeline,
            }
    if not cfg.get('enabled') or not any(state_enabled.values()):
        _sms_scan_set(state='idle', reason='disabled', finished_at=now_iso)
        return {'scanned': 0, 'sent': 0, 'reason': 'disabled'}
    if not (cfg.get('base_url') and cfg.get('api_key')):
        _sms_scan_set(state='idle', reason='gateway_not_configured', finished_at=now_iso)
        return {'scanned': 0, 'sent': 0, 'reason': 'gateway_not_configured'}
    ready, ready_reason, ready_status = _sms_gateway_ready(cfg)
    if not ready:
        _sms_scan_set(state='idle', reason=ready_reason or 'gateway_not_ready',
                      gateway_status=ready_status, finished_at=now_iso)
        return {'scanned': 0, 'sent': 0, 'reason': ready_reason or 'gateway_not_ready',
                'gateway_status': ready_status}
    # Quiet hours: don't send now. Candidates keep their cooldown untouched, so the
    # next run after the window picks them up — a true "send after 8am" queue.
    if _sms_in_quiet_hours(cfg):
        _sms_scan_set(state='idle', reason='quiet_hours', finished_at=now_iso)
        return {'scanned': 0, 'sent': 0, 'reason': 'quiet_hours'}

    jid = job_id or uuid.uuid4().hex
    cooldown_hours = cfg.get('cooldown_hours') or {}

    # Reuse Monitor templates and its long-expired fallback, but classify reminder
    # candidates with the SMS-specific thresholds exposed in Settings. Keep direct
    # lookups here so a configured zero remains meaningful (0 days = expiry day
    # only; 0 GB = disable the low-volume reminder while still detecting ended).
    mon = _get_monitor_settings()
    mfilters = mon.get('filters', {}) if isinstance(mon, dict) else {}
    mtemplates = mon.get('templates', {}) if isinstance(mon, dict) else {}
    warning_days = int(cfg.get('depletion_expiry_days', 3))
    warning_gb = float(cfg.get('depletion_volume_gb', 2.0))
    hide_days = int(mfilters.get('hide_days', 7) or 7)
    # SMS-specific cap on how long-expired an account may be and still get messaged
    # (stops a scan from suddenly texting people who expired years ago). 0 ⇒ fall
    # back to Monitor's hide_days.
    expired_max_age = int(cfg.get('expired_max_age_days') or 0) or hide_days
    # Optional cutoff for volume-ended (no-date) accounts: stop after N days since
    # we first messaged them as 'ended'. 0 ⇒ no cutoff (message every cooldown).
    ended_max_age = int(cfg.get('ended_max_age_days') or 0)

    # One scan at a time across ALL workers (worker + manual "run now" must not
    # clobber each other). Check the shared Redis snapshot, not just local state.
    if _sms_scan_snapshot().get('state') == 'running':
        return {'scanned': 0, 'sent': 0, 'reason': 'already_running'}

    # Reset progress for this run (also clear any leftover cancel signal).
    _sms_scan_cancel_clear()
    with SMS_SCAN_JOB_LOCK:
        SMS_SCAN_JOB.clear()
        SMS_SCAN_JOB.update({
            'id': jid, 'state': 'running', 'triggered_by': triggered_by,
            'started_at': now_iso, 'finished_at': None,
            'states_enabled': [k for k, v in state_enabled.items() if v],
            'manual_states': manual_states if states is not None else None,
            'total_clients': 0, 'candidates': 0, 'processed': 0,
            'sent': 0, 'failed': 0, 'skipped_cooldown': 0, 'skipped_rate': 0,
            'per_state': {k: 0 for k in state_enabled},
            'current': None, 'stopped': None,
        })

    inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    seen = set()
    # Each candidate carries the notification identity it was classified from:
    # (sid_norm, email, email_l, server_name, state, recipient, mvars, comment,
    #  service_key, observed_generation, observed_at)
    candidates = []
    total_clients = 0

    # Pass 1 — classify and collect everyone eligible (matching an enabled state +
    # has a mobile + not reseller-owned). This gives the "will message up to N" count.
    for inbound in inbounds:
        sid = inbound.get('server_id')
        server_name = inbound.get('server_name') or ''
        try:
            sid_norm = int(sid)
        except (TypeError, ValueError):
            sid_norm = None
        for client in (inbound.get('clients') or []):
            email = (client.get('email') or '').strip()
            email_l = email.lower()
            if not email_l:
                continue
            key = (sid_norm, email_l)
            if key in seen:
                continue
            seen.add(key)
            total_clients += 1

            if _account_has_reseller_owner(sid_norm, email):
                continue

            enabled = bool(client.get('enable', True))
            total_bytes = int(client.get('totalGB') or 0)
            try:
                used = int(client.get('up') or 0) + int(client.get('down') or 0)
            except Exception:
                used = 0
            rem_bytes = client.get('remaining_bytes')
            if rem_bytes is None or rem_bytes == -1:
                rem_bytes = max(total_bytes - used, 0) if total_bytes > 0 else None
            rem_gb = (float(rem_bytes) / (1024 ** 3)) if rem_bytes is not None else None

            expiry_ts = int(client.get('expiryTimestamp') or 0)
            exp = format_remaining_days(expiry_ts)

            status = _classify_monitor_status(
                enabled=enabled, total_bytes=total_bytes, remaining_bytes=rem_bytes,
                remaining_gb=rem_gb, expiry_ts=expiry_ts, expiry_info=exp,
                warning_days=warning_days, warning_gb=warning_gb,
            )
            state = SMS_MONITOR_TAG_TO_STATE.get(status or '')
            if not state or not state_enabled.get(state):
                continue

            # Operator option: skip accounts that are unlimited in either dimension
            # (no volume cap or no expiry date). total_bytes<=0 ⇒ unlimited volume,
            # expiry_ts<=0 ⇒ unlimited/no time.
            if cfg.get('skip_unlimited') and (total_bytes <= 0 or expiry_ts <= 0):
                continue

            # Skip long-expired accounts: never text someone who expired ages ago.
            if status == 'expired' and expired_max_age:
                try:
                    days_ago = abs(int(exp.get('days') or 0))
                except Exception:
                    days_ago = 0
                if days_ago > expired_max_age:
                    continue

            # Stop nagging a volume-ended (no-date) account once it's been more than
            # N days since we first messaged it as ended. First contact = anchor.
            if status == 'ended' and ended_max_age:
                first = _ended_first_contact(email_l, sid_norm)
                if first and (datetime.utcnow() - first).days > ended_max_age:
                    continue

            # Operator opt-out: client comment tagged #nosms ⇒ never SMS them,
            # regardless of state/enable/expiry (e.g. user said "won't renew").
            if _sms_comment_opted_out(client.get('comment')):
                continue

            recipient = _extract_iran_mobile_from_text(email, client.get('comment') or '')
            if not recipient:
                continue

            # The canonical, durable service identity. Centralised in the lifecycle
            # service so the renewal side and this scan can never disagree.
            service_key = lifecycle_service.service_key_for_client(
                sid_norm, client, email=email)

            expiry_date = None
            if expiry_ts and expiry_ts > 0:
                try:
                    expiry_date = format_jalali(datetime.utcfromtimestamp(expiry_ts / 1000))
                except Exception:
                    expiry_date = None
            mvars = {
                'user': email,
                'rem': client.get('remaining_formatted') or 'Unlimited',
                'time': exp.get('text') or '-',
                'date': expiry_date or '-',
                'server': server_name,
                '_sub_id': client.get('subId') or client.get('id') or '',
            }
            candidates.append((sid_norm, email, email_l, server_name, state, recipient, mvars,
                               client.get('comment') or '', service_key, None,
                               _snapshot_observation_time(client)))

    # Send priority: volume-running-out → time-running-out → volume-ended →
    # fully-expired. Within a state, original (scan) order is preserved.
    candidates.sort(key=lambda c: priority_map.get(c[4], 99))

    # One batched read of the durable generations for exactly the candidates that
    # survived the cheap scan — never for every dashboard row (no N+1, no
    # full-panel refresh per client).
    try:
        generations = lifecycle_service.generations_for_keys(
            [c[8] for c in candidates if c[8]])
    except Exception:
        generations = {}

    _sms_scan_set(total_clients=total_clients, candidates=len(candidates))

    sent = 0
    # Pass 2 — cooldown gate + rate-limit + send + log per candidate.
    for (sid_norm, email, email_l, server_name, state, recipient, mvars, queued_comment,
         service_key, observed_generation, observed_at) in candidates:
        lifecycle = generations.get(service_key) or {}
        expected_generation = lifecycle.get('generation')
        last_change_at = lifecycle.get('last_lifecycle_change_at')
        if _sms_scan_cancelled():
            _sms_scan_set(state='stopped', stopped='cancelled', finished_at=_utc_iso_now(), current=None)
            return {'scanned': total_clients, 'sent': sent, 'stopped': 'cancelled'}
        _sms_scan_set(current=email)
        _sms_scan_inc('processed')

        if _sms_account_opted_out(sid_norm, email, queued_comment, refresh_shared=True):
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                         'skipped', 'opted_out_recheck')
            continue

        event = f'sms_{state}'
        if _sms_has_manual_review(email_l, sid_norm, state):
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                         'skipped', 'manual_review_pending')
            continue
        # Per-state cooldown in hours, shared across SMS + WhatsApp (event-agnostic).
        cd_hours = int(cooldown_hours.get(state, 24) or 24)
        if _recent_bot_message_within(email_l, sid_norm, cd_hours):
            _sms_scan_inc('skipped_cooldown')
            continue

        tpl = (mtemplates.get(SMS_STATE_TO_MONITOR_TPL[state]) or '').strip()
        if not tpl:
            _sms_scan_inc('skipped_rate')  # no template configured for this state
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient, 'skipped', 'no_template')
            continue
        if _template_wants_recommendation(tpl):
            mvars.update(_recommendation_template_vars(
                sid_norm, mvars.get('_sub_id'), email,
                terminal=state in ('expired', 'ended'),
            ))
        text_msg = _render_monitor_state_template(tpl, mvars)
        if not (text_msg or '').strip():
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient, 'skipped', 'empty_message')
            continue

        segment_info = _sms_segment_info(text_msg)
        segments = segment_info['sms_segments']
        slot_ok, slot_reason = _sms_take_send_slot(recipient, cfg, segments, priority=state)
        if not slot_ok:
            # Daily OR hourly budget exhausted → stop this run cleanly. Candidates
            # already messaged keep their cooldown; everyone else keeps waiting,
            # and the next scheduled scan picks the work back up.
            if slot_reason in ('daily_limit_reached', 'hourly_limit_reached'):
                _sms_scan_set(stopped=slot_reason, state='done', finished_at=_utc_iso_now(), current=None)
                _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient, 'skipped', slot_reason, segment_info)
                return {'scanned': total_clients, 'sent': sent, 'stopped': slot_reason}
            _sms_scan_inc('skipped_rate')
            continue

        # Global pace: keep a gap between ANY two sends so we don't burst the
        # gateway into HTTP 429 (it rate-limits rapid /send calls).
        pace = float(cfg.get('send_pace_seconds') or 0)
        if pace > 0 and SMS_LAST_SEND_TS[0] > 0:
            gap = pace - (time.time() - SMS_LAST_SEND_TS[0])
            if gap > 0:
                time.sleep(min(gap, 60))

        # Re-check after pacing as well: a tag added while this candidate waited
        # must stop the request before it reaches the provider queue.
        if _sms_account_opted_out(sid_norm, email, queued_comment, refresh_shared=True):
            _sms_refund_daily_segments(segments)
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                         'skipped', 'opted_out_recheck', segment_info)
            continue
        # FINAL consistency guard, for this candidate only: durable generation,
        # snapshot age, then live state. Nothing is sent on doubt.
        state_valid, state_reason = _sms_depletion_state_still_valid(
            sid_norm, email, state, cfg, service_key=service_key,
            expected_generation=expected_generation,
            last_lifecycle_change_at=last_change_at,
        )
        if not state_valid:
            if state_reason in _SNAPSHOT_REFRESHABLE_REASONS:
                # The cached copy is merely too old to prove anything. Try one
                # targeted, read-only panel read for THIS client before giving up;
                # never a full-panel fan-out.
                refreshed, observed_at = _targeted_candidate_refresh(
                    sid_norm, email)
                if refreshed:
                    state_valid, state_reason = _sms_depletion_state_still_valid(
                        sid_norm, email, state, cfg, service_key=service_key,
                        expected_generation=expected_generation,
                        last_lifecycle_change_at=last_change_at,
                    )
            if not state_valid:
                _sms_refund_daily_segments(segments)
                _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                             'skipped', state_reason, segment_info,
                             _lifecycle_audit(service_key, expected_generation,
                                              None, observed_at, last_change_at))
                continue

        # The gateway requires an integer generation on every meta-aware send, so a
        # candidate whose durable generation could not be established is SKIPPED
        # rather than posted with `generation: null`. Missing one reminder is
        # recoverable; a rejected request that hides the real cause is not.
        if expected_generation is None:
            established = lifecycle_service.read_or_create_generation(
                service_key, server_id=sid_norm, client_email=email_l,
                client_uuid=_service_uuid_for(sid_norm, email),
                reason='depletion_baseline')
            if not established.get('established'):
                _sms_refund_daily_segments(segments)
                _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                             'skipped',
                             'generation_unavailable:%s'
                             % (established.get('reason') or 'unknown'),
                             segment_info,
                             _lifecycle_audit(service_key, None, None,
                                              observed_at, last_change_at))
                continue
            expected_generation = established.get('generation')
            last_change_at = established.get('last_lifecycle_change_at') or last_change_at

        # Stable Idempotency-Key: identical across every retry of THIS logical
        # message (including the 429 retry below), different for the next window.
        notification_kind = lifecycle_service.sms_notification_kind(state)
        scan_idem = _sms_idempotency_key(
            service_key, expected_generation, notification_kind, cd_hours)
        gmweb_priority = _gmweb_sms_priority(state)
        sms_meta = _depletion_notification_meta(
            service_key, expected_generation, notification_kind,
            last_change_at=last_change_at)
        res = _send_sms_via_gmweb(
            recipient, text_msg, cfg, priority=gmweb_priority,
            idempotency_key=scan_idem, meta=sms_meta)
        # Gateway rate-limited: back off once and retry. If still limited, stop the
        # whole run rather than hammering it with the rest of the batch (the next
        # scheduled scan resumes where this left off).
        if (not res.get('sent')) and res.get('status_code') == 429:
            time.sleep(min(60, max(1, int(res.get('retry_after_seconds') or 60))))
            if _sms_account_opted_out(sid_norm, email, queued_comment, refresh_shared=True):
                _sms_refund_daily_segments(segments)
                _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                             'skipped', 'opted_out_recheck', segment_info)
                continue
            state_valid, state_reason = _sms_depletion_state_still_valid(
                sid_norm, email, state, cfg, service_key=service_key,
                expected_generation=expected_generation,
                last_lifecycle_change_at=last_change_at,
            )
            if not state_valid:
                _sms_refund_daily_segments(segments)
                _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                             'skipped', state_reason, segment_info,
                             _lifecycle_audit(service_key, expected_generation,
                                              None, observed_at, last_change_at))
                continue
            res = _send_sms_via_gmweb(
                recipient, text_msg, cfg, priority=gmweb_priority,
                idempotency_key=scan_idem, meta=sms_meta)
            if (not res.get('sent')) and res.get('status_code') == 429:
                _sms_refund_daily_segments(segments)
                SMS_LAST_SEND_TS[0] = time.time()
                _sms_scan_inc('failed')
                rate_reason = res.get('reason') or 'http_429'
                _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient, 'failed', rate_reason, res)
                _sms_scan_set(stopped=f'gateway_rate_limited: {rate_reason}', state='done',
                              finished_at=_utc_iso_now(), current=None)
                return {'scanned': total_clients, 'sent': sent, 'stopped': 'gateway_rate_limited'}
        SMS_LAST_SEND_TS[0] = time.time()
        if res.get('sent'):
            try:
                db.session.add(WhatsappBotLog(email=email_l, server_id=(sid_norm or 0), event=event))
                db.session.commit()
            except Exception:
                db.session.rollback()
            sent += 1
            _sms_scan_inc('sent')
            with SMS_SCAN_JOB_LOCK:
                ps = SMS_SCAN_JOB.get('per_state') or {}
                ps[state] = int(ps.get(state, 0) or 0) + 1
                SMS_SCAN_JOB['per_state'] = ps
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                         _sms_accepted_status(res), None, res,
                         _lifecycle_audit(service_key, expected_generation,
                                          sms_meta.get('correlationId'),
                                          observed_at, last_change_at,
                                          idempotency_key=scan_idem))
        elif res.get('manual_review'):
            _sms_scan_inc('failed')
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient,
                         'manual_review', 'unverified_manual_review', res,
                         _lifecycle_audit(service_key, expected_generation,
                                          sms_meta.get('correlationId'),
                                          observed_at, last_change_at,
                                          idempotency_key=scan_idem))
        else:
            _sms_refund_daily_segments(segments)
            _sms_scan_inc('failed')
            _sms_log_row(jid, email_l, sid_norm, server_name, state, recipient, 'failed',
                         res.get('reason'), res,
                         _lifecycle_audit(service_key, expected_generation,
                                          sms_meta.get('correlationId'),
                                          observed_at, last_change_at,
                                          idempotency_key=scan_idem))

    _sms_scan_set(state='done', finished_at=_utc_iso_now(), current=None)
    return {'scanned': total_clients, 'sent': sent, 'candidates': len(candidates)}


def _run_sms_royalty_scan(job_id: str | None = None, triggered_by: str = 'auto') -> dict:
    """Royalty SMS: nudge owner-less idle accounts (enabled, zero traffic since the
    royalty window start) once per long cooldown, capped by the SHARED daily budget.

    Runs AFTER the depletion scan so urgent alerts take the budget first; whatever
    daily quota remains flows to royalty. The cap+cooldown+resume loop IS the queue:
    a 1500-name list drains a few hundred a day (daily cap), each user exactly once
    (royalty cooldown), every send AND every cooldown-skip written to the SMS log —
    so a huge list spreads fairly over several days instead of spamming or stalling."""
    from app import _compute_royalty_idle, _render_text_template, _send_sms_via_gmweb, _utc_iso_now, app  # deferred: app-level helper, avoids circular import
    cfg = _get_sms_runtime_settings()
    now_iso = _utc_iso_now()
    if not cfg.get('enabled') or not cfg.get('trigger_royalty'):
        return {'scanned': 0, 'sent': 0, 'reason': 'disabled'}
    if not (cfg.get('base_url') and cfg.get('api_key')):
        return {'scanned': 0, 'sent': 0, 'reason': 'gateway_not_configured'}
    ready, ready_reason, ready_status = _sms_gateway_ready(cfg)
    if not ready:
        return {'scanned': 0, 'sent': 0, 'reason': ready_reason or 'gateway_not_ready',
                'gateway_status': ready_status}
    if _sms_in_quiet_hours(cfg):
        return {'scanned': 0, 'sent': 0, 'reason': 'quiet_hours'}
    if _sms_scan_snapshot().get('state') == 'running':
        return {'scanned': 0, 'sent': 0, 'reason': 'already_running'}

    admin = Admin.query.filter(or_(Admin.is_superadmin == True, Admin.role == 'superadmin')).first()
    if not admin:
        return {'scanned': 0, 'sent': 0, 'reason': 'no_superadmin'}

    days = int(cfg.get('royalty_days') or 3)
    cd_hours = int(cfg.get('royalty_cooldown_days') or 30) * 24

    try:
        # reseller_filter=0 → only owner-less (system/superadmin) accounts.
        idle = _compute_royalty_idle(admin.id, days, None, 0)
    except Exception as exc:
        app.logger.warning("[sms-royalty] idle compute failed: %s", exc)
        return {'scanned': 0, 'sent': 0, 'reason': 'idle_query_failed'}

    base_url = _public_base_url()
    jid = job_id or uuid.uuid4().hex
    content = _get_sms_template_content(ROYALTY_INFO_SMS_TEMPLATE_TYPE, DEFAULT_ROYALTY_INFO_SMS_TEMPLATE)

    _sms_scan_cancel_clear()
    with SMS_SCAN_JOB_LOCK:
        SMS_SCAN_JOB.clear()
        SMS_SCAN_JOB.update({
            'id': jid, 'state': 'running', 'triggered_by': triggered_by, 'kind': 'royalty',
            'started_at': now_iso, 'finished_at': None, 'states_enabled': ['royalty'],
            'total_clients': len(idle), 'candidates': 0, 'processed': 0,
            'sent': 0, 'failed': 0, 'skipped_cooldown': 0, 'skipped_rate': 0,
            'per_state': {'royalty': 0}, 'current': None, 'stopped': None,
        })

    # Pass 1 — eligible: has a mobile, not opted out, not reseller-owned.
    candidates = []
    for it in idle:
        email = (it.get('email') or '').strip()
        email_l = email.lower()
        if not email_l:
            continue
        try:
            sid_norm = int(it.get('server_id'))
        except (TypeError, ValueError):
            sid_norm = None
        if _account_has_reseller_owner(sid_norm, email):
            continue
        if _sms_comment_opted_out(it.get('comment')):
            continue
        recipient = _extract_iran_mobile_from_text(email, it.get('comment') or '')
        if not recipient:
            continue
        dash = (it.get('dash_sub_url') or it.get('sub_url') or '').strip()
        if dash and not dash.startswith('http') and base_url:
            dash = base_url + (dash if dash.startswith('/') else f"/{dash}")
        tpl_vars = {
            'email': email, 'account_name': email, 'service_name': email, 'user': email,
            'dashboard_link': dash, 'sub_link': dash,
            'remaining_time': '-', 'remaining_volume': it.get('remaining_formatted') or '-',
            'server_name': it.get('server_name') or '',
        }
        candidates.append((sid_norm, email, email_l, it.get('server_name') or '', recipient,
                           tpl_vars, it.get('comment') or ''))

    _sms_scan_set(candidates=len(candidates))

    sent = 0
    for (sid_norm, email, email_l, server_name, recipient, tpl_vars, queued_comment) in candidates:
        if _sms_scan_cancelled():
            _sms_scan_set(state='stopped', stopped='cancelled', finished_at=_utc_iso_now(), current=None)
            return {'scanned': len(idle), 'sent': sent, 'stopped': 'cancelled'}
        _sms_scan_set(current=email)
        _sms_scan_inc('processed')

        if _sms_account_opted_out(sid_norm, email, queued_comment, refresh_shared=True):
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient,
                         'skipped', 'opted_out_recheck')
            continue

        # Fairness: skip anyone messaged (any automation) inside the cooldown, log why.
        if _recent_bot_message_within(email_l, sid_norm, cd_hours):
            _sms_scan_inc('skipped_cooldown')
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient, 'skipped', 'cooldown')
            continue
        if _sms_has_manual_review(email_l, sid_norm, 'royalty'):
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient,
                         'skipped', 'manual_review_pending')
            continue

        text_msg = _render_text_template(content, tpl_vars)
        if not (text_msg or '').strip():
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient, 'skipped', 'empty_message')
            continue

        segment_info = _sms_segment_info(text_msg)
        segments = segment_info['sms_segments']
        slot_ok, slot_reason = _sms_take_send_slot(recipient, cfg, segments, priority='royalty')
        if not slot_ok:
            if slot_reason in ('daily_limit_reached', 'hourly_limit_reached'):
                # Out of this hour/day's budget — stop; the next scheduled run
                # resumes the rest (cooldown skips everyone already sent), so
                # the list keeps draining.
                _sms_scan_set(stopped=slot_reason, state='done', finished_at=_utc_iso_now(), current=None)
                _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient, 'skipped', slot_reason, segment_info)
                return {'scanned': len(idle), 'sent': sent, 'stopped': slot_reason}
            _sms_scan_inc('skipped_rate')
            continue

        pace = float(cfg.get('send_pace_seconds') or 0)
        if pace > 0 and SMS_LAST_SEND_TS[0] > 0:
            gap = pace - (time.time() - SMS_LAST_SEND_TS[0])
            if gap > 0:
                time.sleep(min(gap, 60))

        if _sms_account_opted_out(sid_norm, email, queued_comment, refresh_shared=True):
            _sms_refund_daily_segments(segments)
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient,
                         'skipped', 'opted_out_recheck', segment_info)
            continue

        scan_idem = f"royalty-{jid}-{sid_norm}-{email_l}"
        res = _send_sms_via_gmweb(
            recipient, text_msg, cfg, priority=_gmweb_sms_priority('royalty'),
            idempotency_key=scan_idem)
        if (not res.get('sent')) and res.get('status_code') == 429:
            time.sleep(min(60, max(1, int(res.get('retry_after_seconds') or 60))))
            if _sms_account_opted_out(sid_norm, email, queued_comment, refresh_shared=True):
                _sms_refund_daily_segments(segments)
                _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient,
                             'skipped', 'opted_out_recheck', segment_info)
                continue
            res = _send_sms_via_gmweb(
                recipient, text_msg, cfg, priority=_gmweb_sms_priority('royalty'),
                idempotency_key=scan_idem)
            if (not res.get('sent')) and res.get('status_code') == 429:
                _sms_refund_daily_segments(segments)
                SMS_LAST_SEND_TS[0] = time.time()
                _sms_scan_inc('failed')
                rate_reason = res.get('reason') or 'http_429'
                _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient, 'failed', rate_reason, res)
                _sms_scan_set(stopped=f'gateway_rate_limited: {rate_reason}', state='done',
                              finished_at=_utc_iso_now(), current=None)
                return {'scanned': len(idle), 'sent': sent, 'stopped': 'gateway_rate_limited'}
        SMS_LAST_SEND_TS[0] = time.time()
        if res.get('sent'):
            try:
                db.session.add(WhatsappBotLog(email=email_l, server_id=(sid_norm or 0), event='sms_royalty'))
                db.session.commit()
            except Exception:
                db.session.rollback()
            sent += 1
            _sms_scan_inc('sent')
            with SMS_SCAN_JOB_LOCK:
                ps = SMS_SCAN_JOB.get('per_state') or {}
                ps['royalty'] = int(ps.get('royalty', 0) or 0) + 1
                SMS_SCAN_JOB['per_state'] = ps
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient,
                         _sms_accepted_status(res), None, res)
        elif res.get('manual_review'):
            _sms_scan_inc('failed')
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient,
                         'manual_review', 'unverified_manual_review', res)
        else:
            _sms_refund_daily_segments(segments)
            _sms_scan_inc('failed')
            _sms_log_row(jid, email_l, sid_norm, server_name, 'royalty', recipient, 'failed', res.get('reason'), res)

    _sms_scan_set(state='done', finished_at=_utc_iso_now(), current=None)
    return {'scanned': len(idle), 'sent': sent, 'candidates': len(candidates)}


def _sms_log_row(job_id, email_l, sid_norm, server_name, state, recipient, status, reason,
                 gateway_result: dict | None = None, lifecycle: dict | None = None):
    """Persist one audit row for the SMS send-log history. Best-effort.

    ``lifecycle`` carries the notification identity the message was created from
    (``serviceKey``, ``generation``, ``correlationId``, ``idempotencyKey``,
    ``observedAt``, ``lastLifecycleChangeAt``). Persisting it is what lets an
    operator answer, after a customer complaint, whether this SMS was created
    before or after the renewal that supposedly answered it."""
    try:
        gateway_result = gateway_result or {}
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        _observed_at = lifecycle.get('observedAt')
        if _observed_at is not None and not isinstance(_observed_at, datetime):
            _observed_at = None
        _changed_at = lifecycle.get('lastLifecycleChangeAt')
        if _changed_at is not None and not isinstance(_changed_at, datetime):
            _changed_at = None
        row = SmsSendLog(
            email=email_l, server_id=(sid_norm or 0), server_name=(server_name or '')[:255],
            state=state, recipient=_mask_mobile(recipient), status=status,
            reason=(str(reason)[:255] if reason else None), job_id=job_id,
            request_id=(str(gateway_result.get('request_id'))[:128]
                        if gateway_result.get('request_id') else None),
            gateway_provider=(str(gateway_result.get('provider') or 'gmweb')[:24]),
            gateway_job_id=(str(gateway_result.get('job_id'))[:64]
                            if gateway_result.get('job_id') else None),
            status_url=(str(gateway_result.get('status_url'))[:512]
                        if gateway_result.get('status_url') else None),
            gateway_state=(str(gateway_result.get('status'))[:32]
                           if gateway_result.get('status') else None),
            terminal=(bool(gateway_result.get('terminal'))
                      if gateway_result.get('terminal') is not None
                      else False if gateway_result.get('request_id') else None),
            successful=(bool(gateway_result.get('successful'))
                        if gateway_result.get('successful') is not None else None),
            priority=(str(gateway_result.get('priority'))[:24]
                      if gateway_result.get('priority') else None),
            priority_level=(int(gateway_result.get('priority_level'))
                            if gateway_result.get('priority_level') is not None else None),
            queue_position=(int(gateway_result.get('queue_position'))
                            if gateway_result.get('queue_position') is not None else None),
            last_http_status=(int(gateway_result.get('status_code'))
                              if gateway_result.get('status_code') is not None else None),
            submitted_once=(bool(gateway_result.get('submitted_once'))
                            if gateway_result.get('submitted_once') is not None else None),
            verification_status=(str(gateway_result.get('verification_status'))[:64]
                                 if gateway_result.get('verification_status') else None),
            verification_attempts=(int(gateway_result.get('verification_attempts'))
                                   if gateway_result.get('verification_attempts') is not None else None),
            requested_to=(str(gateway_result.get('requested_to'))[:32]
                          if gateway_result.get('requested_to') else str(recipient or '')[:32]),
            sent_to=(str(gateway_result.get('sent_to'))[:32]
                     if gateway_result.get('sent_to') else None),
            recipient_evidence=(json.dumps(gateway_result.get('recipient_evidence'), ensure_ascii=False)
                                if isinstance(gateway_result.get('recipient_evidence'), dict) else None),
            conversation_url=(str(gateway_result.get('conversation_url'))[:2000]
                              if gateway_result.get('conversation_url') else None),
            segment_count=(int(gateway_result.get('sms_segments'))
                           if gateway_result.get('sms_segments') is not None else None),
            message_encoding=(str(gateway_result.get('sms_encoding'))[:16]
                              if gateway_result.get('sms_encoding') else None),
            unit_count=(int(gateway_result.get('sms_units'))
                        if gateway_result.get('sms_units') is not None else None),
            character_count=(int(gateway_result.get('sms_characters'))
                             if gateway_result.get('sms_characters') is not None else None),
            service_key=(str(lifecycle.get('serviceKey'))[:255]
                         if lifecycle.get('serviceKey') else None),
            lifecycle_generation=(int(lifecycle.get('generation'))
                                  if lifecycle.get('generation') is not None else None),
            correlation_id=(str(lifecycle.get('correlationId'))[:64]
                            if lifecycle.get('correlationId') else None),
            idempotency_key=(str(lifecycle.get('idempotencyKey'))[:200]
                             if lifecycle.get('idempotencyKey') else None),
            candidate_observed_at=_observed_at,
            last_lifecycle_change_at=_changed_at,
            gateway_outcome=(str(lifecycle.get('gatewayOutcome'))[:24]
                             if lifecycle.get('gatewayOutcome') else None),
            revocation_reason=(str(lifecycle.get('revocationReason'))[:120]
                               if lifecycle.get('revocationReason') else None),
            revoked_at=(str(lifecycle.get('revokedAt'))[:64]
                        if lifecycle.get('revokedAt') else None),
        )
        db.session.add(row)
        db.session.commit()
        return row.id
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def _sms_status_endpoint(base_url: str, row) -> str:
    status_url = (row.status_url or '').strip()
    if status_url.startswith('http://') or status_url.startswith('https://'):
        # Never let gateway response data turn the poller into an arbitrary
        # URL fetcher. Absolute status URLs are accepted only on the configured
        # gateway origin; otherwise use the documented requestId endpoint.
        if status_url.startswith(base_url.rstrip('/') + '/'):
            return status_url
        status_url = ''
    if not status_url and row.request_id:
        status_url = gmweb_contract.endpoint_path('send_status', requestId=row.request_id)
    return f"{base_url.rstrip('/')}/{status_url.lstrip('/')}"


def _refresh_pending_sms_statuses(limit: int = 100) -> int:
    """Poll accepted gateway tasks using the provider recorded for each send.

    Newest-first on purpose: the gateway only keeps a send in its ledger for a
    bounded window, so rows older than that answer 404 forever. Polling
    oldest-first let a growing tail of unresolvable rows fill every batch and
    starve the fresh sends — the panel then showed recent messages stuck on
    'queued' indefinitely. Ancient rows are closed out by the 404 rule below,
    so the pending set drains instead of growing.
    """
    from app import app  # deferred: app-level helper, avoids circular import
    cfg = _get_sms_runtime_settings()
    rows = SmsSendLog.query.filter(
        SmsSendLog.request_id.isnot(None),
        or_(SmsSendLog.terminal.is_(False), SmsSendLog.terminal.is_(None)),
    ).order_by(SmsSendLog.created_at.desc()).limit(max(1, min(int(limit), 500))).all()
    if not rows:
        return 0

    changed = 0
    affected_campaign_ids = set()
    # A send whose gateway record has aged out can never be resolved. Give it a
    # generous grace window (the gateway may lag right after acceptance), then
    # close it as unverifiable so it stops being polled forever. It is NOT
    # counted as a completed/billable segment — only confirmed sends are.
    status_expiry_cutoff = datetime.utcnow() - SMS_STATUS_UNRESOLVABLE_AFTER
    for row in rows:
        try:
            row_cfg = _get_sms_provider_settings(row.gateway_provider, cfg)
            base = (row_cfg.get('base_url') or '').strip().rstrip('/')
            api_key = (row_cfg.get('api_key') or '').strip()
            if not base or not api_key:
                continue
            headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}
            timeout = int(row_cfg.get('timeout_seconds') or 15)
            resp = requests.get(_sms_status_endpoint(base, row), headers=headers, timeout=timeout)
            if resp.status_code == 404 and (row.created_at or datetime.utcnow()) < status_expiry_cutoff:
                row.terminal = True
                row.successful = False
                row.reason = 'gateway_status_expired'
                row.last_http_status = 404
                row.updated_at = datetime.utcnow()
                changed += 1
                continue
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not isinstance(data, dict):
                continue

            previous = (row.status, row.gateway_state, row.stage, row.terminal,
                        row.successful, row.reason, row.gateway_job_id,
                        row.priority, row.priority_level, row.verification_status,
                        row.verification_attempts, row.submitted_once, row.sent_to)
            gateway_status = str(data.get('status') or '').strip().lower()
            gateway_state = str(data.get('state') or '').strip().lower()
            verification_status = str(data.get('verificationStatus') or '').strip()
            manual_review = (
                gateway_status == 'unverified'
                or gateway_state == 'unverified'
                or verification_status == 'manual_review_required'
            )
            # A lifecycle invalidation does not delete a queued reminder: the
            # gateway marks it terminal as "superseded" (never delivered, never
            # billable, never retried) and keeps the row queryable. Treating that
            # as an unknown status would leave the panel showing a reminder as
            # still queued after the renewal that revoked it.
            superseded = bool(data.get('superseded')) or gateway_status in (
                'superseded', 'suppressed') or gateway_state in ('superseded', 'revoked')
            superseded_reason = data.get('revocationReason')
            if gateway_status:
                row.status = 'manual_review' if manual_review else gateway_status[:16]
            if superseded:
                row.terminal = True
                row.successful = False
                row.invalidated_at = row.invalidated_at or datetime.utcnow()
                row.invalidation_reason = (
                    str(superseded_reason)[:64] if superseded_reason else 'superseded')
                if not row.reason:
                    row.reason = 'superseded_by_renewal'
            if data.get('outcome') is not None:
                row.gateway_outcome = str(data['outcome'])[:24]
            if superseded_reason is not None:
                row.revocation_reason = str(superseded_reason)[:120]
            if data.get('revokedAt') is not None:
                row.revoked_at = str(data['revokedAt'])[:64]
            if data.get('state') is not None:
                row.gateway_state = str(data['state'])[:32]
            row.stage = str(data['stage'])[:64] if data.get('stage') is not None else None
            if data.get('jobId') is not None:
                row.gateway_job_id = str(data['jobId'])[:64]
            if data.get('terminal') is not None:
                row.terminal = bool(data['terminal'])
            if data.get('successful') is not None:
                row.successful = bool(data['successful'])
            if data.get('priority') is not None:
                row.priority = str(data['priority'])[:24]
            if data.get('priorityLevel') is not None:
                row.priority_level = int(data['priorityLevel'])
            if data.get('submittedOnce') is not None:
                row.submitted_once = bool(data['submittedOnce'])
            if verification_status:
                row.verification_status = verification_status[:64]
            if data.get('verificationAttempts') is not None:
                row.verification_attempts = int(data['verificationAttempts'])
            if data.get('requestedTo') is not None:
                row.requested_to = str(data['requestedTo'])[:32]
            if data.get('sentTo') is not None:
                row.sent_to = str(data['sentTo'])[:32]
            if isinstance(data.get('recipientEvidence'), dict):
                row.recipient_evidence = json.dumps(
                    data['recipientEvidence'], ensure_ascii=False)
            if data.get('conversationUrl') is not None:
                row.conversation_url = str(data['conversationUrl'])[:2000]
            if data.get('currentAt') is not None:
                row.gateway_current_at = str(data['currentAt'])[:64]
            if data.get('sentAt') is not None:
                row.gateway_sent_at = str(data['sentAt'])[:64]
            failed_reason = data.get('failedReason')
            if manual_review:
                row.reason = 'unverified_manual_review'
                row.terminal = True
                row.successful = False
            elif failed_reason:
                row.reason = str(failed_reason)[:255]
            elif row.terminal and row.successful:
                row.reason = None
            row.updated_at = datetime.utcnow()

            current = (row.status, row.gateway_state, row.stage, row.terminal,
                       row.successful, row.reason, row.gateway_job_id,
                       row.priority, row.priority_level, row.verification_status,
                       row.verification_attempts, row.submitted_once, row.sent_to)
            if current != previous:
                changed += 1

            delivery = AnnouncementDelivery.query.filter_by(
                gateway_request_id=row.request_id,
                gateway_provider=(row.gateway_provider or 'gmweb')).first()
            if delivery:
                affected_campaign_ids.add(delivery.announcement_id)
                delivery.gateway_state = row.gateway_state or row.status
                delivery.gateway_stage = row.stage
                delivery.gateway_priority = row.priority
                delivery.gateway_priority_level = row.priority_level
                delivery.gateway_submitted_once = row.submitted_once
                delivery.gateway_verification_status = row.verification_status
                delivery.gateway_sent_to = row.sent_to
                if manual_review:
                    delivery.status = 'manual_review'
                    delivery.last_error = 'unverified_manual_review'
                    delivery.last_error_source = row.gateway_provider or 'gmweb'
                    delivery.processed_at = datetime.utcnow()
                    delivery.sent_at = None
                    delivery.next_attempt_at = None
                elif row.terminal and row.status == 'suppressed':
                    delivery.status = 'skipped'
                    delivery.last_error = 'duplicate_suppressed'
                    delivery.last_error_source = row.gateway_provider or 'gmweb'
                    delivery.processed_at = datetime.utcnow()
                    delivery.sent_at = None
                    delivery.next_attempt_at = None
                elif row.terminal and (
                        row.successful is False
                        or row.status in ('failed', 'cancelled')):
                    delivery.status = 'failed'
                    delivery.last_error = (
                        row.reason or row.gateway_state or row.stage or 'gmweb_delivery_failed'
                    )[:500]
                    delivery.last_error_source = row.gateway_provider or 'gmweb'
                    delivery.processed_at = datetime.utcnow()
                    delivery.sent_at = None
                    delivery.next_attempt_at = None
                elif row.terminal and (
                        row.successful or row.status in ('sent', 'completed')):
                    delivery.status = 'sent'
                    delivery.last_error = None
                    delivery.last_error_source = None
                    delivery.processed_at = delivery.processed_at or datetime.utcnow()
                elif not row.terminal and row.status in ('queued', 'active'):
                    delivery.status = row.status
                    delivery.processed_at = None
        except Exception as exc:
            app.logger.debug('[sms-status] poll failed request_id=%s: %s', row.request_id, exc)
    for campaign_id in affected_campaign_ids:
        campaign = db.session.get(Announcement, campaign_id)
        if campaign:
            counts = _recount_announcement_campaign(campaign)
            if not any(counts.get(value, 0) for value in (
                    'pending', 'retry', 'queued', 'active')):
                campaign.status = 'completed'
                campaign.finished_at = campaign.finished_at or datetime.utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return 0
    return changed


def sms_status_worker():
    """Continuously reconcile queued SMS tasks, including after Eve restarts."""
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        pending = False
        try:
            with app.app_context():
                # Safety net for the lifecycle-invalidation outbox. Its own singleton
                # worker is the primary drain; this keeps a failed renewal
                # invalidation moving even when that singleton was not elected or a
                # process restarted mid-retry. flush_invalidation_outbox only picks
                # up rows whose backoff has elapsed, so a double drain is a no-op.
                flush_invalidation_outbox(limit=10)
                pending = SmsSendLog.query.filter(
                    SmsSendLog.request_id.isnot(None),
                    or_(SmsSendLog.terminal.is_(False), SmsSendLog.terminal.is_(None)),
                ).first() is not None
                if pending:
                    _refresh_pending_sms_statuses()
        except Exception as exc:
            try:
                app.logger.warning('[sms-status] worker error: %s', exc)
            except Exception:
                pass
        # GMweb 0.3.30 recommends durable polling as the source of truth. SSE may
        # accelerate UI updates later, but a 10s poll avoids hammering the gateway.
        time.sleep(10 if pending else 30)


def _flush_pending_sms(force: bool = False) -> int:
    """Send SMS that were parked during quiet hours, oldest first, once the window
    has ended. Respects the same per-recipient slot, daily limit, global pace and
    429 back-off as the scan. Returns the number sent. Safe to call every tick."""
    from app import _send_sms_via_gmweb  # deferred: app-level helper, avoids circular import
    cfg = _get_sms_runtime_settings()
    if not cfg.get('enabled') or not (cfg.get('base_url') and cfg.get('api_key')):
        return 0
    if _sms_in_quiet_hours(cfg) and not force:
        return 0  # still inside the window — keep holding
    ready, _ready_reason, _ready_status = _sms_gateway_ready(cfg)
    if not ready:
        return 0
    try:
        rows = PendingSms.query.order_by(PendingSms.created_at.asc()).limit(500).all()
    except Exception:
        return 0
    sent = 0
    for r in rows:
        if _sms_account_opted_out(r.server_id, r.email, refresh_shared=True):
            try:
                _sms_log_row(None, r.email, r.server_id, r.server_name, r.event_name,
                             r.recipient, 'skipped', 'opted_out_recheck')
                db.session.delete(r)
                db.session.commit()
            except Exception:
                db.session.rollback()
            continue
        segment_info = _sms_segment_info(r.text)
        segments = segment_info['sms_segments']
        # These rows are transactional create/renew messages parked by quiet
        # hours, so pass their own event through: the critical lane stays exempt
        # from the hourly bulk throttle.
        slot_ok, slot_reason = _sms_take_send_slot(
            r.recipient, cfg, segments, priority=r.event_name)
        if not slot_ok:
            if slot_reason in ('daily_limit_reached', 'hourly_limit_reached'):
                break  # out of budget for now; retry next tick
            continue
        pace = float(cfg.get('send_pace_seconds') or 0)
        if pace > 0 and SMS_LAST_SEND_TS[0] > 0:
            gap = pace - (time.time() - SMS_LAST_SEND_TS[0])
            if gap > 0:
                time.sleep(min(gap, 60))
        flush_idem = f"flush-{r.id}"
        res = _send_sms_via_gmweb(
            r.recipient, r.text, cfg,
            priority=_gmweb_sms_priority(r.event_name), idempotency_key=flush_idem)
        if (not res.get('sent')) and res.get('status_code') == 429:
            time.sleep(min(60, max(1, int(res.get('retry_after_seconds') or 60))))
            res = _send_sms_via_gmweb(
                r.recipient, r.text, cfg,
                priority=_gmweb_sms_priority(r.event_name), idempotency_key=flush_idem)
        SMS_LAST_SEND_TS[0] = time.time()
        if (not res.get('sent')) and res.get('status_code') == 429:
            _sms_refund_daily_segments(segments)
            break  # gateway throttling — stop, the rest stays queued for next tick
        try:
            if res.get('sent'):
                _sms_log_row(None, r.email, r.server_id, r.server_name, r.event_name,
                             r.recipient, _sms_accepted_status(res), None, res)
                sent += 1
            elif res.get('manual_review'):
                _sms_log_row(None, r.email, r.server_id, r.server_name, r.event_name,
                             r.recipient, 'manual_review', 'unverified_manual_review', res)
            else:
                _sms_refund_daily_segments(segments)
                _sms_log_row(None, r.email, r.server_id, r.server_name, r.event_name,
                             r.recipient, 'failed', res.get('reason'), res)
            db.session.delete(r)
            db.session.commit()
        except Exception:
            db.session.rollback()
    return sent


def sms_bot_worker():
    """Background loop running the SMS near-depletion scan periodically."""
    from app import app  # deferred: app-level helper, avoids circular import
    while True:
        try:
            with app.app_context():
                # Finish any lifecycle invalidation a renewal could not deliver.
                flush_invalidation_outbox(limit=20)
                # First drain anything parked during quiet hours (if the window ended).
                flushed = _flush_pending_sms()
                if flushed:
                    app.logger.info("[sms-bot] flushed %s queued (quiet-hours) SMS", flushed)
                result = _run_sms_depletion_scan()
                if result.get('sent'):
                    app.logger.info("[sms-bot] depletion scan sent=%s scanned=%s", result.get('sent'), result.get('scanned'))
                # Royalty runs on the leftover daily budget (depletion took priority).
                roy = _run_sms_royalty_scan()
                if roy.get('sent'):
                    app.logger.info("[sms-bot] royalty scan sent=%s scanned=%s", roy.get('sent'), roy.get('scanned'))
        except Exception as exc:
            try:
                app.logger.warning("[sms-bot] scan error: %s", exc)
            except Exception:
                pass
        time.sleep(1800)  # every 30 minutes


# Phone normalization helpers extracted to panel.core.phone; re-exported here.
from panel.core.phone import (  # noqa: F401
    _normalize_ascii_digits,
    normalize_iran_mobile,
    normalize_international_phone,
    _normalize_contact_phone,
    _extract_iran_mobile_from_text,
)


def _take_whatsapp_send_slot(recipient: str, runtime_cfg: dict) -> tuple[bool, str | None]:
    now_ts = time.time()
    today = datetime.utcnow().strftime('%Y-%m-%d')
    min_interval = int(runtime_cfg.get('min_interval_seconds') or 45)
    # Warm-up shrinks the daily cap for fresh numbers; falls back to daily_limit.
    daily_limit = _whatsapp_effective_daily_cap(runtime_cfg)

    # Global pace gate (#4): minimum gap + jitter between ANY two sends. OFF by
    # default; protects against a batch turning into a burst.
    pace_enabled = bool(runtime_cfg.get('pace_enabled'))
    pace_gap = int(runtime_cfg.get('pace_min_gap_seconds') or 0)
    pace_jitter = int(runtime_cfg.get('pace_jitter_seconds') or 0)

    with WHATSAPP_SEND_TRACKER_LOCK:
        daily = WHATSAPP_SEND_TRACKER.get('daily') or {}
        if daily.get('date') != today:
            WHATSAPP_SEND_TRACKER['daily'] = {'date': today, 'count': 0}

        current_count = int((WHATSAPP_SEND_TRACKER.get('daily') or {}).get('count') or 0)
        if current_count >= daily_limit:
            return False, 'daily_limit_reached'

        if pace_enabled and pace_gap > 0:
            last_global = float(WHATSAPP_SEND_TRACKER.get('last_global_send') or 0.0)
            required = pace_gap + (random.uniform(0, pace_jitter) if pace_jitter > 0 else 0)
            if last_global > 0 and (now_ts - last_global) < required:
                return False, 'pace_gated'

        per_recipient = WHATSAPP_SEND_TRACKER.get('per_recipient') or {}
        last_sent = float(per_recipient.get(recipient) or 0.0)
        if last_sent > 0 and (now_ts - last_sent) < float(min_interval):
            return False, 'recipient_rate_limited'

        per_recipient[recipient] = now_ts
        WHATSAPP_SEND_TRACKER['per_recipient'] = per_recipient
        WHATSAPP_SEND_TRACKER['daily'] = {'date': today, 'count': current_count + 1}
        WHATSAPP_SEND_TRACKER['last_global_send'] = now_ts

    return True, None


def _send_whatsapp_message(event_name: str, recipient_source: str, message_text: str, *, recipient_comment: str = '') -> dict:
    runtime_cfg = _get_whatsapp_runtime_settings()
    result = {
        'attempted': False,
        'sent': False,
        'event': event_name,
        'recipient': None,
        'reason': None,
        'status_code': None,
    }

    if runtime_cfg.get('deployment_region') == 'iran':
        result['reason'] = 'deployment_in_iran'
        return result
    if not runtime_cfg.get('enabled'):
        result['reason'] = 'feature_disabled'
        return result

    trigger_key = f"trigger_{event_name}"
    if trigger_key in runtime_cfg and not runtime_cfg.get(trigger_key):
        result['reason'] = 'trigger_disabled'
        return result

    recipient = _extract_iran_mobile_from_text(recipient_source, recipient_comment or None)
    if not recipient:
        result['reason'] = 'recipient_not_found'
        return result

    gateway_url = (runtime_cfg.get('gateway_url') or '').strip()
    if not gateway_url:
        result['reason'] = 'gateway_not_configured'
        return result

    slot_ok, slot_reason = _take_whatsapp_send_slot(recipient, runtime_cfg)
    if not slot_ok:
        result['reason'] = slot_reason
        result['recipient'] = recipient
        return result

    provider = (runtime_cfg.get('provider') or 'baileys').strip().lower()
    api_key = (runtime_cfg.get('gateway_api_key') or '').strip()
    timeout = int(runtime_cfg.get('gateway_timeout_seconds') or 10)
    result['attempted'] = True
    result['recipient'] = recipient

    if provider == 'openwa':
        # OpenWA self-hosted gateway: session-scoped send-text endpoint.
        session_value = (runtime_cfg.get('session_id') or '').strip()
        if not session_value:
            result['attempted'] = False
            result['reason'] = 'openwa_session_not_configured'
            return result
        chat_id = _whatsapp_chat_id(recipient)
        if not chat_id:
            result['reason'] = 'invalid_recipient'
            return result
        # OpenWA's engine is keyed by UUID; a name works for DB lookup but not
        # for active messaging. Resolve to the real UUID before sending.
        session_uuid, resolve_err = _openwa_resolve_session_id(gateway_url, api_key, session_value, timeout)
        if not session_uuid:
            result['reason'] = f"openwa_session_unavailable: {resolve_err}"
            return result
        headers = {'Content-Type': 'application/json'}
        if api_key:
            headers['X-API-Key'] = api_key
        payload = {'chatId': chat_id, 'text': (message_text or '').strip()}
        try:
            response = requests.post(
                f"{gateway_url}/api/sessions/{session_uuid}/messages/send-text",
                json=payload,
                headers=headers,
                timeout=timeout,
                verify=outbound_tls_verify('EVE_WHATSAPP_CA_BUNDLE'),
            )
            result['status_code'] = int(response.status_code)
            if 200 <= response.status_code < 300:
                result['sent'] = True
                return result
            # Stale UUID (session restarted) — drop the cache so the next send re-resolves.
            _OPENWA_SESSION_ID_CACHE.pop((_normalize_whatsapp_gateway_url(gateway_url), session_value.strip().lower()), None)
            # Surface the gateway's own message (e.g. "session not active") to help debugging.
            try:
                body_msg = (response.json() or {}).get('message')
            except Exception:
                body_msg = None
            result['reason'] = f"gateway_http_{response.status_code}" + (f": {body_msg}" if body_msg else '')
            return result
        except Exception as exc:
            result['reason'] = f"gateway_error: {exc}"
            return result

    # Baileys-style simple gateway: POST /send
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f"Bearer {api_key}"

    payload = {
        'to': recipient,
        'message': (message_text or '').strip(),
        'event': event_name,
    }

    try:
        response = requests.post(
            f"{gateway_url}/send",
            json=payload,
            headers=headers,
            timeout=timeout,
            verify=outbound_tls_verify('EVE_WHATSAPP_CA_BUNDLE'),
        )
        result['status_code'] = int(response.status_code)
        if 200 <= response.status_code < 300:
            result['sent'] = True
            return result

        result['reason'] = f"gateway_http_{response.status_code}"
        return result
    except Exception as exc:
        result['reason'] = f"gateway_error: {exc}"
        return result
