import os
import socket
from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

import io
import re
import hmac
import json
import math
import sqlite3
import base64
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from telegram_diagnostics import (
    classify_telegram_connection_error, probe_telegram_transport,
    redact_connection_error,
)
from telegram_xray import build_xray_config_from_uri, find_xray_binary
import logging
import qrcode
import uuid
import secrets
import random
import string
import shutil
import glob
import subprocess
import tempfile
import threading
import time
import concurrent.futures
try:
    import magic  # python-magic (requires libmagic on many platforms)
except Exception:
    magic = None
from collections import defaultdict
from types import SimpleNamespace
import sys
from typing import Any

# bleach is required for HTML sanitization. Provide a clear runtime message
# if it's missing so developers running `py app.py` without the project's
# virtualenv get actionable instructions.
try:
    import bleach
    from bleach.css_sanitizer import CSSSanitizer
except ModuleNotFoundError:
    bleach = None
    CSSSanitizer = None
    if __name__ == '__main__':
        sys.stderr.write('\nMissing required package: bleach\n')
        sys.stderr.write('Install into the project venv or activate it before running.\n')
        sys.stderr.write('To activate venv in PowerShell:\n')
        sys.stderr.write('  .\\.venv\\Scripts\\Activate.ps1\n')
        sys.stderr.write('Then run: python app.py\n')
        sys.stderr.write('Or run directly with the venv python:\n')
        sys.stderr.write('  .\\venv\\Scripts\\python.exe app.py\n\n')
        sys.exit(1)
    else:
        # If imported as a library, raise a clearer error at import time
        raise

# cryptography is required for encrypting stored panel passwords (Server.password)
try:
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError:
    Fernet = None
    InvalidToken = Exception
    if __name__ == '__main__':
        sys.stderr.write('\nMissing required package: cryptography\n')
        sys.stderr.write('Install into the project venv or activate it before running.\n')
        sys.stderr.write('Example:\n')
        sys.stderr.write('  pip install -r requirements.txt\n\n')
        sys.exit(1)
    else:
        raise
from datetime import datetime, timedelta, timezone
from functools import wraps
import copy
try:
    from zoneinfo import ZoneInfo, available_timezones
except Exception:
    ZoneInfo = None
    available_timezones = None
from flask import Flask, render_template, jsonify, request, send_file, redirect, url_for, session, g, make_response, Response, stream_with_context
try:
    # Optional: HTTP response compression. The dashboard /api/refresh payload can be
    # tens of MB of JSON; gzipping it at the app level (before it leaves gunicorn)
    # cuts transfer size ~85-90% and, crucially, works even when an upstream CDN
    # (e.g. Cloudflare) refuses to compress very large responses at the edge.
    from flask_compress import Compress
except Exception:
    Compress = None
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from urllib.parse import urlparse, quote, urlencode, unquote
from jdatetime import datetime as jdatetime_class
from sqlalchemy import or_, and_, func, text, inspect, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

APP_VERSION = "2.5.73"
GITHUB_REPO = "aibedini/eve"
APP_START_TS = time.time()
PROCESS_ROLE = (os.environ.get('EVE_PROCESS_ROLE') or 'combined').strip().lower()
if PROCESS_ROLE not in ('combined', 'web', 'background', 'telegram-egress', 'telegram-bot'):
    PROCESS_ROLE = 'combined'

# Simple in-memory cache for update checks
UPDATE_CACHE = {
    'last_check': 0,
    'data': None,
    'ttl': 3600  # 1 hour cache
}
SYSTEM_UPDATE_STATE_DIR = os.environ.get(
    'EVE_SYSTEM_UPDATE_STATE_DIR', '/var/lib/eve-manager/web-update')
SYSTEM_UPDATE_UNIT_PATH = os.environ.get(
    'EVE_SYSTEM_UPDATE_UNIT_PATH', '/etc/systemd/system/eve-web-update.service')
SYSTEM_UPDATE_START_COMMAND = (
    'sudo', '-n', '/bin/systemctl', '--no-block', 'start',
    'eve-web-update.service',
)
XRAY_INSTALL_UNIT_PATH = os.environ.get(
    'EVE_XRAY_INSTALL_UNIT_PATH', '/etc/systemd/system/eve-xray-install.service')
XRAY_INSTALL_START_COMMAND = (
    'sudo', '-n', '/bin/systemctl', '--no-block', 'start',
    'eve-xray-install.service',
)
_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')

# ── Optional Redis (shared cache across gunicorn workers) ────────────────────
# Extracted to panel.core.redis_client; re-exported here for compatibility.
from panel.core.redis_client import (  # noqa: F401
    GLOBAL_SERVER_DATA,
    GLOBAL_REFRESH_LOCK,
    REDIS_URL,
    REDIS_SNAPSHOT_KEY,
    REDIS_SNAPSHOT_MANIFEST_KEY,
    REDIS_SERVER_SNAPSHOT_PREFIX,
    REDIS_SNAPSHOT_VERSION_KEY,
    REDIS_SNAPSHOT_TTL,
    REDIS_REFRESH_QUEUE_KEY,
    REDIS_REFRESH_PROCESSING_KEY,
    REDIS_REFRESH_JOB_PREFIX,
    REDIS_REFRESH_SCOPE_PREFIX,
    REDIS_REFRESH_JOB_TTL,
    get_redis,
    redis_enabled,
    publish_snapshot_to_redis,
    load_snapshot_from_redis,
)

# Ownership cache: pre-loaded from DB, used by enrich_inbounds_with_ownership.
# Avoids a per-request DB query with thousands of emails in IN clause.
_OWNERSHIP_CACHE: dict = {
    'email_map': {},   # {(server_id, email_lower): {'id':..,'username':..,'created_at':..}}
    'uuid_map':  {},   # {(server_id, uuid_lower):  {'id':..,'username':..,'created_at':..}}
    'uuid_global_map': {},  # {uuid_lower: {...}} — server-independent fallback so
                            # ownership survives a server re-add (new server_id) or
                            # an inbound rebuild (new inbound_id). 3X-UI UUIDs are
                            # globally unique, so matching by UUID alone is safe.
    'updated_at': 0.0, # time.monotonic()
}
_OWNERSHIP_CACHE_LOCK = threading.Lock()
OWNERSHIP_CACHE_TTL = 30  # seconds — ownership refreshed at most every 30 s

def _build_ownership_maps() -> tuple[dict, dict, dict, bool]:
    """Query DB once and return (email_map, uuid_map, uuid_global_map, success)."""
    email_map: dict = {}
    uuid_map:  dict = {}
    uuid_global_map: dict = {}
    try:
        rows = (
            db.session.query(ClientOwnership, Admin)
            .join(Admin, ClientOwnership.reseller_id == Admin.id)
            .all()
        )
        for own, reseller in rows:
            try:
                sid = int(own.server_id)
            except Exception:
                continue
            created = own.created_at or datetime.min
            info = {
                'id': int(reseller.id) if reseller else None,
                'username': reseller.username if reseller else None,
                'created_at': created,
            }
            em = (own.client_email or '').strip().lower()
            if em:
                key = (sid, em)
                ex = email_map.get(key)
                if not ex or created >= ex.get('created_at', datetime.min):
                    email_map[key] = info
            uu = (own.client_uuid or '').strip().lower()
            if uu:
                key = (sid, uu)
                ex = uuid_map.get(key)
                if not ex or created >= ex.get('created_at', datetime.min):
                    uuid_map[key] = info
                exg = uuid_global_map.get(uu)
                if not exg or created >= exg.get('created_at', datetime.min):
                    uuid_global_map[uu] = info
        return email_map, uuid_map, uuid_global_map, True
    except Exception:
        app.logger.exception("_build_ownership_maps failed — ownership cache not updated")
        return email_map, uuid_map, uuid_global_map, False

def _get_ownership_maps(force: bool = False) -> tuple[dict, dict]:
    """Return cached (email_map, uuid_map); rebuild from DB if stale."""
    _refresh_ownership_cache_if_stale(force)
    return _OWNERSHIP_CACHE['email_map'], _OWNERSHIP_CACHE['uuid_map']

def _get_ownership_uuid_global(force: bool = False) -> dict:
    """Return the server-independent {uuid: info} map (rebuilt if stale)."""
    _refresh_ownership_cache_if_stale(force)
    return _OWNERSHIP_CACHE['uuid_global_map']

def _refresh_ownership_cache_if_stale(force: bool = False) -> None:
    now = time.monotonic()
    with _OWNERSHIP_CACHE_LOCK:
        if not force and now - _OWNERSHIP_CACHE['updated_at'] < OWNERSHIP_CACHE_TTL:
            return
    # Build outside lock to avoid blocking other threads
    em, um, ug, ok = _build_ownership_maps()
    with _OWNERSHIP_CACHE_LOCK:
        _OWNERSHIP_CACHE['email_map'] = em
        _OWNERSHIP_CACHE['uuid_map']  = um
        _OWNERSHIP_CACHE['uuid_global_map'] = ug
        if ok:
            # Only cache on success — if DB failed, next request retries immediately
            _OWNERSHIP_CACHE['updated_at'] = time.monotonic()

def invalidate_ownership_cache() -> None:
    """Call after any ownership change so next request rebuilds from DB."""
    with _OWNERSHIP_CACHE_LOCK:
        _OWNERSHIP_CACHE['updated_at'] = 0.0

# Prevent overlapping forced refreshes (e.g. after rapid UI actions)
# GLOBAL_REFRESH_LOCK lives in panel.core.redis_client (imported above).

# Refresh job tracking (in-memory; per-process)
# Refresh/bulk-job pipeline extracted to panel.jobs.refresh (re-exported).
from panel.jobs.refresh import (  # noqa: F401
    REFRESH_JOBS,
    REFRESH_JOBS_LOCK,
    REFRESH_MAX_JOBS,
    SMS_SCAN_JOB,
    SMS_SCAN_JOB_LOCK,
    SMS_SCAN_REDIS_KEY,
    SMS_SCAN_CANCEL_REDIS_KEY,
    SMS_SCAN_REDIS_TTL,
    BULK_JOBS_FILE,
    BULK_JOBS,
    BULK_JOBS_CLIENTS,
    BULK_JOBS_LOCK,
    BULK_MAX_JOBS,
    BULK_SAVE_EVERY,
    _SNAPSHOT_PROGRESS_FILE,
    _SNAPSHOT_PROGRESS,
    _set_snap_progress,
    _read_snap_progress,
    TELEGRAM_BACKUP_JOBS_FILE,
    TELEGRAM_BACKUP_JOBS,
    TELEGRAM_BACKUP_JOBS_LOCK,
    TELEGRAM_BACKUP_MAX_JOBS,
    MAX_FILE_SIZE,
    REFRESH_BACKOFF,
    REFRESH_MAX_BACKOFF_SEC,
    _summarize_job,
    _summarize_bulk_job,
    _summarize_telegram_backup_job,
    _prune_telegram_backup_jobs_locked,
    _load_telegram_backup_jobs_locked,
    _save_telegram_backup_jobs_locked,
    _get_telegram_backup_job,
    _update_telegram_backup_job,
    _run_telegram_backup_job,
    _prune_bulk_jobs_locked,
    _load_bulk_jobs_locked,
    _save_bulk_jobs_locked,
    _bulk_progress_update,
    _run_bulk_job,
    _prune_refresh_jobs_locked,
    _refresh_job_redis_key,
    _refresh_scope_redis_key,
    _get_refresh_job,
    _store_refresh_job,
    _release_refresh_scope,
    _backoff_get,
    _backoff_should_skip,
    _backoff_record_failure,
    _backoff_record_success,
    _check_server_reachable,
    _update_reachability_status,
    _run_refresh_job,
    enqueue_refresh_job,
    refresh_queue_worker,
    _recompute_global_stats_from_server_statuses,
    fetch_and_update_server_data,
    _recompute_cached_client,
    _iter_cached_client_copies,
    patch_cached_client,
    add_cached_client,
    remove_cached_client,
    clone_cached_client_into_inbound,
)
def sanitize_html(content, tags=None, attributes=None, styles=None):
    """Sanitize HTML content to prevent XSS."""
    if content is None:
        return None
    if not isinstance(content, str):
        content = str(content)
    
    # bleach 5.0+ uses CSSSanitizer instead of styles argument
    css_sanitizer = None
    if styles is not None and CSSSanitizer:
        css_sanitizer = CSSSanitizer(allowed_css_properties=styles)
    
    return bleach.clean(
        content,
        tags=tags if tags is not None else [],
        attributes=attributes if attributes is not None else {},
        css_sanitizer=css_sanitizer,
        strip=True
    )

# Backoff to avoid hammering failing servers during periodic refresh

# Session/capability caches for X-UI panels live in panel.adapters.xui; re-exported here.
from panel.adapters.xui import (  # noqa: F401
    XUI_CAPABILITY_CACHE,
    XUI_CAPABILITY_TTL,
    XUI_SESSION_CACHE,
    XUI_SESSION_TTL,
)

# Messaging automation workers extracted to panel.jobs.messaging (re-exported).
from panel.jobs.messaging import (  # noqa: F401
    WHATSAPP_DEPLOYMENT_REGION_KEY,
    WHATSAPP_ENABLED_KEY,
    WHATSAPP_PROVIDER_KEY,
    WHATSAPP_TRIGGER_RENEW_KEY,
    WHATSAPP_TRIGGER_WELCOME_KEY,
    WHATSAPP_TRIGGER_PRE_EXPIRY_KEY,
    WHATSAPP_MIN_INTERVAL_SECONDS_KEY,
    WHATSAPP_DAILY_LIMIT_KEY,
    WHATSAPP_PRE_EXPIRY_HOURS_KEY,
    WHATSAPP_RETRY_COUNT_KEY,
    WHATSAPP_BACKOFF_SECONDS_KEY,
    WHATSAPP_CIRCUIT_BREAKER_KEY,
    WHATSAPP_TEMPLATE_RENEW_KEY,
    WHATSAPP_TEMPLATE_WELCOME_KEY,
    WHATSAPP_TEMPLATE_PRE_EXPIRY_KEY,
    WHATSAPP_GATEWAY_URL_KEY,
    WHATSAPP_GATEWAY_API_KEY,
    WHATSAPP_GATEWAY_TIMEOUT_KEY,
    WHATSAPP_SESSION_KEY,
    WHATSAPP_WARMUP_ENABLED_KEY,
    WHATSAPP_WARMUP_START_DATE_KEY,
    WHATSAPP_WARMUP_START_PER_DAY_KEY,
    WHATSAPP_WARMUP_RAMP_DAYS_KEY,
    WHATSAPP_PACE_ENABLED_KEY,
    WHATSAPP_PACE_MIN_GAP_KEY,
    WHATSAPP_PACE_JITTER_KEY,
    WHATSAPP_DEPLETION_ENABLED_KEY,
    WHATSAPP_DEPLETION_EXPIRY_DAYS_KEY,
    WHATSAPP_DEPLETION_VOLUME_GB_KEY,
    WHATSAPP_DEPLETION_COOLDOWN_DAYS_KEY,
    WHATSAPP_BOT_TPL_CREATED_KEY,
    WHATSAPP_BOT_TPL_RENEW_KEY,
    WHATSAPP_BOT_TPL_ENDED_KEY,
    WHATSAPP_BOT_TPL_INFO_KEY,
    DEFAULT_WHATSAPP_BOT_TPL_CREATED,
    DEFAULT_WHATSAPP_BOT_TPL_RENEW,
    DEFAULT_WHATSAPP_BOT_TPL_ENDED,
    DEFAULT_WHATSAPP_BOT_TPL_INFO,
    DEFAULT_WHATSAPP_TEMPLATE_RENEW,
    DEFAULT_WHATSAPP_TEMPLATE_WELCOME,
    DEFAULT_WHATSAPP_TEMPLATE_PRE_EXPIRY,
    WHATSAPP_SEND_TRACKER,
    WHATSAPP_SEND_TRACKER_LOCK,
    _normalize_whatsapp_region,
    _normalize_whatsapp_provider,
    _normalize_whatsapp_session,
    _whatsapp_chat_id,
    _normalize_whatsapp_gateway_url,
    _openwa_session_status,
    _OPENWA_SESSION_ID_CACHE,
    _OPENWA_SESSION_ID_TTL,
    _openwa_resolve_session_id,
    _get_whatsapp_runtime_settings,
    _whatsapp_effective_daily_cap,
    _whatsapp_render_bot_template,
    _public_base_url,
    _whatsapp_blocked_account_keys,
    _whatsapp_automation_allowed_for_account,
    _run_whatsapp_depletion_scan,
    whatsapp_bot_worker,
    TG_DEPLETION_ENABLED_KEY,
    TG_DEPLETION_RECOMMEND_KEY,
    TG_TRIGGER_RENEW_KEY,
    TG_DEPLETION_EXPIRY_DAYS_KEY,
    TG_DEPLETION_VOLUME_GB_KEY,
    TG_DEPLETION_COOLDOWN_DAYS_KEY,
    TG_TPL_RENEW_KEY,
    TG_TPL_NEAR_EXPIRY_KEY,
    TG_TPL_LOW_VOLUME_KEY,
    DEFAULT_TG_TPL_NEAR_EXPIRY,
    DEFAULT_TG_TPL_RENEW,
    DEFAULT_TG_TPL_LOW_VOLUME,
    _get_telegram_depletion_settings,
    _notification_bot_for_reseller,
    _notification_bot_for_account,
    _notify_customer_telegram,
    _depletion_renew_reply_markup,
    _run_telegram_depletion_scan,
    telegram_depletion_worker,
    _run_telegram_announcement_batch,
    telegram_announcement_worker,
    SMS_AUTOMATION_ENABLED_KEY,
    SMS_GMWEB_BASE_URL_KEY,
    SMS_GMWEB_API_KEY_KEY,
    SMS_GMWEB_TIMEOUT_KEY,
    SMS_TRIGGER_CREATED_KEY,
    SMS_TRIGGER_RENEW_KEY,
    SMS_TRIGGER_DEPLETION_KEY,
    SMS_TRIGGER_NEAR_EXPIRY_KEY,
    SMS_TRIGGER_LOW_VOLUME_KEY,
    SMS_TRIGGER_EXPIRED_KEY,
    SMS_TRIGGER_ENDED_KEY,
    SMS_DEPLETION_EXPIRY_DAYS_KEY,
    SMS_DEPLETION_VOLUME_GB_KEY,
    SMS_DEPLETION_COOLDOWN_DAYS_KEY,
    SMS_COOLDOWN_HOURS_NEAR_EXPIRY_KEY,
    SMS_COOLDOWN_HOURS_LOW_VOLUME_KEY,
    SMS_COOLDOWN_HOURS_EXPIRED_KEY,
    SMS_COOLDOWN_HOURS_ENDED_KEY,
    SMS_EXPIRED_MAX_AGE_DAYS_KEY,
    SMS_ENDED_MAX_AGE_DAYS_KEY,
    SMS_MIN_INTERVAL_SECONDS_KEY,
    SMS_DAILY_LIMIT_KEY,
    SMS_ANNOUNCEMENT_DAILY_LIMIT_KEY,
    SMS_SEND_PACE_SECONDS_KEY,
    SMS_QUIET_ENABLED_KEY,
    SMS_QUIET_START_KEY,
    SMS_QUIET_END_KEY,
    SMS_SKIP_UNLIMITED_KEY,
    SMS_TRIGGER_ROYALTY_KEY,
    SMS_ROYALTY_DAYS_KEY,
    SMS_ROYALTY_COOLDOWN_DAYS_KEY,
    SMS_SEND_TRACKER,
    SMS_SCAN_CANCEL,
    SMS_LAST_SEND_TS,
    SMS_SEND_TRACKER_LOCK,
    _GSM7_BASIC,
    _GSM7_EXTENDED,
    _sms_segment_info,
    _sms_tehran_day,
    _sms_announcement_segments_used_today,
    _sms_db_segments_used_today,
    _sms_db_segment_stats_today,
    _sms_segment_counter_key,
    _sms_reserve_daily_segments,
    _sms_refund_daily_segments,
    _sms_daily_segments_used,
    _get_sms_runtime_settings,
    _tehran_hour,
    _sms_in_quiet_hours,
    _account_has_reseller_owner,
    _recent_bot_message_within,
    _ended_first_contact,
    _comment_opted_out,
    SMS_COMMENT_OPTOUT_TAGS,
    _sms_comment_opted_out,
    _sms_account_opted_out,
    _toggle_optout_tags,
    _clear_message_cooldown,
    _sms_take_send_slot,
    _sms_gmweb_error_reason,
    GMWEB_SMS_PRIORITY_LEVELS,
    _gmweb_sms_priority,
    _get_gmweb_send_capacity,
    _send_sms_via_gmweb,
    _cancel_sms_via_gmweb,
    STALE_ACCOUNT_SMS_STATES,
    _cancel_pending_sms_for_account,
    _cancel_stale_account_sms,
    _sms_accepted_status,
    _sms_has_manual_review,
    _sms_should_send,
    _get_sms_template_content,
    _fire_automation_sms,
    SMS_MONITOR_TAG_TO_STATE,
    SMS_STATE_TO_MONITOR_TPL,
    SMS_STATE_PRIORITY,
    SMS_SCAN_STATES,
    SMS_MANUAL_DEFAULT_STATES,
    _normalize_sms_scan_states,
    _sms_gateway_ready,
    _mask_mobile,
    _render_monitor_state_template,
    _sms_scan_persist_locked,
    _sms_scan_snapshot,
    _sms_scan_set,
    _sms_scan_inc,
    _sms_scan_cancel_clear,
    _sms_scan_cancelled,
    _classify_monitor_status,
    _run_sms_depletion_scan,
    _run_sms_royalty_scan,
    _sms_log_row,
    _sms_status_endpoint,
    _refresh_pending_sms_statuses,
    sms_status_worker,
    _flush_pending_sms,
    sms_bot_worker,
    _take_whatsapp_send_slot,
    _send_whatsapp_message,
)

# Phone normalization helpers (panel.core.phone) — re-export restored.
from panel.core.phone import (  # noqa: F401
    _normalize_ascii_digits,
    normalize_iran_mobile,
    normalize_international_phone,
    _normalize_contact_phone,
    _extract_iran_mobile_from_text,
)


def _utc_iso_now():
    return datetime.utcnow().isoformat()


def _parse_bool(value) -> bool:
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')


# (refresh/bulk-job pipeline extracted to panel.jobs.refresh — imported at REFRESH_JOBS above.)
# BACKGROUND_THREADS_STARTED lives in panel.jobs.schedulers (imported below).

SERVER_PASSWORD_PREFIX = 'enc:'
_SERVER_PASSWORD_FERNET = None
_SERVER_PASSWORD_MIGRATION_DONE = False
_SERVER_PASSWORD_MIGRATION_LOCK = threading.Lock()


def _is_dev_mode() -> bool:
    
    env = (os.environ.get('FLASK_ENV') or os.environ.get('ENV') or '').strip().lower()
    debug = (os.environ.get('DEBUG') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    return debug or env in ('development', 'dev')


def _get_server_password_fernet() -> Any:
    """Return cached Fernet instance from SERVER_PASSWORD_KEY.

    SERVER_PASSWORD_KEY must be a URL-safe base64-encoded 32-byte key.
    """
    global _SERVER_PASSWORD_FERNET
    if _SERVER_PASSWORD_FERNET is not None:
        return _SERVER_PASSWORD_FERNET

    key = (os.environ.get('SERVER_PASSWORD_KEY') or '').strip()
    if not key:
        return None

    try:
        _SERVER_PASSWORD_FERNET = Fernet(key)
        return _SERVER_PASSWORD_FERNET
    except Exception:
        # Invalid key format. Log warning and return None to fallback to plaintext.
        app.logger.warning("Invalid SERVER_PASSWORD_KEY (must be Fernet key). Encryption/Decryption disabled.")
        return None


def encrypt_server_password(plaintext: str) -> str:
    f = _get_server_password_fernet()
    if not f:
        # If no key is configured, we store as plaintext (legacy behavior)
        return plaintext
    plain = str(plaintext or '')
    token = f.encrypt(plain.encode('utf-8')).decode('utf-8')
    return f'{SERVER_PASSWORD_PREFIX}{token}'


def decrypt_server_password(value: str) -> str:
    raw = str(value or '')
    if not raw:
        return ''
    if not raw.startswith(SERVER_PASSWORD_PREFIX):
        return raw

    f = _get_server_password_fernet()
    if not f:
        # If no key is configured, we can't decrypt. 
        # Return raw value as fallback (might be plaintext from legacy)
        return raw

    token = raw[len(SERVER_PASSWORD_PREFIX):]
    try:
        return f.decrypt(token.encode('utf-8')).decode('utf-8')
    except InvalidToken:
        # Decryption failed (e.g. key changed or data corrupted).
        # We log a warning instead of raising RuntimeError to avoid crashing background tasks.
        app.logger.warning("Failed to decrypt a stored server password (invalid key/token). Returning empty string.")
        return ""


def get_server_password(server: 'Server') -> str:
    return decrypt_server_password(getattr(server, 'password', '') or '')


def _maybe_migrate_server_passwords() -> None:
    """Encrypt any legacy plaintext Server.password values once (best-effort).

    Runs only when SERVER_PASSWORD_KEY is configured.
    """
    global _SERVER_PASSWORD_MIGRATION_DONE
    if _SERVER_PASSWORD_MIGRATION_DONE:
        return

    f = _get_server_password_fernet()
    if not f:
        return

    with _SERVER_PASSWORD_MIGRATION_LOCK:
        if _SERVER_PASSWORD_MIGRATION_DONE:
            return

        try:
            inspector = inspect(db.engine)
            if 'servers' not in inspector.get_table_names():
                _SERVER_PASSWORD_MIGRATION_DONE = True
                return
        except Exception:
            # DB not ready yet
            return

        try:
            servers = Server.query.all()
            changed = False
            for s in servers:
                try:
                    cur = (s.password or '').strip()
                    if not cur or cur.startswith(SERVER_PASSWORD_PREFIX):
                        continue
                    s.password = encrypt_server_password(cur)
                    changed = True
                except Exception:
                    continue
            if changed:
                db.session.commit()
            _SERVER_PASSWORD_MIGRATION_DONE = True
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass


def _security_per_request_setup():
    # CSP nonce for inline <script> blocks that cannot be moved yet.
    # Keep stable per request.
    g.csp_nonce = secrets.token_urlsafe(16)
    _maybe_migrate_server_passwords()

app = Flask(__name__)
app.json.ensure_ascii = False  # send emoji/Persian as real UTF-8, not \uXXXX escapes

# Enable HTTP response compression (gzip/br) for text payloads. The biggest win is
# the dashboard /api/refresh snapshot, which can be 20+ MB of JSON uncompressed.
# Skip tiny responses and already-compressed binary types.
if Compress is not None:
    app.config.setdefault('COMPRESS_MIMETYPES', [
        'application/json',
        'application/javascript',
        'text/html',
        'text/css',
        'text/plain',
        'text/xml',
        'image/svg+xml',
    ])
    app.config.setdefault('COMPRESS_MIN_SIZE', 1024)   # don't bother for sub-1KB bodies
    app.config.setdefault('COMPRESS_LEVEL', 6)          # balanced CPU vs ratio
    Compress(app)
else:
    try:
        app.logger.warning('flask-compress not installed; HTTP responses will not be gzipped (large /api/refresh payloads stay uncompressed).')
    except Exception:
        pass
# Trust one proxy hop (nginx SSL termination) so Flask sees correct scheme/host
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Register per-request security setup.
app.before_request(_security_per_request_setup)


@app.context_processor
def inject_csp_nonce():
    return {'csp_nonce': getattr(g, 'csp_nonce', '')}

_session_secret = (os.environ.get('SESSION_SECRET') or '').strip()
if not _session_secret:
    if _is_dev_mode():
        # Dev convenience: use a random secret so session forgery isn't trivial.
        # Sessions will reset on restart.
        app.secret_key = secrets.token_urlsafe(32)
        try:
            app.logger.warning('SESSION_SECRET not set; using random dev secret (sessions reset on restart).')
        except Exception:
            pass
    else:
        raise RuntimeError('SESSION_SECRET is required in production (no default fallback).')
else:
    app.secret_key = _session_secret

# Require server password encryption key in production.
if not _is_dev_mode():
    if not (_get_server_password_fernet()):
        raise RuntimeError('SERVER_PASSWORD_KEY is required in production to encrypt stored server passwords.')

# Use SQLite by default, but allow override via DATABASE_URL
db_url = os.environ.get("DATABASE_URL")
if db_url:
    db_url = str(db_url).strip()
    # Heroku-style scheme; SQLAlchemy expects postgresql://
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
else:
    db_path = os.path.join(app.instance_path, 'servers.db')
    os.makedirs(app.instance_path, exist_ok=True)
    db_url = f"sqlite:///{db_path}"

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 2048 * 1024 * 1024  # 2 GB — covers large migration bundles / installers / videos
# Re-read templates from disk on each render in dev so UI edits show without a
# full restart. Harmless in prod; production still benefits from a restart.
if _is_dev_mode():
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True


@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify({'success': False, 'error': 'File too large. Maximum allowed size is 512 MB.'}), 413


def _want_json() -> bool:
    """True when the caller expects a JSON response (API path or Accept: application/json)."""
    return (
        request.path.startswith('/api/')
        or 'application/json' in (request.headers.get('Accept') or '')
        or request.is_json
    )


@app.errorhandler(404)
def not_found(e):
    if _want_json():
        return jsonify({'success': False, 'error': f'Not found: {request.path}'}), 404
    return e


@app.errorhandler(405)
def method_not_allowed(e):
    if _want_json():
        return jsonify({'success': False, 'error': f'Method not allowed: {request.method} {request.path}'}), 405
    return e


@app.errorhandler(429)
def too_many_requests(e):
    if _want_json():
        retry_after = getattr(e, 'retry_after', None)
        message = getattr(e, 'description', None) or 'Too many requests. Please wait a moment and try again.'
        payload = {'success': False, 'error': message}
        if retry_after is not None:
            payload['retry_after'] = retry_after
        return jsonify(payload), 429
    return e


@app.errorhandler(500)
def internal_server_error(e):
    if _want_json():
        return jsonify({'success': False, 'error': f'Internal server error: {e}'}), 500
    return e


@app.after_request
def add_security_headers(response):
    # Baseline security headers (kept permissive to avoid breaking current inline scripts/styles)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')

    # Settings contain live operational state and must never be replayed by a
    # browser, reverse proxy, or CDN. In particular, Telegram tester/runtime
    # rows change immediately after mutations and a stale GET is misleading.
    try:
        if (request.path == '/settings'
                or request.path.startswith('/api/settings/')
                or request.path.startswith('/api/telegram-announcements')
                or request.path.startswith('/api/custom-subscriptions')):
            response.headers['Cache-Control'] = 'private, no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            response.headers['Surrogate-Control'] = 'no-store'
            response.headers.add('Vary', 'Cookie')
    except Exception:
        pass

    # The subscription route (/s/...) must be LIVE for everyone — both the HTML
    # manager page AND the VPN-app config. Force no-store on EVERY /s/ response
    # (all branches/return paths) so neither the browser nor the CDN (WCDN) serves
    # a stale copy. Covers the "mobile shows disabled, desktop active" cache bug.
    try:
        if request.path.startswith('/s/') or request.path.startswith('/cs/'):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
    except Exception:
        pass

    # Finance/package data is edited interactively and must reflect updates
    # immediately. Do not let browsers or proxies reuse stale rows.
    try:
        live_finance_path = (
            request.path == '/finance'
            or request.path.startswith('/api/payments')
            or request.path.startswith('/api/finance/')
        )
        live_package_path = (
            request.path in ('/packages', '/my-packages')
            or request.path.startswith('/api/packages')
            or request.path.startswith('/api/my-packages')
            or request.path.startswith('/api/price-tiers')
            or request.path.startswith('/admin/packages')
            or request.path == '/admin/config'
        )
        if live_finance_path or live_package_path:
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            response.headers['Surrogate-Control'] = 'no-store'
    except Exception:
        pass

    # The CDN (WCDN) replaces ANY non-2xx response with its own HTML error page,
    # throwing away our JSON {success:false, error:"..."} body — so the UI only
    # saw "Server error (HTTP 4xx)" with no reason. For business errors on /api/
    # JSON responses, downgrade the status to 200 so the CDN passes the body
    # through; the real code is kept in X-Eve-Status. Auth/404/5xx are untouched.
    try:
        if (response.status_code in (400, 402, 409, 422)
                and (response.content_type or '').startswith('application/json')
                and (request.path or '').startswith('/api/')):
            _payload = response.get_json(silent=True)
            if isinstance(_payload, dict) and _payload.get('success') is False:
                response.headers['X-Eve-Status'] = str(response.status_code)
                response.status_code = 200
    except Exception:
        pass

    nonce = getattr(g, 'csp_nonce', None) or ''
    
    # Debug endpoint
    # print(f"DEBUG: endpoint={getattr(request, 'endpoint', '')}", flush=True)

    # All assets are local by default. Subscription page can optionally allow external
    # online-chat widget domains when an active chat script is configured.
    allow_external_chat = bool(getattr(g, 'allow_external_chat_widget', False))
    script_src_extra = " https:" if allow_external_chat else ""
    connect_src_extra = " https: wss:" if allow_external_chat else ""
    frame_src_part = "frame-src 'self' https:; " if allow_external_chat else ""

    style_src = f"style-src 'self' 'nonce-{nonce}'; "
    response.headers.setdefault(
        'Content-Security-Policy',
        (
            "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            f"{frame_src_part}"
            f"{style_src}"
            "style-src-attr 'unsafe-inline'; "
            f"script-src 'self' 'nonce-{nonce}'{script_src_extra}; "
            "script-src-attr 'unsafe-inline'; "
            f"connect-src 'self'{connect_src_extra}"
        )
    )
    return response
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_recycle': 1800,
    'pool_pre_ping': True,
    'pool_size': 15,
    'max_overflow': 5,
    'pool_timeout': 10,
}
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=(os.environ.get('SESSION_COOKIE_SAMESITE') or ('Lax' if _is_dev_mode() else 'Strict')),
    SESSION_COOKIE_SECURE=((os.environ.get('SESSION_COOKIE_SECURE') or '').strip().lower() in ('1', 'true', 'yes', 'on'))
    if (os.environ.get('SESSION_COOKIE_SECURE') is not None)
    else False
)

RECEIPT_ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'heic', 'heif', 'pdf'}
RECEIPTS_DIR = os.path.join(app.instance_path, 'receipts')
os.makedirs(RECEIPTS_DIR, exist_ok=True)

BACKUP_DIR = os.path.join(app.instance_path, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)

# --- Backup services (extracted to panel.services.backup; re-exported here) ---
from panel.services.backup import (  # noqa: F401
    TELEGRAM_BACKUP_DEFAULT_INTERVAL_MINUTES,
    TELEGRAM_BACKUP_LOCK,
    TELEGRAM_BACKUP_MAX_INTERVAL_MINUTES,
    TELEGRAM_UPLOAD_CONNECT_TIMEOUT_SECONDS,
    TELEGRAM_UPLOAD_READ_TIMEOUT_SECONDS,
    TELEGRAM_UPLOAD_RETRIES,
    _ANALYTICS_EXCLUDE_TABLES,
    _build_telegram_backup_caption,
    _build_telegram_panel_backup_caption,
    _build_telegram_proxies,
    _check_proxy_reachable,
    _collect_backup_endpoints,
    _content_disposition_filename,
    _create_database_backup_file,
    _create_full_migration_zip,
    _db_uri,
    _extract_backup_bytes_from_response,
    _extract_backup_payload_from_json,
    _fetch_xui_backup,
    _get_system_setting_value,
    _get_telegram_backup_settings,
    _guess_backup_extension,
    _inject_proxy_credentials,
    _is_postgres_db,
    _is_sqlite_db,
    _is_sqlite_payload,
    _migration_file_dirs,
    _normalize_proxy_url,
    _parse_int,
    _parse_iso_datetime,
    _pg_dump_backup,
    _pg_env_from_uri,
    _pg_reset_public_schema,
    _pg_restore_backup,
    _pg_restore_jobs,
    _restore_full_migration_zip,
    _run_telegram_backup,
    _set_system_setting_value,
    _telegram_backup_route_proxies,
    _telegram_get_me,
    _telegram_send_document,
    _try_base64_decode,
    init_backup_tmp_dir,
)
TELEGRAM_BACKUP_TMP_DIR = init_backup_tmp_dir(app)

# Extensions are constructed unbound in panel.extensions and bound here, so
# modules (models, workers, services) can import db/limiter without the app.
from panel.extensions import db, limiter  # noqa: F401
limiter.init_app(app)
db.init_app(app)

from panel.core.logging_config import setup_logging, get_logger  # noqa: E402
setup_logging(app)
logger = get_logger(__name__)

# --- MODELS --- (extracted to panel.models; re-exported here for compatibility)
from panel.models import *  # noqa: F401,F403

RENEW_TEMPLATE_SETTING_KEY = 'renew_template'
DEFAULT_RENEW_TEMPLATE = """🔰{email}\n⌛{days_label} 📊{volume_label}{if_gift}\n🎁 +{gift_volume} گیگ هدیه{/if_gift}\nتمدید شد"""

MONITOR_SETTINGS_KEY = 'monitor_settings'
GENERAL_TIMEZONE_SETTING_KEY = 'general_timezone'
GENERAL_CALENDAR_SETTING_KEY = 'general_calendar'
PANEL_UI_LANG_SETTING_KEY = 'panel_ui_lang'
PANEL_DOMAIN_SETTING_KEY = 'panel_domain'
GENERAL_EXPIRY_WARNING_DAYS_KEY = 'general_expiry_warning_days'
GENERAL_EXPIRY_WARNING_HOURS_KEY = 'general_expiry_warning_hours'
GENERAL_LOW_VOLUME_WARNING_GB_KEY = 'general_low_volume_warning_gb'
DEFAULT_APP_TIMEZONE = 'Asia/Tehran'
DEFAULT_APP_CALENDAR = 'jalali'
# (WhatsApp config keys & bot template defaults moved to panel.jobs.messaging.)
# Template-type constants & defaults moved to panel.routes.templates_api.
from panel.routes.templates_api import (  # noqa: F401
    ROYALTY_INFO_SMS_TEMPLATE_TYPE,
    CLIENT_CREATED_SMS_TEMPLATE_TYPE,
    RENEW_SMS_TEMPLATE_TYPE,
    DEFAULT_ACCOUNT_INFO_WHATSAPP_TEMPLATE,
    DEFAULT_ROYALTY_INFO_SMS_TEMPLATE,
    DEFAULT_CLIENT_CREATED_SMS_TEMPLATE,
    DEFAULT_RENEW_SMS_TEMPLATE,
)

WHATSAPP_CONFIG_KEYS = {
    WHATSAPP_DEPLOYMENT_REGION_KEY,
    WHATSAPP_ENABLED_KEY,
    WHATSAPP_PROVIDER_KEY,
    WHATSAPP_TRIGGER_RENEW_KEY,
    WHATSAPP_TRIGGER_WELCOME_KEY,
    WHATSAPP_TRIGGER_PRE_EXPIRY_KEY,
    WHATSAPP_MIN_INTERVAL_SECONDS_KEY,
    WHATSAPP_DAILY_LIMIT_KEY,
    WHATSAPP_PRE_EXPIRY_HOURS_KEY,
    WHATSAPP_RETRY_COUNT_KEY,
    WHATSAPP_BACKOFF_SECONDS_KEY,
    WHATSAPP_CIRCUIT_BREAKER_KEY,
    WHATSAPP_TEMPLATE_RENEW_KEY,
    WHATSAPP_TEMPLATE_WELCOME_KEY,
    WHATSAPP_TEMPLATE_PRE_EXPIRY_KEY,
    WHATSAPP_GATEWAY_URL_KEY,
    WHATSAPP_GATEWAY_API_KEY,
    WHATSAPP_GATEWAY_TIMEOUT_KEY,
    WHATSAPP_SESSION_KEY,
    WHATSAPP_WARMUP_ENABLED_KEY,
    WHATSAPP_WARMUP_START_DATE_KEY,
    WHATSAPP_WARMUP_START_PER_DAY_KEY,
    WHATSAPP_WARMUP_RAMP_DAYS_KEY,
    WHATSAPP_PACE_ENABLED_KEY,
    WHATSAPP_PACE_MIN_GAP_KEY,
    WHATSAPP_PACE_JITTER_KEY,
    WHATSAPP_DEPLETION_ENABLED_KEY,
    WHATSAPP_DEPLETION_EXPIRY_DAYS_KEY,
    WHATSAPP_DEPLETION_VOLUME_GB_KEY,
    WHATSAPP_DEPLETION_COOLDOWN_DAYS_KEY,
    WHATSAPP_BOT_TPL_CREATED_KEY,
    WHATSAPP_BOT_TPL_RENEW_KEY,
    WHATSAPP_BOT_TPL_ENDED_KEY,
    WHATSAPP_BOT_TPL_INFO_KEY,
}
DEFAULT_MONITOR_SETTINGS = {
    "filters": {
        "warning_days": 3,
        "warning_gb": 2.0,
        "hide_days": 7,
        "debug": False
    },
    "templates": {
        "ended": "مشترک گرامی {user}، حجم سرویس شما به پایان رسیده است.\nلطفا جهت تمدید اقدام فرمایید.",
        "expired": "مشترک گرامی {user}، زمان سرویس شما به پایان رسیده است.\nلطفا جهت تمدید اقدام فرمایید.",
        "low": "مشترک گرامی {user}، تنها {rem} از حجم سرویس شما باقی مانده است.\nتمدید میفرمایید؟",
        "soon": "مشترک گرامی {user}، تنها {time} از زمان سرویس شما باقی مانده است.\nتمدید میفرمایید؟",
        "disabled": "مشترک گرامی {user}، سرویس شما غیرفعال شده است.\nبرای پیگیری با پشتیبانی در تماس باشید.",
        "zero_usage": ""
    }
}

STANDARD_TIMEZONE_OPTIONS = [
    'Asia/Tehran',
    'UTC',
    'Europe/London', 'Europe/Dublin', 'Europe/Paris', 'Europe/Berlin', 'Europe/Amsterdam', 'Europe/Brussels',
    'Europe/Madrid', 'Europe/Rome', 'Europe/Vienna', 'Europe/Prague', 'Europe/Warsaw', 'Europe/Zurich',
    'Europe/Athens', 'Europe/Helsinki', 'Europe/Bucharest', 'Europe/Istanbul', 'Europe/Moscow',
    'Asia/Dubai', 'Asia/Riyadh', 'Asia/Jerusalem', 'Asia/Baghdad', 'Asia/Kuwait', 'Asia/Qatar',
    'Asia/Baku', 'Asia/Tbilisi', 'Asia/Yerevan', 'Asia/Karachi', 'Asia/Kolkata', 'Asia/Dhaka',
    'Asia/Bangkok', 'Asia/Jakarta', 'Asia/Kuala_Lumpur', 'Asia/Singapore', 'Asia/Manila',
    'Asia/Hong_Kong', 'Asia/Shanghai', 'Asia/Taipei', 'Asia/Seoul', 'Asia/Tokyo',
    'Australia/Perth', 'Australia/Adelaide', 'Australia/Sydney', 'Pacific/Auckland',
    'Africa/Cairo', 'Africa/Johannesburg', 'Africa/Nairobi', 'Africa/Lagos',
    'America/St_Johns', 'America/Halifax', 'America/Toronto', 'America/New_York', 'America/Chicago',
    'America/Denver', 'America/Phoenix', 'America/Los_Angeles', 'America/Anchorage', 'Pacific/Honolulu',
    'America/Mexico_City', 'America/Bogota', 'America/Lima', 'America/Caracas', 'America/Sao_Paulo',
    'America/Argentina/Buenos_Aires', 'America/Santiago',
]


def _get_standard_timezone_options() -> list[str]:
    base = []
    seen = set()
    for item in STANDARD_TIMEZONE_OPTIONS:
        key = str(item or '').strip()
        if not key or key in seen:
            continue
        seen.add(key)
        base.append(key)
    return base


def _normalize_timezone_name(value: str | None) -> str | None:
    raw = str(value or '').strip()
    if not raw:
        return None

    lowered = raw.lower()
    for tz_name in _get_standard_timezone_options():
        if tz_name.lower() == lowered:
            return tz_name
    return None


def _get_or_create_system_setting(key: str, default_value: str | None = None) -> str | None:
    """Fetch a SystemSetting value; optionally create with default if missing.

    Keep this safe for request-time usage; only writes when the row is missing.
    """
    setting = db.session.get(SystemSetting, key)
    if setting:
        return setting.value
    if default_value is None:
        return None
    try:
        setting = SystemSetting(key=key, value=str(default_value))
        db.session.add(setting)
        db.session.commit()
    except Exception:
        # Don't fail the request if we can't persist the default.
        try:
            db.session.rollback()
        except Exception:
            pass
    return str(default_value)


# Conditional template blocks: {if_<name>}...{/if_<name>}
# The block is KEPT (markers stripped) when variables['<name>_given'] is truthy
# (falling back to variables['<name>']), otherwise the whole block — along with
# the newline that precedes it — is removed so no blank line is left behind.
# This must run BEFORE str.format(), because the {if_..}/{/if_..} markers are
# not valid format fields and would otherwise raise.
_TEMPLATE_COND_RE = re.compile(r'(\n?)\{if_([a-zA-Z0-9_]+)\}(.*?)\{/if_\2\}', re.DOTALL)


def _apply_template_conditionals(template: str | None, variables: dict) -> str:
    def _sub(m):
        lead, name, inner = m.group(1), m.group(2), m.group(3)
        flag = variables.get(f'{name}_given')
        if flag is None:
            flag = variables.get(name)
        return (lead + inner) if flag else ''
    return _TEMPLATE_COND_RE.sub(_sub, template or '')


class _SafeFormatDict(dict):
    """Missing template placeholders render as '' instead of raising KeyError, so a
    template that references a variable the caller didn't supply degrades to a blank
    rather than dumping the raw, unrendered template (braces and all) to the user."""
    def __missing__(self, key):
        return ''


def _render_text_template(template: str | None, variables: dict) -> str:
    """Render a python-format template with a safe fallback.

    Supports optional {if_<name>}...{/if_<name>} conditional blocks. Unknown
    placeholders are blanked rather than raising, so a partially-matching template
    never leaks raw {tokens} into the message.
    """
    raw = (template or '').strip() or DEFAULT_RENEW_TEMPLATE
    raw = _apply_template_conditionals(raw, variables)
    try:
        return raw.format_map(_SafeFormatDict(variables))
    except Exception:
        # Last resort if the template has malformed syntax (e.g. a stray single brace).
        try:
            return _apply_template_conditionals(DEFAULT_RENEW_TEMPLATE, variables).format_map(_SafeFormatDict(variables))
        except Exception:
            return DEFAULT_RENEW_TEMPLATE


def _normalize_monitor_settings(payload: dict | None) -> dict:
    data = payload if isinstance(payload, dict) else {}
    defaults = copy.deepcopy(DEFAULT_MONITOR_SETTINGS)

    filters = data.get('filters') if isinstance(data.get('filters'), dict) else {}
    templates = data.get('templates') if isinstance(data.get('templates'), dict) else {}

    defaults['filters']['warning_days'] = _parse_int(
        filters.get('warning_days'),
        defaults['filters']['warning_days'],
        min_value=1,
        max_value=365
    )
    try:
        defaults['filters']['warning_gb'] = float(filters.get('warning_gb', defaults['filters']['warning_gb']))
    except Exception:
        defaults['filters']['warning_gb'] = DEFAULT_MONITOR_SETTINGS['filters']['warning_gb']
    defaults['filters']['warning_gb'] = max(0.1, min(defaults['filters']['warning_gb'], 1024.0))

    defaults['filters']['hide_days'] = _parse_int(
        filters.get('hide_days'),
        defaults['filters']['hide_days'],
        min_value=0,
        max_value=365
    )
    defaults['filters']['debug'] = bool(filters.get('debug', defaults['filters']['debug']))

    for key in defaults['templates'].keys():
        val = templates.get(key)
        if isinstance(val, str) and val.strip():
            defaults['templates'][key] = val.strip()

    return defaults


def _get_monitor_settings() -> dict:
    raw = _get_or_create_system_setting(
        MONITOR_SETTINGS_KEY,
        json.dumps(DEFAULT_MONITOR_SETTINGS, ensure_ascii=False)
    )
    try:
        parsed = json.loads(raw) if raw else {}
    except Exception:
        parsed = {}
    return _normalize_monitor_settings(parsed)


def _is_valid_timezone_name(value: str | None) -> bool:
    tz_name = _normalize_timezone_name(value)
    if not tz_name:
        return False

    # If tz database is unavailable in runtime, still accept curated standard names.
    if ZoneInfo is None:
        return True

    try:
        ZoneInfo(tz_name)
        return True
    except Exception:
        # Some environments miss tzdata; allow curated values for UX consistency.
        return tz_name in _get_standard_timezone_options()


def _get_app_timezone_name() -> str:
    stored = _get_or_create_system_setting(GENERAL_TIMEZONE_SETTING_KEY, DEFAULT_APP_TIMEZONE)
    normalized = _normalize_timezone_name(stored)
    if _is_valid_timezone_name(normalized):
        return str(normalized).strip()
    return DEFAULT_APP_TIMEZONE


def _normalize_calendar_name(value: str | None) -> str | None:
    normalized = str(value or '').strip().lower()
    aliases = {
        'jalali': 'jalali', 'persian': 'jalali', 'solar_hijri': 'jalali',
        'gregorian': 'gregorian', 'gregory': 'gregorian', 'miladi': 'gregorian',
    }
    return aliases.get(normalized)


def _get_app_calendar_name() -> str:
    stored = _get_or_create_system_setting(GENERAL_CALENDAR_SETTING_KEY, DEFAULT_APP_CALENDAR)
    return _normalize_calendar_name(stored) or DEFAULT_APP_CALENDAR


def _normalize_ui_lang(value: str | None, default: str = 'en') -> str:
    raw = (value or '').strip().lower()
    if raw in ('fa', 'en'):
        return raw
    return default


def _get_panel_ui_lang() -> str:
    stored = _get_or_create_system_setting(PANEL_UI_LANG_SETTING_KEY, 'en')
    return _normalize_ui_lang(stored, default='en')


def _get_dashboard_status_thresholds() -> dict:
    raw_days = _get_or_create_system_setting(GENERAL_EXPIRY_WARNING_DAYS_KEY, '3')
    raw_hours = _get_or_create_system_setting(GENERAL_EXPIRY_WARNING_HOURS_KEY, '0')
    raw_gb = _get_or_create_system_setting(GENERAL_LOW_VOLUME_WARNING_GB_KEY, '1')

    near_expiry_days = _parse_int(raw_days, 3, min_value=0, max_value=365)
    near_expiry_hours = _parse_int(raw_hours, 0, min_value=0, max_value=23)

    try:
        low_volume_gb = float(raw_gb if raw_gb is not None else 1.0)
    except Exception:
        low_volume_gb = 1.0
    low_volume_gb = max(0.01, min(low_volume_gb, 1024.0))

    return {
        'near_expiry_days': near_expiry_days,
        'near_expiry_hours': near_expiry_hours,
        'low_volume_gb': low_volume_gb,
    }


def _compute_client_service_state(*, enabled: bool, total_bytes: int, remaining_bytes: int | None, expiry_ts: int, expiry_info: dict, thresholds: dict, lang: str = 'en') -> dict:
    is_fa = _normalize_ui_lang(lang, default='en') == 'fa'

    labels = {
        'active': 'فعاله' if is_fa else 'Active',
        'inactive': 'غیرفعال' if is_fa else 'Inactive',
        'expired': 'منقضی شده' if is_fa else 'Expired',
        'volume_low': 'حجم رو به اتمامه' if is_fa else 'Low Volume',
        'expiring_soon': 'انقضا نزدیکه' if is_fa else 'Expiring Soon',
        'volume_ended': 'حجم تمام کردی' if is_fa else 'Volume Ended',
    }

    low_volume_threshold_gb = float((thresholds or {}).get('low_volume_gb') or 1.0)
    near_expiry_days = int((thresholds or {}).get('near_expiry_days') or 0)
    near_expiry_hours = int((thresholds or {}).get('near_expiry_hours') or 0)
    near_expiry_ms = ((near_expiry_days * 24) + near_expiry_hours) * 3600 * 1000

    # Reasons that hold regardless of the enable flag. Sanaei-style panels flip
    # enable=False the moment time or traffic runs out, so checking these BEFORE
    # the bare enable flag is what lets us show the real reason (expired / volume
    # ended) instead of a generic "inactive" for auto-disabled accounts.
    if total_bytes > 0 and remaining_bytes is not None and remaining_bytes <= 0:
        return {'key': 'volume_ended', 'label': labels['volume_ended'], 'emoji': '🚫', 'tag': 'ended'}

    if str((expiry_info or {}).get('type') or '').lower() == 'expired':
        return {'key': 'expired', 'label': labels['expired'], 'emoji': '⛔', 'tag': 'expired'}

    # Past time/traffic checks: a still-disabled account was turned off manually.
    if not enabled:
        return {'key': 'inactive', 'label': labels['inactive'], 'emoji': '⏸️', 'tag': 'inactive'}

    if total_bytes > 0 and remaining_bytes is not None:
        remaining_gb = float(remaining_bytes) / (1024 ** 3)
        if remaining_gb <= low_volume_threshold_gb:
            return {'key': 'volume_low', 'label': labels['volume_low'], 'emoji': '⚠️', 'tag': 'low'}

    if expiry_ts and expiry_ts > 0 and str((expiry_info or {}).get('type') or '').lower() not in ('unlimited', 'start_after_use'):
        now_ms = int(time.time() * 1000)
        remaining_ms = expiry_ts - now_ms
        if remaining_ms > 0 and near_expiry_ms > 0 and remaining_ms <= near_expiry_ms:
            return {'key': 'expiring_soon', 'label': labels['expiring_soon'], 'emoji': '⏳', 'tag': 'soon'}

    return {'key': 'active', 'label': labels['active'], 'emoji': '✅', 'tag': 'ok'}



def _get_system_config_text(key: str, default: str = '') -> str:
    conf = db.session.get(SystemConfig, key)
    if not conf or conf.value is None:
        return default
    return str(conf.value)


def _get_system_config_int(key: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    return _parse_int(_get_system_config_text(key, str(default)), default, min_value=min_value, max_value=max_value)


def _get_system_config_bool(key: str, default: bool = False) -> bool:
    return _parse_bool(_get_system_config_text(key, 'true' if default else 'false'))


def _get_system_configs_batch(keys: list) -> dict:
    if not keys:
        return {}
    rows = SystemConfig.query.filter(SystemConfig.key.in_(keys)).all()
    result = {r.key: r.value for r in rows}
    for k in keys:
        if k not in result:
            result[k] = None
    return result


# (messaging workers extracted to panel.jobs.messaging — imported above.)
def _get_app_tzinfo():
    tz_name = _get_app_timezone_name()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    # Fallback when zoneinfo database is unavailable
    return timezone(timedelta(hours=3, minutes=30))


def _to_app_timezone(dt: datetime | None):
    if not dt:
        return None
    app_tz = _get_app_tzinfo()
    try:
        if dt.tzinfo is None:
            dt_utc = dt.replace(tzinfo=timezone.utc)
        else:
            dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.astimezone(app_tz)
    except Exception:
        return dt

# (Finance/ownership models extracted to panel.models.finance — imported at MODELS above.)
# Ownership-claim review/verification extracted to panel.services.ownership.
from panel.services.ownership import (  # noqa: F401
    _can_review_ownership_claim,
    _refresh_ownership_claim_status,
    review_ownership_claim_item,
    discover_phone_ownership_claim,
    _live_client_for_claim_item,
    verify_ownership_claim_subscription,
)


# (Telegram bot models extracted to panel.models.telegram — imported at MODELS above.)
# (Ops/monitoring models extracted to panel.models.ops — imported at MODELS above.)
def _add_health_log(level, category, message, action_taken=None, details=None, resolved=False):
    """Helper to insert a HealthLog row safely."""
    try:
        log_entry = HealthLog(
            level=level,
            category=category,
            message=message,
            action_taken=action_taken,
            details=json.dumps(details) if isinstance(details, (dict, list)) else details,
            resolved=resolved,
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning("Failed to write health log: %s", exc)


CLIENT_UPDATE_FALLBACKS = [
    "/panel/api/inbounds/updateClient/:clientId",
    "/panel/api/inbounds/:id/updateClient/:clientId",
    "/xui/API/inbounds/updateClient/:clientId",
    "/xui/inbound/updateClient/:clientId"
]

CLIENT_RESET_FALLBACKS = [
    "/panel/api/inbounds/:id/resetClientTraffic/:email",
    "/xui/API/inbounds/:id/resetClientTraffic/:email",
    "/xui/inbounds/:id/resetClientTraffic/:email",
    "/xui/inbound/:id/resetClientTraffic/:email"
]

CLIENT_DELETE_FALLBACKS = [
    "/panel/api/inbounds/:id/delClient/:clientId",
    "/xui/API/inbounds/:id/delClient/:clientId",
    "/xui/inbound/delClient/:clientId"
]


INBOUND_GET_FALLBACKS = [
    "/panel/api/inbounds/get/:id",
    "/xui/API/inbounds/get/:id",
    "/xui/inbound/get/:id",
    "/xui/inbounds/get/:id",
]


INBOUND_UPDATE_FALLBACKS = [
    "/panel/api/inbounds/update/:id",
    "/xui/API/inbounds/update/:id",
    "/xui/inbound/update/:id",
    "/xui/inbounds/update/:id",
]


def _json_field(value, default=None):
    """Parse an x-ui inbound field (settings / streamSettings / sniffing) that may
    arrive as a JSON-encoded STRING (x-ui v2 / Sanaei / Alireza) or as a nested
    JSON OBJECT (3x-ui v3+, which returns these fields already decoded).

    Returns a dict/list; falls back to `default` on anything unparseable.
    This is the single compatibility shim that lets the same code path work
    against both old and new panels.
    """
    if default is None:
        default = {}
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default
    return default


def collect_endpoint_templates(panel_type, attr_name, fallbacks):
    """Return ordered list of endpoint templates for the requested action."""
    templates = []

    normalized = (panel_type or 'auto').strip().lower()

    # If panel type is known, prefer its configured endpoint first.
    panel_api = get_panel_api(normalized)
    if panel_api:
        value = getattr(panel_api, attr_name, None)
        if value:
            templates.append(value)

    # Fast path: try hardcoded fallbacks early (especially important for panel_type='auto').
    for item in (fallbacks or []):
        if item and item not in templates:
            templates.append(item)

    # Finally, include any configured endpoints from other panel types.
    # This keeps compatibility with custom PanelAPI rows without making 'auto' slow.
    for api in PanelAPI.query.all():
        value = getattr(api, attr_name, None)
        if value and value not in templates:
            templates.append(value)

    return templates


def build_panel_url(host, template, replacements):
    if not template:
        return None
    endpoint = template
    for key, value in (replacements or {}).items():
        if value is None:
            continue
        safe_value = quote(str(value), safe='')
        endpoint = endpoint.replace(f":{key}", safe_value).replace(f"{{{key}}}", safe_value)
    if endpoint.startswith('http://') or endpoint.startswith('https://'):
        return endpoint
    base, webpath = extract_base_and_webpath(host)
    endpoint_clean = endpoint if endpoint.startswith('/') else f"/{endpoint}"
    return f"{base}{webpath}{endpoint_clean}"


# ── SSL auto-detection (defined early so startup context block can use it) ──

_SSL_KNOWN_PATHS = [
    # Copied by setup.sh / ssl/sync endpoint — evemgr-owned, always readable
    ('/etc/ssl/eve-manager/fullchain.pem', '/etc/ssl/eve-manager/privkey.pem'),
    # Self-signed via setup.sh
    ('/etc/ssl/eve-manager/cert.pem',      '/etc/ssl/eve-manager/privkey.pem'),
]

def _is_ip_address(value: str) -> bool:
    import re as _re
    return bool(_re.match(r'^(\d{1,3}\.){3}\d{1,3}$', (value or '').strip()))








def _autodetect_ssl_paths():
    """Return (cert_path, key_path) from well-known locations, or ('', '').

    Detection order:
    1. Known static paths under /etc/ssl/eve-manager/ (evemgr-readable)
    2. Nginx config ssl_certificate directive (most reliable)
    3. Let's Encrypt live glob
    """
    for cert_cand, key_cand in _SSL_KNOWN_PATHS:
        if os.path.isfile(cert_cand) and os.path.isfile(key_cand):
            return cert_cand, key_cand

    # Read nginx config to discover paths
    try:
        import re as _re
        _service = os.environ.get('SERVICE_NAME', 'eve-manager')
        _nginx_candidates = [
            f'/etc/nginx/sites-available/{_service}',
            f'/etc/nginx/sites-enabled/{_service}',
            '/etc/nginx/sites-available/eve-manager',
            '/etc/nginx/sites-available/eve-xui-manager',
            '/etc/nginx/sites-enabled/eve-manager',
            '/etc/nginx/conf.d/eve-manager.conf',
        ]
        for _nc in _nginx_candidates:
            if not os.path.isfile(_nc):
                continue
            try:
                with open(_nc, 'r', errors='ignore') as _nf:
                    _conf = _nf.read()
                _cm = _re.search(r'ssl_certificate\s+([^;]+);', _conf)
                _km = _re.search(r'ssl_certificate_key\s+([^;]+);', _conf)
                if _cm and _km:
                    _det_cert = _cm.group(1).strip()
                    _det_key = _km.group(1).strip()
                    # Only return if actually readable by the current process
                    if os.path.isfile(_det_cert) and os.access(_det_cert, os.R_OK) \
                            and os.path.isfile(_det_key) and os.access(_det_key, os.R_OK):
                        return _det_cert, _det_key
            except Exception:
                continue
    except Exception:
        pass

    # Let's Encrypt glob fallback (may fail due to permissions on privkey)
    try:
        import glob as _glob
        for _le_cert in sorted(_glob.glob('/etc/letsencrypt/live/*/fullchain.pem')):
            _le_key = os.path.join(os.path.dirname(_le_cert), 'privkey.pem')
            if os.path.isfile(_le_key) and os.access(_le_cert, os.R_OK) and os.access(_le_key, os.R_OK):
                return _le_cert, _le_key
    except Exception:
        pass

    return '', ''


# Schema migrations & seeds now run through panel.migrate (single, locked runner).
from panel.migrate import _migrate_add_columns, run_migrations  # noqa: F401

if os.environ.get('EVE_SKIP_IMPORT_MIGRATIONS') != '1':
    with app.app_context():
        run_migrations()
# --- HELPERS ---

# Auth guards extracted to panel.routes.common (re-exported for compatibility).
from panel.routes.common import (  # noqa: F401
    login_required,
    client_portal_required,
    superadmin_required,
    user_management_required,
)


def _normalize_username(raw: str | None) -> str:
    username = (raw or '').strip().lower()
    username = re.sub(r'\s+', '', username)
    username = re.sub(r'[\u0600-\u06FF]', '', username)
    return username


def _validate_username(username: str) -> str | None:
    if not username:
        return 'Username is required'
    if ' ' in username:
        return 'Username cannot contain spaces'
    if any(u'\u0600' <= c <= u'\u06FF' for c in username):
        return 'Persian characters are not allowed'
    return None


def ensure_reseller_allowed_for_assignment(reseller: 'Admin', server_id: int, inbound_id: int | None) -> None:
    """Ensure reseller.allowed_servers includes the given server+inbound.

    This keeps the "Allowed Servers" UI in sync with actual assignments.
    It only ever *adds* permissions; it does not remove them on unassign.
    """
    try:
        if not reseller or reseller.role != 'reseller':
            return
        if reseller.allowed_servers == '*':
            return

        sid = int(server_id)
        inb = int(inbound_id) if inbound_id is not None else None

        allowed_map = resolve_allowed_map(reseller.allowed_servers)
        if allowed_map == '*':
            return

        current = allowed_map.get(sid)
        if current == '*':
            return

        if inb is None:
            allowed_map[sid] = '*'
        else:
            cur_set = set()
            if isinstance(current, (set, list, tuple)):
                for v in current:
                    try:
                        cur_set.add(int(v))
                    except Exception:
                        continue
            cur_set.add(inb)
            allowed_map[sid] = cur_set

        payload = []
        for s, rule in allowed_map.items():
            if rule == '*':
                payload.append({'server_id': int(s), 'inbounds': '*'})
            else:
                try:
                    payload.append({'server_id': int(s), 'inbounds': sorted([int(v) for v in (rule or [])])})
                except Exception:
                    payload.append({'server_id': int(s), 'inbounds': []})

        reseller.allowed_servers = serialize_allowed_servers(payload)
    except Exception:
        # Best-effort; assignment should not fail because of permissions sync.
        return

def validate_password_strength(password):
    """
    Validates password strength:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one number
    """
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    if not any(not c.isalnum() for c in password):
        return False, "Password must contain at least one special character"
    return True, None

# (Billing/recommendation cluster extracted to panel.services.billing.)
from panel.services.billing import (  # noqa: F401
    calculate_reseller_price,
    _build_sub_page_packages,
    _select_subscription_package,
    _live_subscription_usage,
    _build_subscription_package_recommendation,
    RECOMMENDATION_TEMPLATE_TOKENS,
    _template_wants_recommendation,
    _empty_recommendation_template_vars,
    _recommendation_template_vars,
    get_config,
    log_transaction,
    inject_wallet_credit,
)
app.context_processor(inject_wallet_credit)

def format_app_datetime(dt):
    if not dt:
        return None
    try:
        dt_local = _to_app_timezone(dt)
        if not dt_local:
            return None
        if _get_app_calendar_name() == 'gregorian':
            return dt_local.strftime('%Y/%m/%d %H:%M')
        calendar_date = jdatetime_class.fromgregorian(datetime=dt_local.replace(tzinfo=None))
        return calendar_date.strftime('%Y/%m/%d %H:%M')
    except Exception:
        return dt.isoformat() if dt else None


def format_jalali(dt):
    """Backward-compatible display formatter honoring the global calendar setting."""
    return format_app_datetime(dt)

_DIGIT_TRANSLATION = str.maketrans({
    '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4', '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
})


def parse_jalali_date(date_str, end_of_day=False):
    """Parse a panel date using the configured calendar and timezone into naive UTC."""
    if not date_str:
        return None
    normalized = str(date_str).strip().translate(_DIGIT_TRANSLATION).replace('T', ' ')
    if not normalized:
        return None
    patterns = ['%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M', '%Y/%m/%d', '%Y-%m-%d']
    for pattern in patterns:
        try:
            if _get_app_calendar_name() == 'gregorian':
                gregorian = datetime.strptime(normalized, pattern)
            else:
                gregorian = jdatetime_class.strptime(normalized, pattern).togregorian()
            dt = None
            if 'H' not in pattern:
                day = gregorian.date()
                time_part = datetime.max.time() if end_of_day else datetime.min.time()
                dt = datetime.combine(day, time_part)
            else:
                dt = gregorian

            app_tz = _get_app_tzinfo()
            return dt.replace(tzinfo=app_tz).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None

def parse_allowed_servers(raw_value):
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        return raw_value
    normalized = str(raw_value).strip()
    if normalized == '*':
        return '*'
    if normalized.startswith('"') and normalized.endswith('"'):
        inner = normalized.strip('"')
        if inner == '*':
            return '*'
        # Attempt to decode double-encoded JSON strings
        try:
            decoded_inner = json.loads(inner)
            return decoded_inner if decoded_inner is not None else []
        except Exception:
            pass
    try:
        parsed = json.loads(normalized)
        if isinstance(parsed, str) and parsed.strip() == '*':
            return '*'
        return parsed if isinstance(parsed, list) else parsed
    except Exception:
        return []

def serialize_allowed_servers(value):
    """Serialize server/inbound permissions.

    Supports legacy formats (list of server ids or '*') and new structured
    payloads like [{'server_id': 1, 'inbounds': [10, 12]}, ...].
    """

    def _normalize_inbounds(val):
        if val is None:
            return '*'
        if val == '*' or (isinstance(val, str) and val.strip() == '*'):
            return '*'
        if isinstance(val, list):
            normalized = []
            for v in val:
                try:
                    normalized.append(int(v))
                except (TypeError, ValueError):
                    continue
            return normalized
        try:
            return [int(val)]
        except (TypeError, ValueError):
            return []

    if value == '*' or (isinstance(value, str) and value.strip() == '*'):
        return '*'

    # Parse JSON strings if provided
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return serialize_allowed_servers(parsed)
        except Exception:
            # Fallback for simple comma-separated server IDs
            parts = [p.strip() for p in value.split(',') if p.strip()]
            try:
                return serialize_allowed_servers([int(p) for p in parts])
            except Exception:
                return json.dumps([])

    if isinstance(value, dict):
        value = [value]

    normalized = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                server_id = item.get('server_id') or item.get('server') or item.get('id')
                try:
                    server_id = int(server_id)
                except (TypeError, ValueError):
                    continue
                inbounds = _normalize_inbounds(item.get('inbounds', '*'))
                normalized.append({'server_id': server_id, 'inbounds': '*' if inbounds == '*' else inbounds})
            else:
                try:
                    server_id = int(item)
                    normalized.append({'server_id': server_id, 'inbounds': '*'})
                except (TypeError, ValueError):
                    continue

    if not normalized:
        return json.dumps([])

    # Merge duplicates and keep unique inbound lists per server
    merged = {}
    for entry in normalized:
        sid = entry['server_id']
        inb = entry['inbounds']
        if sid not in merged:
            merged[sid] = inb
            continue
        existing = merged[sid]
        if existing == '*' or inb == '*':
            merged[sid] = '*'
        else:
            merged[sid] = sorted(list(set(existing) | set(inb)))

    final_list = [{'server_id': sid, 'inbounds': val} for sid, val in merged.items()]
    return json.dumps(final_list)

def resolve_allowed_servers(raw_value):
    """Backward-compatible resolver returning only server IDs or '*'."""
    allowed_map = resolve_allowed_map(raw_value)
    if allowed_map == '*':
        return '*'
    return list(allowed_map.keys())


def resolve_allowed_map(raw_value):
    """Return mapping of server_id -> inbound rule ('*' or set of ids)."""
    parsed = parse_allowed_servers(raw_value)
    if parsed == '*':
        return '*'

    allowed_map = {}
    items = parsed
    if isinstance(items, dict):
        items = [items]

    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                server_id = item.get('server_id') or item.get('server') or item.get('id')
                try:
                    server_id = int(server_id)
                except (TypeError, ValueError):
                    continue
                inbounds_raw = item.get('inbounds', '*')
                inbounds = set()
                if inbounds_raw == '*' or (isinstance(inbounds_raw, str) and inbounds_raw.strip() == '*'):
                    allowed_map[server_id] = '*'
                    continue
                if isinstance(inbounds_raw, list):
                    for v in inbounds_raw:
                        try:
                            inbounds.add(int(v))
                        except (TypeError, ValueError):
                            continue
                else:
                    try:
                        inbounds.add(int(inbounds_raw))
                    except (TypeError, ValueError):
                        pass
                allowed_map[server_id] = inbounds
            else:
                try:
                    sid = int(item)
                    allowed_map[sid] = '*'
                except (TypeError, ValueError):
                    continue
    return allowed_map


def get_reseller_access_maps(user):
    """Return (allowed_map, assignment_map) for a reseller user."""
    if not user or user.role != 'reseller':
        return '*', {}

    allowed_map = resolve_allowed_map(user.allowed_servers)
    assignments = defaultdict(set)

    ownerships = ClientOwnership.query.filter_by(reseller_id=user.id).all()
    for own in ownerships:
        try:
            sid = int(own.server_id)
        except (TypeError, ValueError):
            continue
        if own.inbound_id is not None:
            try:
                assignments[sid].add(int(own.inbound_id))
            except (TypeError, ValueError):
                continue

    return allowed_map, assignments


def is_server_accessible(server_id, allowed_map, assignments):
    if allowed_map == '*':
        return True
    if server_id in assignments:
        return True
    return server_id in allowed_map


def is_inbound_accessible(server_id, inbound_id, allowed_map, assignments):
    if allowed_map == '*':
        return True

    # Access via explicit assignment
    assigned = assignments.get(server_id, set())
    if '*' in assigned or inbound_id in assigned:
        return True

    server_rule = allowed_map.get(server_id)
    if server_rule == '*':
        return True
    if isinstance(server_rule, (set, list, tuple)):
        return inbound_id in server_rule
    return False

def parse_iso_datetime(value):
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)
    except Exception:
        try:
            # fallback for "2024-12-01 12:00"
            return datetime.strptime(value, '%Y-%m-%d %H:%M')
        except Exception:
            return None

def allowed_receipt_file(file_storage):
    # Check extension
    if not file_storage or not file_storage.filename or '.' not in file_storage.filename:
        return False
    
    # Check actual file type (best-effort). On some platforms (notably Windows)
    # python-magic may be installed without libmagic, so we fall back to
    # extension-only checks instead of crashing the app.
    mime = None
    if magic is not None:
        try:
            file_bytes = file_storage.read(2048)
            file_storage.seek(0)
            mime = magic.from_buffer(file_bytes, mime=True)
        except Exception:
            mime = None
    
    allowed_mimes = {'image/jpeg', 'image/png', 'image/webp', 'image/heic', 'application/pdf'}
    
    ext = file_storage.filename.rsplit('.', 1)[1].lower()
    if ext not in RECEIPT_ALLOWED_EXTENSIONS:
        return False

    # If we couldn't detect MIME, allow based on extension only.
    if not mime:
        return True

    return mime in allowed_mimes

def save_receipt_file(file_storage):
    if not file_storage or not allowed_receipt_file(file_storage):
        return None
    ext = file_storage.filename.rsplit('.', 1)[1].lower()
    subdir = datetime.utcnow().strftime('%Y/%m')
    dest_dir = os.path.join(RECEIPTS_DIR, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    safe_name = secure_filename(unique_name)
    relative_path = os.path.join('receipts', subdir, safe_name)
    full_path = os.path.join(app.instance_path, relative_path)
    file_storage.save(full_path)
    return relative_path

def get_active_auto_window(now=None):
    now = now or datetime.utcnow()
    return AutoApprovalWindow.query.filter(
        AutoApprovalWindow.status == 'enabled',
        AutoApprovalWindow.starts_at <= now,
        AutoApprovalWindow.ends_at >= now
    ).order_by(AutoApprovalWindow.ends_at.asc()).first()

def apply_receipt_credit(receipt, reviewer=None, auto=False):
    owner = db.session.get(Admin, receipt.admin_id)
    if not owner:
        return False, 'Owner not found'
    owner.credit = (owner.credit or 0) + receipt.amount
    tx_type = 'manual_receipt_auto' if auto else 'manual_receipt'
    description = f"Receipt #{receipt.id}"
    log_transaction(owner.id, receipt.amount, tx_type, description)
    receipt.status = RECEIPT_STATUS_AUTO_APPROVED if auto else RECEIPT_STATUS_APPROVED
    receipt.reviewed_at = datetime.utcnow()
    receipt.reviewer_id = reviewer.id if reviewer else None
    receipt.auto_deadline = None
    receipt.rejection_reason = None
    return True, None

def rollback_receipt_credit(receipt, reviewer=None, reason=None):
    owner = db.session.get(Admin, receipt.admin_id)
    if not owner:
        return False, 'Owner not found'
    owner.credit = (owner.credit or 0) - receipt.amount
    log_transaction(owner.id, -receipt.amount, 'manual_receipt_reversal', f"Receipt #{receipt.id} rejected")
    receipt.reviewer_id = reviewer.id if reviewer else None
    receipt.reviewed_at = datetime.utcnow()
    receipt.rejection_reason = reason
    return True, None

def trigger_auto_receipt_processing():
    now = datetime.utcnow()
    due_receipts = ManualReceipt.query.filter(
        ManualReceipt.status == RECEIPT_STATUS_AUTO_PENDING,
        ManualReceipt.auto_deadline.isnot(None),
        ManualReceipt.auto_deadline <= now
    ).all()
    updated = 0
    for receipt in due_receipts:
        success, err = apply_receipt_credit(receipt, reviewer=None, auto=True)
        if success:
            updated += 1
        else:
            receipt.status = RECEIPT_STATUS_PENDING
            receipt.auto_deadline = None
            receipt.rejection_reason = err
    if updated or due_receipts:
        db.session.commit()

def format_bytes(size):
    if size is None or size == 0: return "0 B"
    power = 2**10
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size >= power and n < 4:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"

def format_bytes_gb_tb(size):
    """Formats bytes to GB or TB only."""
    if size is None or size == 0: return "0 GB"
    
    gb_val = size / (1024**3)
    if gb_val >= 1024:
        tb_val = gb_val / 1024
        return f"{tb_val:.2f} TB"
    else:
        return f"{gb_val:.2f} GB"

def format_remaining_days(timestamp, lang: str = 'en'):
    is_fa = _normalize_ui_lang(lang, default='en') == 'fa'

    def _fmt_future(days: int, hours: int, minutes: int) -> str:
        if is_fa:
            parts = []
            if days > 0:
                parts.append(f"{days} روز")
            if hours > 0:
                parts.append(f"{hours} ساعت")
            if minutes > 0 and not parts:
                parts.append(f"{minutes} دقیقه")
            if not parts:
                return "امروز"
            return f"{' و '.join(parts)} باقی مانده"

        if days > 0 and hours > 0:
            return f"{days}d {hours}h left"
        if days > 0:
            return f"{days}d left"
        if hours > 0:
            return f"{hours}h left"
        if minutes > 0:
            return f"{minutes}m left"
        return "Today"

    def _fmt_expired(days_ago: int, hours_ago: int) -> str:
        if is_fa:
            if days_ago > 0 and hours_ago > 0:
                ago_label = f"{days_ago} روز و {hours_ago} ساعت پیش"
            elif days_ago > 0:
                ago_label = f"{days_ago} روز پیش"
            elif hours_ago > 0:
                ago_label = f"{hours_ago} ساعت پیش"
            else:
                ago_label = "لحظاتی پیش"
            return f"منقضی شده ({ago_label})"

        if days_ago > 0 and hours_ago > 0:
            ago_label = f"{days_ago}d {hours_ago}h ago"
        elif days_ago > 0:
            ago_label = f"{days_ago}d ago"
        elif hours_ago > 0:
            ago_label = f"{hours_ago}h ago"
        else:
            ago_label = "just now"
        return f"Expired ({ago_label})"

    # Some code paths (e.g. cached client objects) may pass expiry as string
    # like "Unlimited" or a numeric string. Normalize to int milliseconds.
    if isinstance(timestamp, str):
        ts = timestamp.strip()
        if not ts:
            timestamp = 0
        else:
            try:
                timestamp = int(float(ts))
            except Exception:
                # Non-numeric strings (e.g. "Unlimited", "Expired ...")
                timestamp = 0

    if timestamp == 0 or timestamp is None:
        return {"text": ("نامحدود" if is_fa else "Unlimited"), "days": -1, "type": "unlimited"}
    if timestamp < 0:
        days = abs(timestamp) // 86400000
        if is_fa:
            text = (f"{days} روز بعد از اولین اتصال" if days > 0 else "بعد از اولین اتصال")
        else:
            text = (f"Not started ({days} days)" if days > 0 else "Not started")
        return {"text": text, "days": days, "type": "start_after_use"}
    try:
        now_ms = int(time.time() * 1000)
        delta_ms = int(timestamp) - now_ms

        if delta_ms <= 0:
            past_ms = abs(delta_ms)
            days_ago = past_ms // 86400000
            hours_ago = (past_ms % 86400000) // 3600000
            return {"text": _fmt_expired(int(days_ago), int(hours_ago)), "days": -int(days_ago), "type": "expired"}

        days = delta_ms // 86400000
        hours = (delta_ms % 86400000) // 3600000
        minutes = (delta_ms % 3600000) // 60000

        text = _fmt_future(int(days), int(hours), int(minutes))

        if days == 0:
            return {"text": text, "days": 0, "type": "today"}
        if days < 7:
            return {"text": text, "days": int(days), "type": "soon"}
        return {"text": text, "days": int(days), "type": "normal"}
    except:
        return {"text": ("تاریخ نامعتبر" if is_fa else "Invalid Date"), "days": 0, "type": "error"}


def get_accessible_servers(user, include_disabled=False):
    if not user:
        return []
    query = Server.query
    if not include_disabled:
        query = query.filter_by(enabled=True)
    if user.role == 'reseller':
        allowed_map, assignments = get_reseller_access_maps(user)
        if allowed_map == '*':
            return query.all()

        server_ids = set(allowed_map.keys()) | set(assignments.keys())
        if not server_ids:
            return []
        return query.filter(Server.id.in_(server_ids)).all()
    return query.all()

# --- 3X-UI PANEL ADAPTER --- (extracted to panel.adapters.xui; re-exported here for compatibility)
from panel.adapters.xui import (  # noqa: F401
    XUI_COOKIE_SESSION_CACHE,
    _add_client_to_inbound,
    _autoupgrade_http_to_https,
    _fetch_csrf_token,
    _format_panel_connection_error,
    _normalize_server_status_payload,
    _pick_first_value,
    _probe_v3_client_api,
    _push_full_inbound,
    _reconcile_client_inbounds,
    _remember_v3_capability,
    _remove_client_from_inbound,
    _rename_client_email_local,
    _safe_response_json,
    _sync_membership_ownership,
    _v3_client_payload,
    _v3_fix_spaced_email,
    _v3_get,
    _v3_get_client,
    _v3_post,
    _v3_rename_email_via_inbounds,
    _v3_sanitize_email,
    extract_base_and_webpath,
    fetch_direct_link_from_subscription,
    fetch_inbounds,
    fetch_onlines,
    fetch_server_status,
    get_server_api_token,
    get_xui_cookie_session,
    get_xui_session,
    persist_detected_panel_type,
    server_is_v3,
    v3_add_client,
    v3_attach_client,
    v3_delete_client,
    v3_detach_client,
    v3_enable_client,
    v3_reset_client,
    v3_update_client,
)

from panel.services.subscription import (  # noqa: F401
    build_public_subscription_url,
    build_subscription_configs,
    find_client,
    generate_client_link,
    get_public_base_url,
)

def process_inbounds(inbounds, server, user, allowed_map='*', assignments=None, app_base_url=None, online_index=None):
    processed = []
    stats = {"total_inbounds": 0, "active_inbounds": 0, "total_clients": 0, "online_clients": 0, "active_clients": 0, "inactive_clients": 0, "not_started_clients": 0, "unlimited_expiry_clients": 0, "unlimited_volume_clients": 0, "upload_raw": 0, "download_raw": 0, "remaining_raw": 0}
    dashboard_thresholds = _get_dashboard_status_thresholds()
    panel_lang = _get_panel_ui_lang()
    
    assignments = assignments or {}
    online_index = online_index or {"pairs": set(), "emails": set()}
    online_pairs = online_index.get('pairs') if isinstance(online_index, dict) else set()
    online_emails = online_index.get('emails') if isinstance(online_index, dict) else set()

    owned_emails = set()
    if user.role == 'reseller':
        ownerships = ClientOwnership.query.filter_by(reseller_id=user.id, server_id=server.id).all()
        owned_emails = {o.client_email.lower() for o in ownerships if o.client_email}

    # ── Hoist server-level values out of the per-client loop (computed once) ──
    _parsed_host = urlparse(server.host)
    _hostname = _parsed_host.hostname
    _scheme = _parsed_host.scheme
    _final_port = server.sub_port if server.sub_port else _parsed_host.port
    _port_str = f":{_final_port}" if _final_port else ""
    _base_sub = f"{_scheme}://{_hostname}{_port_str}"
    _s_path = (server.sub_path or '').strip('/')
    _j_path = (server.json_path or '').strip('/')
    if app_base_url:
        _app_base = get_public_base_url(app_base_url)
    else:
        try:
            _app_base = get_public_base_url(request.url_root)
        except RuntimeError:
            _app_base = get_public_base_url()  # background worker
    _is_sanaei = (server.panel_type == 'sanaei')
    _server_id = server.id
    # On v3 the same client (by email) is mirrored across several inbounds.
    # Count each person ONCE for all aggregate stats so totals aren't inflated.
    _is_v3 = server_is_v3(server)
    _v3_seen_emails = set()

    for inbound in inbounds:
        try:
            inbound_id_raw = inbound.get('id')
            try:
                inbound_id = int(inbound_id_raw)
            except (TypeError, ValueError):
                inbound_id = inbound_id_raw

            if user.role == 'reseller':
                accessible = is_inbound_accessible(server.id, inbound_id, allowed_map, assignments)
                if not accessible:
                    continue

            settings = _json_field(inbound.get('settings'), {})
            clients = settings.get('clients', [])
            client_stats = inbound.get('clientStats', [])

            # Build an email -> stats lookup ONCE per inbound (was O(clients*stats))
            stats_by_email = {}
            for _st in client_stats:
                _e = _st.get('email')
                if _e is not None and _e not in stats_by_email:
                    stats_by_email[_e] = _st

            processed_clients = []
            seen_client_keys = set()
            for client in clients:
                email = client.get('email', '')
                email_l = (str(email or '').strip().lower())
                client_uuid = (client.get('id') or '').strip().lower()
                dedup_key = (email_l, client_uuid)
                if dedup_key != ('', '') and dedup_key in seen_client_keys:
                    continue
                seen_client_keys.add(dedup_key)

                if user.role == 'reseller' and email.lower() not in owned_emails:
                    continue

                # v3: only the first inbound where this email appears feeds the stats.
                if _is_v3 and email_l:
                    _count_stat = email_l not in _v3_seen_emails
                    if _count_stat:
                        _v3_seen_emails.add(email_l)
                else:
                    _count_stat = True
                
                sub_id = client.get('subId', '')
                sub_url = ""
                json_url = ""
                dash_sub_url = ""

                if sub_id or (_is_sanaei and client.get('id')):
                    final_id = sub_id if sub_id else client.get('id')
                    sub_url = f"{_base_sub}/{_s_path}/{final_id}"
                    json_url = f"{_base_sub}/{_j_path}/{final_id}"
                    dash_sub_url = build_public_subscription_url(
                        _server_id, final_id, _app_base,
                    )

                _stat = stats_by_email.get(email)
                client_up = _stat.get('up', 0) if _stat else 0
                client_down = _stat.get('down', 0) if _stat else 0

                total_bytes = client.get('totalGB', 0) or 0
                remaining_bytes = max(total_bytes - (client_up + client_down), 0) if total_bytes > 0 else None
                total_formatted = format_bytes_gb_tb(total_bytes) if total_bytes > 0 else "Unlimited"

                if _count_stat and total_bytes <= 0:
                    stats["unlimited_volume_clients"] += 1
                
                volume_status = ""
                if remaining_bytes is not None:
                    remaining_formatted = format_bytes_gb_tb(remaining_bytes)
                    if remaining_bytes <= 0:
                        remaining_formatted = "Suspended"
                        volume_status = "suspended"
                    elif remaining_bytes < int(float(dashboard_thresholds.get('low_volume_gb', 1.0)) * (1024 ** 3)):
                        remaining_formatted = f"{remaining_formatted} Low"
                        volume_status = "low"
                else:
                    remaining_formatted = "Unlimited"
                    # Use existing purple badge style (expiry-start-after) for unlimited volume
                    volume_status = "expiry-start-after"

                expiry_raw = client.get('expiryTime', 0)
                expiry_info = format_remaining_days(expiry_raw, lang=panel_lang)
                account_state = _compute_client_service_state(
                    enabled=bool(client.get('enable', True)),
                    total_bytes=int(total_bytes or 0),
                    remaining_bytes=(None if remaining_bytes is None else int(remaining_bytes)),
                    expiry_ts=int(expiry_raw or 0),
                    expiry_info=expiry_info,
                    thresholds=dashboard_thresholds,
                    lang=panel_lang,
                )

                if expiry_info.get('type') == 'start_after_use':
                    stats["not_started_clients"] += 1

                if expiry_info.get('type') == 'unlimited':
                    stats["unlimited_expiry_clients"] += 1

                # Online status (best-effort; depends on panel API support)
                inbound_id_norm = None
                try:
                    inbound_id_norm = int(inbound.get('id'))
                except Exception:
                    inbound_id_norm = str(inbound.get('id'))
                is_online = False
                try:
                    if email_l:
                        is_online = ((inbound_id_norm, email_l) in (online_pairs or set())) or (email_l in (online_emails or set()))
                except Exception:
                    is_online = False

                client_data = {
                    "email": email,
                    "comment": (client.get('comment') or '').strip(),
                    "id": client.get('id', ''),
                    "subId": sub_id,
                    "enable": client.get('enable', True),
                    "is_online": bool(is_online),
                    "totalGB": total_bytes,
                    "totalGB_formatted": total_formatted,
                    "remaining_bytes": remaining_bytes if remaining_bytes is not None else -1,
                    "remaining_formatted": remaining_formatted,
                    "volume_status": volume_status,
                    "service_state": account_state.get('key', 'active'),
                    "service_state_label": account_state.get('label', 'فعاله' if panel_lang == 'fa' else 'Active'),
                    "service_state_emoji": account_state.get('emoji', '✅'),
                    "service_state_tag": account_state.get('tag', 'ok'),
                    "expiryTime": expiry_info['text'],
                    "expiryTimestamp": expiry_raw,
                    "expiryType": expiry_info['type'],
                    "up": client_up,
                    "down": client_down,
                    "up_formatted": format_bytes(client_up),
                    "down_formatted": format_bytes(client_down),
                    "sub_url": sub_url,
                    "json_url": json_url,
                    "dash_sub_url": dash_sub_url,
                    "server_id": server.id,
                    "inbound_id": inbound.get('id'),
                    "link": sub_url,  # Use subscription URL - client apps will fetch correct configs from panel
                    "raw_client": client  # Store original object for faster updates
                }
                processed_clients.append(client_data)

                if _count_stat:
                    stats["total_clients"] += 1
                    if is_online:
                        stats["online_clients"] += 1
                    if client.get('enable', True): stats["active_clients"] += 1
                    else: stats["inactive_clients"] += 1
                    stats["upload_raw"] += client_up
                    stats["download_raw"] += client_down
                    # Accumulate remaining for active limited-volume clients only
                    if client.get('enable', True) and remaining_bytes is not None and remaining_bytes >= 0:
                        stats["remaining_raw"] += int(remaining_bytes)
            
            # استخراج network و security از settings
            streamSettings = settings.get('streamSettings', {})
            network = streamSettings.get('network', 'tcp')
            security = streamSettings.get('security', 'none')
            
            # Remaining = sum of active+usable clients only:
            # enabled, not expired, not disabled — active / expiring_soon / volume_low
            _ACTIVE_STATES = {'active', 'expiring_soon', 'volume_low'}
            _inbound_remaining_raw = sum(
                c['remaining_bytes'] for c in processed_clients
                if c.get('enable', True)
                and c.get('service_state', 'active') in _ACTIVE_STATES
                and c.get('remaining_bytes', -1) >= 0
            )
            _active_count = sum(1 for _c in processed_clients if _c.get('enable', True))
            processed.append({
                "id": inbound.get('id'),
                "remark": inbound.get('remark', ''),
                "port": inbound.get('port', ''),
                "protocol": inbound.get('protocol', ''),
                "network": network,
                "security": security,
                "clients": processed_clients,
                "client_count": len(processed_clients),
                "active_count": _active_count,
                "enable": inbound.get('enable', False),
                "server_id": server.id,
                "server_name": server.name,
                "total_up": format_bytes(inbound.get('up', 0)),
                "total_down": format_bytes(inbound.get('down', 0)),
                "up_raw": inbound.get('up', 0),
                "down_raw": inbound.get('down', 0),
                "remaining_total_raw": _inbound_remaining_raw,
                "remaining_total": format_bytes(_inbound_remaining_raw) if _inbound_remaining_raw > 0 else None,
            })
            
            # total_clients is now counted per-client above (v3-deduplicated).
            if inbound.get('enable', False): stats["active_inbounds"] += 1
            
        except Exception as e:
            continue
            
    stats["total_inbounds"] = len(processed)
    stats["total_upload"] = format_bytes(stats["upload_raw"])
    stats["total_download"] = format_bytes(stats["download_raw"])
    stats["total_traffic"] = format_bytes(stats["upload_raw"] + stats["download_raw"])
    stats["total_remaining"] = format_bytes(stats["remaining_raw"])
    stats["limited_clients"] = stats["total_clients"] - stats["unlimited_volume_clients"]

    return processed, stats

# --- ROUTES ---

# Extracted blueprint route groups (panel/routes/). Registered here, ahead of
# the remaining in-file routes; endpoint names are blueprint-prefixed.
from panel.routes.admin import bp as admin_bp
from panel.routes.auth import bp as auth_bp
from panel.routes.clients import (  # noqa: F401
    add_client, renew_client, rotate_client,
)
from panel.routes.clients import bp as clients_bp
from panel.routes.bank_cards import bp as bank_cards_bp
from panel.routes.custom_subs import bp as custom_subs_bp
from panel.routes.dashboard import bp as dashboard_bp
from panel.routes.finance import bp as finance_bp
from panel.routes.merger import bp as merger_bp
from panel.routes.monitor import bp as monitor_bp
from panel.routes.packages import _calculate_minimum_price  # noqa: F401
from panel.routes.packages import bp as packages_bp
from panel.routes.pages import bp as pages_bp
from panel.routes.pulse import PULSE_COPY, _pulse_queue_snapshot  # noqa: F401
from panel.routes.pulse import bp as pulse_bp
from panel.routes.receipts import bp as receipts_bp
from panel.routes.royalty import bp as royalty_bp
from panel.routes.subscription_pages import bp as subscription_pages_bp
from panel.routes.system import bp as system_bp
from panel.routes.telegram import bp as telegram_bp
from panel.routes.usage import bp as usage_bp
from panel.routes.backups import bp as backups_bp
from panel.routes.bnqo import bp as bnqo_bp
from panel.routes.content import bp as content_bp
from panel.routes.files import bp as files_bp
from panel.routes.messaging import bp as messaging_bp
from panel.routes.settings import bp as settings_bp
from panel.routes.templates_api import bp as templates_api_bp

app.register_blueprint(auth_bp)
app.register_blueprint(pages_bp)
app.register_blueprint(system_bp)
app.register_blueprint(pulse_bp)
app.register_blueprint(bnqo_bp)
app.register_blueprint(royalty_bp)
app.register_blueprint(merger_bp)
app.register_blueprint(monitor_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(usage_bp)
app.register_blueprint(clients_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(finance_bp)
app.register_blueprint(packages_bp)
app.register_blueprint(receipts_bp)
app.register_blueprint(bank_cards_bp)
app.register_blueprint(custom_subs_bp)
app.register_blueprint(subscription_pages_bp)
app.register_blueprint(telegram_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(content_bp)
app.register_blueprint(files_bp)
app.register_blueprint(messaging_bp)
app.register_blueprint(templates_api_bp)
app.register_blueprint(backups_bp)







def _pulse_enqueue_targets(targets, profile='quick', vantage='local', sites=None,
                           triggered_by='web', template_name=None,
                           download_bytes=10_000_000, upload_bytes=2_000_000):
    run_ids = []
    batch_id = secrets.token_hex(6)
    for index, target in enumerate(targets, 1):
        run = PulseRun(
            server_id=target['server_id'],
            server_name=target.get('server_name'),
            scope='config',
            inbound_label=target.get('inbound_label'),
            profile=profile if profile in ('quick', 'full') else 'quick',
            vantage=vantage or 'local',
            status='queued',
            triggered_by=triggered_by,
            params_json=json.dumps({
                'inbound_id': target['inbound_id'],
                'inbound_ids': (target.get('inbound_ids') or
                                ([] if target.get('config_source') == 'manual'
                                 else [target['inbound_id']])),
                'config_ids': target['config_ids'],
                'config_source': target.get('config_source') or 'panel',
                'manual_configs': target.get('manual_configs') or [],
                'v3_mode': bool(target.get('v3_mode')),
                'limit': len(target.get('manual_configs') or target['config_ids']),
                'sites': sites or [],
                'download_bytes': download_bytes,
                'upload_bytes': upload_bytes,
                'batch_id': batch_id,
                'batch_index': index,
                'batch_total': len(targets),
                'template_name': template_name,
            }, ensure_ascii=False),
        )
        db.session.add(run)
        db.session.flush()
        run_ids.append(run.id)
    db.session.commit()
    return run_ids






class RoyaltyBaselineError(Exception):
    """Raised when the usage-history baseline query can't be computed."""


def _royalty_parse_filters(args):
    days = _parse_int(args.get('days'), 1, min_value=1, max_value=365)
    try:
        server_filter = int(args.get('server_id')) if args.get('server_id') not in (None, '', 'all') else None
    except Exception:
        server_filter = None
    try:
        reseller_filter = int(args.get('reseller_id')) if args.get('reseller_id') not in (None, '', 'all') else None
    except Exception:
        reseller_filter = None
    return days, server_filter, reseller_filter


def _compute_royalty_idle(admin_id, days, server_filter, reseller_filter):
    """Return the list of idle (no-traffic-in-window) active clients.

    Must run inside an app context. Raises RoyaltyBaselineError if the baseline
    query fails. Logs timing so a slow scan can be diagnosed from the server log.
    """
    t0 = time.perf_counter()
    user = db.session.get(Admin, admin_id)
    is_reseller = bool(user and user.role == 'reseller')
    window_start = datetime.utcnow() - timedelta(days=days)

    # One compact daily row contains the account counters at day start, so the
    # idle scan stays constant-cost regardless of the requested history window.
    baseline = {}  # (server_id, sub_id) -> total_bytes
    start_date = _usage_tehran_date(window_start)
    try:
        q = UsageDaily.query.filter(UsageDaily.usage_date == start_date)
        if server_filter is not None:
            q = q.filter(UsageDaily.server_id == server_filter)
        rows = q.all()
        for r in rows:
            baseline[(int(r.server_id), str(r.sub_id))] = (
                int(r.opening_upload_bytes or 0) + int(r.opening_download_bytes or 0)
            )
    except Exception as exc:
        app.logger.warning("royalty baseline query failed: %s", exc)
        raise RoyaltyBaselineError(str(exc))
    t_base = time.perf_counter()

    # 2) Ownership maps (for filtering + labeling), incl. server-independent UUID.
    email_map, uuid_map = _get_ownership_maps()
    uuid_global = _get_ownership_uuid_global()

    # 3) Walk current cached clients and pick the idle ones.
    idle = []
    seen_clients = set()  # one card per user per server (v3 assigns one user to many inbounds)
    snapshot = GLOBAL_SERVER_DATA.get('inbounds') or []
    canonical_totals = {
        key: int(point['total_bytes'] or 0)
        for key, point in _usage_account_points(snapshot).items()
    }
    for inbound in snapshot:
        try:
            sid = int(inbound.get('server_id'))
        except Exception:
            continue
        if server_filter is not None and sid != server_filter:
            continue
        server_name = inbound.get('server_name') or ''
        for c in (inbound.get('clients') or []):
            if not c.get('enable', True):
                continue
            sub_id = str(c.get('subId') or '').strip()
            if not sub_id:
                continue
            base = baseline.get((sid, sub_id))
            if base is None:
                continue  # no history at window start → can't classify
            current_total = canonical_totals.get((sid, sub_id),
                                                  int(c.get('up', 0) or 0) + int(c.get('down', 0) or 0))
            if current_total != base:
                continue  # had traffic → not idle

            # Ownership / reseller resolution (UUID first, then email, then global UUID)
            uu = str(c.get('id') or '').strip().lower()
            em = (c.get('email') or '').strip().lower()
            owner = (uuid_map.get((sid, uu)) if uu else None) \
                or (email_map.get((sid, em)) if em else None) \
                or (uuid_global.get(uu) if uu else None)
            owner_id = owner.get('id') if owner else None
            owner_username = owner.get('username') if owner else None

            if is_reseller:
                if owner_id != user.id:
                    continue
            elif reseller_filter is not None:
                if reseller_filter == 0:
                    if owner_id is not None:
                        continue  # only unassigned/system
                elif owner_id != reseller_filter:
                    continue

            # One card per user per server: same email = same user regardless of
            # inbound count or UUID (v3 assigns one user to multiple inbounds).
            if em:
                dedupe_key = ('e', sid, em)
            elif uu:
                dedupe_key = ('u', sid, uu)
            else:
                dedupe_key = None
            if dedupe_key is not None:
                if dedupe_key in seen_clients:
                    continue
                seen_clients.add(dedupe_key)

            idle.append({
                'email': c.get('email') or '',
                'comment': c.get('comment') or '',
                'server_id': sid,
                'server_name': server_name,
                'inbound_id': inbound.get('id'),
                'client_uuid': str(c.get('id') or ''),
                'sub_id': str(c.get('subId') or c.get('id') or ''),
                'sub_url': c.get('sub_url') or '',
                'dash_sub_url': c.get('dash_sub_url') or '',
                'expiryTime': c.get('expiryTime') or '',
                'remaining_formatted': c.get('remaining_formatted') or '',
                'total_used_formatted': format_bytes(current_total),
                'owner_username': owner_username,
                'is_online': bool(c.get('is_online')),
            })

    idle.sort(key=lambda x: (x.get('server_name') or '', (x.get('email') or '').lower()))
    try:
        app.logger.info(
            "royalty idle: days=%s server=%s reseller=%s baseline_rows=%d "
            "baseline_ms=%.0f total_ms=%.0f idle=%d",
            days, server_filter, reseller_filter, len(baseline),
            (t_base - t0) * 1000.0, (time.perf_counter() - t0) * 1000.0, len(idle),
        )
    except Exception:
        pass
    return idle






def _merger_user_is_allowed():
    return bool(session.get('is_superadmin') or session.get('role') == 'superadmin')




def _ss_key_len(method: str) -> int:
    m = (method or '').lower()
    if '128' in m:
        return 16
    return 32  # aes-256-gcm, chacha20-ietf-poly1305, 2022-256 variants


def _ss_password(method: str) -> str:
    return base64.b64encode(os.urandom(_ss_key_len(method))).decode('ascii')













def fetch_worker(server_dict):
    with app.app_context():
        # Convert dict to object for compatibility with existing functions
        server_obj = SimpleNamespace(**server_dict)
        session_obj, error = get_xui_session(server_obj)
        if error:
            return server_dict['id'], None, None, None, None, error, 'auto'
        
        inbounds, fetch_error, detected_type = fetch_inbounds(session_obj, server_obj.host, server_obj.panel_type)
        online_index, _ = fetch_onlines(session_obj, server_obj.host, server_obj.panel_type)
        status_payload, status_error, _status_type = fetch_server_status(session_obj, server_obj.host, server_obj.panel_type)

        # Enrich status_payload with online_count from the onlines endpoint
        # (the /status API does NOT return online_count; it comes from /onlines)
        if online_index:
            online_count = len(online_index.get('pairs', set())) + len(online_index.get('emails', set()))
            if status_payload is None:
                status_payload = {}
            if status_payload.get('online_count') is None and online_count > 0:
                status_payload['online_count'] = online_count

        return server_dict['id'], inbounds, online_index, status_payload, status_error, fetch_error, detected_type


def enrich_inbounds_with_ownership(inbounds):
    """Attach owner fields to inbound clients using the in-memory ownership cache.

    Old approach: built email/uuid sets → huge IN-clause DB query → second iteration.
    New approach: read from _OWNERSHIP_CACHE (rebuilt at most every 30 s); each
    client lookup is O(1) dict access.  No per-request DB query.
    """
    try:
        if not isinstance(inbounds, list) or not inbounds:
            return inbounds

        email_map, uuid_map = _get_ownership_maps()
        uuid_global = _get_ownership_uuid_global()
        if not email_map and not uuid_map and not uuid_global:
            return inbounds

        for inbound in inbounds:
            try:
                sid = int(inbound.get('server_id'))
            except Exception:
                continue
            for client in (inbound.get('clients') or []):
                uu = str(client.get('id') or '').strip().lower()
                em = (client.get('email') or '').strip().lower()
                info = uuid_map.get((sid, uu)) if uu else None
                if not info and em:
                    info = email_map.get((sid, em))
                # Final fallback: match by UUID alone, so ownership survives a
                # server re-add (new server_id) or inbound rebuild (new inbound_id).
                if not info and uu:
                    info = uuid_global.get(uu)
                if info and info.get('username'):
                    client['owner_reseller_id'] = info.get('id')
                    client['owner_username']    = info.get('username')
                else:
                    client.pop('owner_reseller_id', None)
                    client.pop('owner_username',    None)

    except Exception:
        app.logger.exception("Failed to enrich inbounds with ownership")
    return inbounds


def _ensure_snapshot_enriched():
    """Enrich the shared GLOBAL_SERVER_DATA snapshot with ownership in place, but
    only once per (snapshot, ownership) version. Enrichment is idempotent and
    additive (it just sets/clears owner_* fields on client dicts), so the read
    path can serve the shared lists directly — avoiding a full deepcopy of the
    snapshot on every /api/refresh, which was the dominant cost at scale."""
    try:
        key = (GLOBAL_SERVER_DATA.get('last_update'), _OWNERSHIP_CACHE.get('updated_at'))
        if GLOBAL_SERVER_DATA.get('_enriched_key') == key:
            return
        enrich_inbounds_with_ownership(GLOBAL_SERVER_DATA.get('inbounds') or [])
        GLOBAL_SERVER_DATA['_enriched_key'] = key
    except Exception:
        app.logger.exception("Failed to ensure snapshot enrichment")














def _get_cached_raw_client(server_id: int, inbound_id: int, email: str):
    target_client = None
    cached_inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    for ib in cached_inbounds:
        try:
            if int(ib.get('server_id', -1)) == int(server_id) and int(ib.get('id', -1)) == int(inbound_id):
                for c in ib.get('clients', []):
                    if c.get('email') == email and 'raw_client' in c:
                        target_client = copy.deepcopy(c['raw_client'])
                        break
        except (ValueError, TypeError):
            continue
        if target_client:
            break
    return target_client


def _reseller_can_create_free(user) -> bool:
    """Free creation/renew is allowed for admins/superadmins, and only for
    resellers explicitly granted the permission in their user settings."""
    if getattr(user, 'role', None) != 'reseller':
        return True
    return bool(getattr(user, 'allow_free_creation', False))


def _user_can_afford(user, price: int) -> tuple[bool, str | None]:
    """Check if user can afford price, respecting negative credit allowance.
    Returns (ok, error_message_or_None).
    """
    if price <= 0:
        return True, None
    cur = getattr(user, 'credit', 0) or 0
    allow_neg = getattr(user, 'allow_negative_credit', False) or False
    neg_limit = getattr(user, 'negative_credit_limit', 0) or 0
    min_bal = -(abs(neg_limit)) if allow_neg else 0
    if cur - price < min_bal:
        shortfall = (cur - price) - min_bal
        if _get_panel_ui_lang() == 'fa':
            return False, (
                f"موجودی کافی نیست — اعتبار فعلی: {cur:,} T، "
                f"هزینه: {price:,} T، کسری: {abs(shortfall):,} T"
            )
        return False, (
            f"Insufficient credit — current balance: {cur:,} T, "
            f"cost: {price:,} T, shortfall: {abs(shortfall):,} T"
        )
    return True, None


def _has_client_access(user, server_id: int, email: str, inbound_id: int | None = None, client_uuid: str | None = None) -> bool:
    if not user:
        return False
    if user.role != 'reseller':
        return True

    email_l = (email or '').strip().lower()
    cu = (client_uuid or '').strip()
    if not cu and inbound_id is not None:
        try:
            raw = _get_cached_raw_client(int(server_id), int(inbound_id), email)
            cu = (raw.get('id') or '').strip() if isinstance(raw, dict) else ''
        except Exception:
            cu = ''

    q = ClientOwnership.query.filter(
        ClientOwnership.reseller_id == user.id,
        ClientOwnership.server_id == server_id,
    )
    key_filters = []
    if cu:
        key_filters.append(ClientOwnership.client_uuid == cu)
    if email_l:
        key_filters.append(func.lower(ClientOwnership.client_email) == email_l)
    if key_filters:
        q = q.filter(or_(*key_filters))
    return bool(q.first())


def _toggle_client_core(user, server, inbound_id: int, email: str, enable: bool):
    """Core implementation for toggling a client; returns (ok, error, status_code)."""
    price = 0
    description = f"Toggle client {email} to {enable}"

    if not _has_client_access(user, server.id, email, inbound_id=inbound_id):
        return False, "Access denied", 403

    if user.role == 'reseller':
        ok, err = _user_can_afford(user, price)
        if not ok:
            return False, err, 402

    target_client = _get_cached_raw_client(server.id, inbound_id, email)

    session_obj, error = get_xui_session(server)
    if error:
        return False, error, 400

    try:
        if not target_client:
            inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
            if fetch_err:
                return False, fetch_err, 400

            persist_detected_panel_type(server, detected_type)
            target_client, _ = find_client(inbounds, inbound_id, email)
            if not target_client:
                return False, "Client not found", 404

        target_client['enable'] = bool(enable)
        # Manual enable/disable doubles as an SMS/WhatsApp opt-out switch: disabling
        # tags the comment with #nosms #nopm, enabling removes them. x-ui's own
        # auto-disable on expiry does NOT pass through here, so an expired account
        # is never auto-opted-out by this.
        try:
            target_client['comment'] = _toggle_optout_tags(target_client.get('comment'), add=not enable)
        except Exception:
            pass
        client_identifier = target_client.get('id') or target_client.get('password') or target_client.get('email')

        # v3: enable/disable via the first-class client update (legacy updateClient is 404).
        if server_is_v3(server):
            ok, _vr, verr = v3_update_client(server, session_obj, email, target_client)
            if ok:
                patch_cached_client(server.id, email, enable=bool(enable), comment=target_client.get('comment'))
                if not enable:
                    _cancel_pending_sms_for_account(server.id, email, reason='client_disabled')
                return True, None, 200
            return False, f"v3 toggle failed: {verr}", 502

        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [target_client]})
        }

        replacements = {
            'id': inbound_id,
            'inbound_id': inbound_id,
            'inboundId': inbound_id,
            'clientId': client_identifier,
            'client_id': client_identifier,
            'email': email
        }

        templates = collect_endpoint_templates(server.panel_type, 'client_update', CLIENT_UPDATE_FALLBACKS)
        errors = []
        for template in templates:
            full_url = build_panel_url(server.host, template, replacements)
            if not full_url:
                continue
            try:
                resp = session_obj.post(full_url, json=payload, verify=False, timeout=10)
            except Exception as exc:
                errors.append(f"{template}: {exc}")
                continue

            if resp.status_code == 200:
                try:
                    resp_json = resp.json()
                    if isinstance(resp_json, dict) and resp_json.get('success') is False:
                        errors.append(f"{template}: success false")
                        continue
                except ValueError:
                    pass
                if user.role == 'reseller' and price > 0:
                    user.credit -= price
                    log_transaction(user.id, -price, 'renew', description or f"Renew client {email}", server_id=server.id)
                    db.session.commit()
                patch_cached_client(server.id, email, enable=bool(enable), comment=target_client.get('comment'))
                if not enable:
                    _cancel_pending_sms_for_account(server.id, email, reason='client_disabled')
                return True, None, 200

            errors.append(f"{template}: {resp.status_code}")
            if resp.status_code != 404:
                break

        app.logger.warning("Toggle failed for %s: %s", email, '; '.join(errors))
        return False, "Client update endpoint returned error", 400
    except Exception as exc:
        app.logger.error("Toggle error: %s", exc)
        return False, str(exc), 400


def _delete_client_core(user, server, inbound_id: int, email: str):
    """Core implementation for deleting a client; returns (ok, error, status_code)."""
    if not _has_client_access(user, server.id, email, inbound_id=inbound_id):
        return False, "Access denied", 403

    target_client = _get_cached_raw_client(server.id, inbound_id, email)

    session_obj, error = get_xui_session(server)
    if error:
        return False, error, 400

    _delete_inbound_row = None
    try:
        if not target_client:
            inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
            if fetch_err:
                return False, "Failed to fetch inbounds", 400

            persist_detected_panel_type(server, detected_type)
            target_client, _delete_inbound_row = find_client(inbounds, inbound_id, email)
            if not target_client:
                return False, "Client not found", 404

        client_id = target_client.get('id', target_client.get('password', email))

        # v3: delete the first-class client by email (legacy delClient is 404).
        if server_is_v3(server):
            ok, _vr, verr = v3_delete_client(server, session_obj, email)
            if not ok:
                return False, f"v3 delete failed: {verr}", 502
            email_l = (email or '').strip().lower()
            cu = str(client_id) if client_id else ''
            q = ClientOwnership.query.filter(ClientOwnership.server_id == server.id)
            kf = []
            if cu:
                kf.append(ClientOwnership.client_uuid == cu)
            if email_l:
                kf.append(func.lower(ClientOwnership.client_email) == email_l)
            if kf:
                q.filter(or_(*kf)).delete(synchronize_session=False)
            db.session.commit()
            invalidate_ownership_cache()
            try:
                log_transaction(user.id, 0, 'delete_client', f"Deleted client {email}", server_id=server.id, client_email=email)
            except Exception:
                pass
            remove_cached_client(server.id, email, client_uuid=str(client_id) if client_id else None)
            return True, None, 200

        # Shadowsocks clients have no UUID — delClient/:clientId won't work.
        # Remove the client from the full inbound settings and push.
        if 'id' not in target_client:
            _full_ib_del = _delete_inbound_row
            if _full_ib_del is None:
                _ibs_del, _fe_del, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                if not _fe_del:
                    for _ib_del in (_ibs_del or []):
                        if _ib_del.get('id') == inbound_id:
                            _full_ib_del = _ib_del
                            break
            if _full_ib_del is None:
                return False, "shadowsocks: could not fetch full inbound for delete", 400
            _fs_del = _json_field(_full_ib_del.get('settings'), {})
            _fs_del['clients'] = [c for c in _fs_del.get('clients', []) if c.get('email') != email]
            _ok_del, _err_del = _push_full_inbound(server, session_obj, _full_ib_del, _fs_del)
            if not _ok_del:
                detail = _err_del or 'shadowsocks inbound delete failed'
                app.logger.warning("Delete client failed for %s: %s", email, detail)
                return False, detail, 400
            success = True
        else:
            replacements = {
                'id': inbound_id,
                'inbound_id': inbound_id,
                'inboundId': inbound_id,
                'clientId': client_id,
                'client_id': client_id,
                'email': email
            }

            templates = collect_endpoint_templates(server.panel_type, 'client_delete', CLIENT_DELETE_FALLBACKS)
            errors = []
            success = False

            for template in templates:
                full_url = build_panel_url(server.host, template, replacements)
                if not full_url:
                    continue
                try:
                    resp = session_obj.post(full_url, verify=False, timeout=10)
                except Exception as exc:
                    errors.append(f"{template}: {exc}")
                    continue

                if resp.status_code == 200:
                    try:
                        resp_json = resp.json()
                        if isinstance(resp_json, dict) and resp_json.get('success') is False:
                            panel_msg = resp_json.get('msg') or resp_json.get('message') or 'success=false'
                            errors.append(f"{template}: {panel_msg}")
                            continue
                    except ValueError:
                        pass

                    success = True
                    break

                errors.append(f"{template}: HTTP {resp.status_code}")

            if not success:
                detail = '; '.join(errors) or 'no endpoint succeeded'
                app.logger.warning("Delete client failed for %s: %s", email, detail)
                return False, detail, 400

        if success:
            email_l = (email or '').strip().lower()
            ClientOwnership.query.filter(
                ClientOwnership.server_id == server.id,
                or_(
                    ClientOwnership.client_uuid == str(client_id),
                    func.lower(ClientOwnership.client_email) == email_l,
                )
            ).delete(synchronize_session=False)
            db.session.commit()
            invalidate_ownership_cache()

            try:
                log_transaction(user.id, 0, 'delete_client', f"Deleted client {email}", server_id=server.id, client_email=email)
            except Exception:
                pass

            remove_cached_client(server.id, email, client_uuid=str(client_id) if client_id else None,
                                 inbound_id=inbound_id)
            return True, None, 200

    except Exception as exc:
        app.logger.error("Delete client error: %s", exc)
        return False, str(exc), 400













































# ── App File Manager ──────────────────────────────────────────────────────────
# Separate from the general /api/upload — restricted to superadmin,
# larger size limits, strict whitelist, stored in static/app-files/.

_APP_FILES_DIR_NAME  = 'app-files'



def _app_files_dir() -> str:
    """Return (and create if needed) the app-files storage directory.
    Raises RuntimeError with a descriptive message on permission failure.
    """
    d = os.path.join(app.static_folder, _APP_FILES_DIR_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"Cannot create upload directory '{d}': {e}. "
            "Run: mkdir -p {d} && chown <user> {d} && chmod 755 {d}"
        ) from e
    if not os.access(d, os.W_OK):
        raise RuntimeError(
            f"Upload directory '{d}' exists but is not writable. "
            f"Run: chown $(whoami) '{d}' && chmod 755 '{d}'"
        )
    return d














# Startup: ensure app-files directory exists and is writable
with app.app_context():
    try:
        _app_files_dir()
        logger.info("app-files upload directory is ready")
    except RuntimeError as _appfiles_err:
        logger.warning("app-files directory not ready: %s", _appfiles_err)
        logger.warning("File uploads will fail. Use the 'Fix Setup' button in File Manager, or run:")
        logger.warning("  mkdir -p '%s' && chmod 755 <dir>", os.path.join(app.static_folder or '', _APP_FILES_DIR_NAME))


# ── SSL startup migration ─────────────────────────────────────────────────────
# When upgrading from a version that didn't copy certs to /etc/ssl/eve-manager/,
# we try to do the copy automatically so the export feature works immediately.
# This is silent — failure never blocks startup.
with app.app_context():
    try:
        _ssl_dest_cert = '/etc/ssl/eve-manager/fullchain.pem'
        _ssl_dest_key  = '/etc/ssl/eve-manager/privkey.pem'
        _need_copy = not (os.path.isfile(_ssl_dest_cert) and os.access(_ssl_dest_cert, os.R_OK)
                         and os.path.isfile(_ssl_dest_key) and os.access(_ssl_dest_key, os.R_OK))

        if _need_copy:
            # Try to find source paths (nginx config → letsencrypt glob)
            import re as _re, glob as _gl
            _src_cert = _src_key = ''

            # 1. Read nginx config
            for _nc in ['/etc/nginx/sites-available/eve-manager',
                        '/etc/nginx/sites-enabled/eve-manager',
                        '/etc/nginx/sites-available/eve-xui-manager']:
                if not os.path.isfile(_nc):
                    continue
                try:
                    with open(_nc, 'r', errors='ignore') as _nf:
                        _conf = _nf.read()
                    _cm = _re.search(r'ssl_certificate\s+([^;]+);', _conf)
                    _km = _re.search(r'ssl_certificate_key\s+([^;]+);', _conf)
                    if _cm and _km:
                        _src_cert = _cm.group(1).strip()
                        _src_key  = _km.group(1).strip()
                        break
                except Exception:
                    pass

            # 2. Fallback: letsencrypt glob
            if not _src_cert:
                for _lc in sorted(_gl.glob('/etc/letsencrypt/live/*/fullchain.pem')):
                    _src_cert = _lc
                    _src_key  = os.path.join(os.path.dirname(_lc), 'privkey.pem')
                    break

            if _src_cert and _src_key and os.path.isfile(_src_cert) and os.path.isfile(_src_key):
                _app_user = os.environ.get('APP_USER', 'evemgr')
                _copy_cmds = [
                    ['sudo', 'mkdir', '-p', '/etc/ssl/eve-manager'],
                    ['sudo', 'cp', '-f', _src_cert, _ssl_dest_cert],
                    ['sudo', 'cp', '-f', _src_key,  _ssl_dest_key],
                    ['sudo', 'chown', f'{_app_user}:{_app_user}', _ssl_dest_cert, _ssl_dest_key],
                    ['sudo', 'chmod', '644', _ssl_dest_cert],
                    ['sudo', 'chmod', '600', _ssl_dest_key],
                ]
                _copy_ok = True
                for _cmd in _copy_cmds:
                    _r = subprocess.run(_cmd, capture_output=True, timeout=10)
                    if _r.returncode != 0:
                        _copy_ok = False
                        break

                if _copy_ok:
                    # Update DB paths to the new readable location
                    for _k, _v in [('ssl_cert_path', _ssl_dest_cert), ('ssl_key_path', _ssl_dest_key)]:
                        _row = db.session.get(SystemSetting, _k) or SystemSetting(key=_k, value=_v)
                        _row.value = _v
                        db.session.merge(_row)
                    db.session.commit()
                    logger.info("SSL certs migrated to /etc/ssl/eve-manager/")
                else:
                    logger.warning("SSL cert migration failed (sudo not configured — use Settings → SSL → Sync)")
            else:
                logger.info("SSL not detected or not yet configured — skipping cert migration")
    except Exception as _ssl_migrate_err:
        logger.warning("SSL migration skipped: %s", _ssl_migrate_err)


































def _account_info_channel_links(admin: 'Admin') -> dict:
    """Return telegram_channel / whatsapp_channel for the logged-in admin,
    resolved by role exactly like the public subscription page:

    - reseller            → the reseller's own channel fields, falling back to
                            the global SystemConfig channels if unset.
    - superadmin / admin  → the global SystemConfig channels
                            ('channel_telegram' / 'channel_whatsapp'), falling
                            back to any value stored on the admin row.

    (Superadmin channels live in SystemConfig, NOT on the Admin row, which is
    why reading only admin.channel_* left {whatsapp_channel} blank for them.)
    - still not set       → empty string (template var stays blank, not shown).
    """
    def _cfg(key):
        row = db.session.get(SystemConfig, key)
        return ((row.value if row else '') or '').strip()

    own_tg = (getattr(admin, 'channel_telegram', '') or '').strip()
    own_wa = (getattr(admin, 'channel_whatsapp', '') or '').strip()
    glob_tg = _cfg('channel_telegram')
    glob_wa = _cfg('channel_whatsapp')

    is_reseller = (getattr(admin, 'role', None) == 'reseller')
    if is_reseller:
        return {
            'telegram_channel': own_tg or glob_tg,
            'whatsapp_channel': own_wa or glob_wa,
        }
    return {
        'telegram_channel': glob_tg or own_tg,
        'whatsapp_channel': glob_wa or own_wa,
    }



























def _cleanup_old_backups(days: int) -> dict:
    """Delete backup files older than `days` from BACKUP_DIR.
    Safety pre_restore_* files are kept. Returns {deleted, freed_bytes}."""
    deleted, freed = 0, 0
    if days < 1 or not os.path.isdir(BACKUP_DIR):
        return {'deleted': 0, 'freed_bytes': 0}
    cutoff = time.time() - days * 86400
    for pat in ('*.db', '*.dump', '*.sql', '*.zip'):
        for f in glob.glob(os.path.join(BACKUP_DIR, pat)):
            name = os.path.basename(f)
            if name.startswith('pre_restore_'):
                continue  # never auto-delete safety backups
            try:
                if os.path.getmtime(f) < cutoff:
                    sz = os.path.getsize(f)
                    os.remove(f)
                    deleted += 1
                    freed += sz
            except Exception:
                pass
    return {'deleted': deleted, 'freed_bytes': freed}




def _central_telegram_bot(create=False):
    bot = TelegramBotInstance.query.filter_by(scope_key='system').first()
    if bot is None and create:
        bot = TelegramBotInstance(scope_key='system', owner_type='system')
        db.session.add(bot)
        db.session.commit()
    return bot


def _telegram_bot_manageable_by(user, bot) -> bool:
    """Superadmins manage every bot; resellers only their own instance."""
    if not user or not bot:
        return False
    if user.role == 'superadmin' or user.is_superadmin:
        return True
    return user.role == 'reseller' and bot.owner_admin_id == user.id


def _log_audit(action, target=None, actor=None, meta=None) -> None:
    """Best-effort audit row. Never raises and never commits — the caller's
    transaction carries the row."""
    try:
        target_type = None
        target_id = None
        if isinstance(target, tuple):
            target_type, target_id = target
        elif target is not None:
            target_type = target.__class__.__name__
            target_id = getattr(target, 'id', None)
        actor_type = 'system'
        actor_admin_id = None
        if isinstance(actor, Admin):
            actor_type = 'admin'
            actor_admin_id = actor.id
        elif isinstance(actor, str) and actor in ('admin', 'system', 'customer'):
            actor_type = actor
        db.session.add(AuditLog(
            actor_type=actor_type,
            actor_admin_id=actor_admin_id,
            action=str(action)[:64],
            target_type=str(target_type)[:32] if target_type else None,
            target_id=str(target_id)[:64] if target_id not in (None, '') else None,
            meta_json=json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
        ))
    except Exception:
        pass


def _requested_telegram_bot():
    """Resolve the bot targeted by an optional bot_id with an ownership check.

    Without bot_id the central bot is targeted, preserving legacy behavior.
    Returns (bot, error_response); exactly one of the two is not None.
    """
    user = db.session.get(Admin, session['admin_id']) if 'admin_id' in session else None
    if not user:
        return None, (jsonify({'success': False, 'error': 'Unauthorized'}), 401)
    raw = request.args.get('bot_id')
    if raw in (None, '') and request.method != 'GET':
        raw = (request.get_json(silent=True) or {}).get('bot_id')
    if raw in (None, ''):
        bot = _central_telegram_bot(create=True)
    else:
        try:
            bot = db.session.get(TelegramBotInstance, int(raw))
        except (TypeError, ValueError):
            return None, (jsonify({'success': False, 'error': 'Invalid bot ID'}), 400)
        if bot is None:
            return None, (jsonify({'success': False, 'error': 'Telegram bot not found'}), 404)
    if not _telegram_bot_manageable_by(user, bot):
        return None, (jsonify({'success': False, 'error': 'Access Denied: not the bot owner'}), 403)
    if bot.archived_at is not None:
        return None, (jsonify({'success': False, 'error': 'This bot is archived'}), 409)
    return bot, None


def _telegram_bot_health(bot: TelegramBotInstance) -> dict:
    """Server-side per-bot health summary derived from the runtime row."""
    runtime = bot.runtime
    snapshot = runtime.to_safe_dict() if runtime else {}
    if bot.archived_at is not None:
        state = 'archived'
    elif not bot.enabled:
        state = 'disabled'
    elif runtime is None:
        state = 'stopped'
    else:
        state = str(snapshot.get('status') or 'stopped')
        if int(snapshot.get('failed_update_count') or 0) > 0 or (
                snapshot.get('last_error') and state != 'running'):
            state = 'error'
    return {
        'state': state,
        'last_heartbeat_at': snapshot.get('last_heartbeat_at'),
        'last_error': snapshot.get('last_error'),
        'failed_update_count': int(snapshot.get('failed_update_count') or 0),
    }


def _telegram_bot_token_conflict(bot, token=None):
    """Return the other instance already bound to this token, if any.

    The token's numeric prefix is the bot's Telegram user ID, so a conflict
    can be detected at save time without calling getMe.
    """
    bot_user_id = None
    if token:
        left = str(token).strip().partition(':')[0]
        if left.isdigit():
            bot_user_id = int(left)
    elif bot.bot_user_id:
        bot_user_id = int(bot.bot_user_id)
    if bot_user_id is None:
        return None
    query = TelegramBotInstance.query.filter(TelegramBotInstance.bot_user_id == bot_user_id)
    if bot.id is not None:
        query = query.filter(TelegramBotInstance.id != bot.id)
    return query.first()


TELEGRAM_PURCHASE_STRATEGIES = {'least_clients', 'priority', 'weighted_random', 'random'}
TELEGRAM_ACCOUNT_NAME_MODES = {'generated', 'customer'}
TELEGRAM_ACCOUNT_NAME_TOKENS = {
    'order_id', 'phone', 'phone_last4', 'telegram_username', 'random4',
}
TELEGRAM_INBOUND_ROUTE_MODES = {'manual', 'auto_detect'}
TELEGRAM_CUSTOMER_INBOUND_PROTOCOLS = {
    'vmess', 'vless', 'trojan', 'shadowsocks', 'wireguard', 'hysteria', 'mtproto',
}


def _telegram_purchase_policy(bot: TelegramBotInstance, create=False):
    policy = db.session.get(TelegramPurchasePolicy, bot.id)
    if policy is None and create:
        policy = TelegramPurchasePolicy(bot_instance_id=bot.id)
        db.session.add(policy)
        db.session.flush()
    return policy


def _validate_telegram_account_name_template(value) -> str:
    template = str(value or 'tg{order_id}-{phone_last4}').strip()
    if not 3 <= len(template) <= 120:
        raise ValueError('Account name template must be between 3 and 120 characters')
    if not re.fullmatch(r'[A-Za-z0-9_{}-]+', template):
        raise ValueError('Account name template may contain ASCII letters, numbers, underscore, dash, and tokens')
    tokens = set(re.findall(r'\{([A-Za-z0-9_]+)\}', template))
    if not tokens.issubset(TELEGRAM_ACCOUNT_NAME_TOKENS):
        unknown = ', '.join(sorted(tokens - TELEGRAM_ACCOUNT_NAME_TOKENS))
        raise ValueError(f'Unsupported account name template token: {unknown}')
    rendered = template.format(
        order_id='1842',
        phone='09195292411',
        phone_last4='2411',
        telegram_username='mahna',
        random4='a7f2',
    )
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{2,63}', rendered):
        raise ValueError('Rendered account names must be 3-64 safe ASCII characters')
    return template


def _telegram_purchase_servers_payload(bot: TelegramBotInstance):
    load_snapshot_from_redis()
    statuses = {}
    for status in GLOBAL_SERVER_DATA.get('servers_status') or []:
        try:
            statuses[int(status.get('server_id'))] = status
        except (TypeError, ValueError, AttributeError):
            continue
    rules = {
        row.server_id: row for row in TelegramPurchaseServerRule.query.filter_by(
            bot_instance_id=bot.id,
        ).all()
    }
    payload = []
    for server in Server.query.filter_by(enabled=True, hidden=False).order_by(
            Server.name.asc(), Server.id.asc()).all():
        rule = rules.get(server.id)
        status = statuses.get(server.id) or {}
        stats = status.get('stats') if isinstance(status.get('stats'), dict) else {}
        payload.append({
            'server_id': server.id,
            'server_name': server.name,
            'eligible': bool(rule.eligible) if rule else True,
            'customer_visible': bool(rule.customer_visible) if rule else False,
            'display_name': (rule.display_name if rule else None) or server.name,
            'priority': int(rule.priority if rule else 100),
            'weight': int(rule.weight if rule else 1),
            'active_clients': int(stats.get('active_clients', stats.get('total_clients', 0)) or 0),
            'healthy': (bool(status.get('success')) if status else None),
            'supports_v3_clients': bool(server_is_v3(server)),
        })
    return payload


def _telegram_customer_inbounds(server_id: int):
    """Return active client-capable inbounds from the freshest shared snapshot."""
    load_snapshot_from_redis()
    rows = []
    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        try:
            if int(inbound.get('server_id') or 0) != int(server_id):
                continue
            if not bool(inbound.get('enable', True)):
                continue
            protocol = str(inbound.get('protocol') or '').strip().lower()
            if protocol not in TELEGRAM_CUSTOMER_INBOUND_PROTOCOLS:
                continue
            rows.append({
                'id': int(inbound.get('id')),
                'remark': str(inbound.get('remark') or inbound.get('tag') or '').strip(),
                'protocol': protocol,
                'client_count': len(inbound.get('clients') or []),
            })
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(rows, key=lambda row: row['id'])


def _telegram_purchase_routes_payload(bot: TelegramBotInstance):
    routes = TelegramPurchaseInboundRoute.query.filter_by(bot_instance_id=bot.id).all()
    return [row.to_safe_dict() for row in sorted(
        routes, key=lambda row: (row.server_id, row.package_id, row.id),
    )]


def _telegram_purchase_packages_payload(bot: TelegramBotInstance = None):
    # Mirrors telegram_bot_worker._purchase_packages / _resolve_purchase_price;
    # app.py cannot import the worker (the worker already imports app).
    owner_id = int(bot.owner_admin_id) if bot and bot.owner_admin_id else None
    owner = db.session.get(Admin, owner_id) if owner_id else None
    packages = Package.query.filter_by(enabled=True).order_by(
        Package.display_order.asc(), Package.id.asc(),
    ).all()
    rows = []
    for package in packages:
        if getattr(package, 'is_trial', False):
            continue
        if not getattr(package, 'show_on_create', True):
            continue
        scope = str(package.scope or 'global').lower()
        if scope != 'global':
            try:
                assigned = {int(value) for value in json.loads(package.assigned_reseller_ids or '[]')}
            except (TypeError, ValueError):
                assigned = set()
            if not owner_id or (
                owner_id not in assigned and
                not (scope == 'personal' and int(package.created_by or 0) == owner_id)
            ):
                continue
        price = int(package.price or 0)
        if owner and str(getattr(owner, 'role', '') or '').lower() == 'reseller':
            price = int(calculate_reseller_price(owner, package=package) or 0)
        rows.append({
            'id': package.id,
            'name': package.name,
            'days': int(package.days or 0),
            'volume': int(package.volume or 0),
            'price': price,
        })
    return rows


def _v3_client_rows(payload):
    """Normalize the response shapes used by 3x-ui's v3 clients/list endpoint."""
    value = payload.get('obj') if isinstance(payload, dict) else payload
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ('clients', 'items', 'records', 'list', 'data'):
            rows = value.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _detect_telegram_inbound_profiles(server: Server):
    """Inspect existing v3 clients and count their exact valid inbound sets."""
    panel_session, error = get_xui_session(server)
    if not panel_session or error:
        raise ValueError(error or 'Could not connect to the selected server')
    if not server_is_v3(server, panel_session):
        raise ValueError('Auto Detect requires 3x-ui v3 or newer; legacy panels need one manual inbound')
    ok, payload, api_error = _v3_get(server, panel_session, '/panel/api/clients/list')
    if not ok:
        raise ValueError(api_error or 'Could not read v3 client assignments')
    valid_ids = {row['id'] for row in _telegram_customer_inbounds(server.id)}
    counts = {}
    for client in _v3_client_rows(payload):
        raw_ids = client.get('inboundIds')
        if raw_ids is None:
            raw_ids = client.get('inbound_ids')
        combination = []
        for value in raw_ids if isinstance(raw_ids, list) else []:
            try:
                inbound_id = int(value)
            except (TypeError, ValueError):
                continue
            if inbound_id in valid_ids and inbound_id not in combination:
                combination.append(inbound_id)
        signature = tuple(sorted(combination))
        if signature:
            counts[signature] = counts.get(signature, 0) + 1
    profiles = [{
        'inbound_ids': list(signature),
        'client_count': count,
        'signature': ','.join(str(value) for value in signature),
    } for signature, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]
    if not profiles:
        raise ValueError('No existing v3 client has a valid inbound assignment combination on this server')
    recurring = [profile for profile in profiles if profile['client_count'] >= 2]
    return recurring or profiles


def _save_telegram_purchase_routes(bot: TelegramBotInstance, raw_routes):
    if not isinstance(raw_routes, list) or len(raw_routes) > 2000:
        raise ValueError('purchase_inbound_routes must be a list of at most 2000 rows')
    valid_packages = {row.id for row in Package.query.filter_by(enabled=True).all()}
    valid_servers = {row.id for row in Server.query.filter_by(enabled=True, hidden=False).all()}
    valid_inbounds = {
        server_id: {row['id'] for row in _telegram_customer_inbounds(server_id)}
        for server_id in valid_servers
    }
    seen = set()
    for raw in raw_routes:
        if not isinstance(raw, dict):
            raise ValueError('Invalid purchase inbound route')
        try:
            package_id = int(raw.get('package_id'))
            server_id = int(raw.get('server_id'))
        except (TypeError, ValueError):
            raise ValueError('Invalid package or server in purchase inbound route')
        key = (package_id, server_id)
        if key in seen or package_id not in valid_packages or server_id not in valid_servers:
            raise ValueError('Purchase inbound route is unavailable or duplicated')
        seen.add(key)
        mode = str(raw.get('mode') or 'manual').strip().lower()
        if mode not in TELEGRAM_INBOUND_ROUTE_MODES:
            raise ValueError('Invalid purchase inbound route mode')
        inbound_ids = []
        for value in raw.get('inbound_ids') if isinstance(raw.get('inbound_ids'), list) else []:
            try:
                inbound_id = int(value)
            except (TypeError, ValueError):
                continue
            if inbound_id in valid_inbounds.get(server_id, set()) and inbound_id not in inbound_ids:
                inbound_ids.append(inbound_id)
        enabled = bool(raw.get('enabled', True))
        if enabled and mode == 'manual' and not inbound_ids:
            raise ValueError('Each enabled manual route needs at least one active client inbound')
        row = TelegramPurchaseInboundRoute.query.filter_by(
            bot_instance_id=bot.id, package_id=package_id, server_id=server_id,
        ).first()
        if row is None:
            row = TelegramPurchaseInboundRoute(
                bot_instance_id=bot.id, package_id=package_id, server_id=server_id,
            )
            db.session.add(row)
        row.mode = mode
        row.inbound_ids_json = json.dumps(sorted(inbound_ids), separators=(',', ':'))
        row.enabled = enabled
    for existing in TelegramPurchaseInboundRoute.query.filter_by(bot_instance_id=bot.id).all():
        if (existing.package_id, existing.server_id) not in seen:
            db.session.delete(existing)


def _save_telegram_purchase_settings(bot: TelegramBotInstance, data: dict):
    policy_data = data.get('purchase_policy')
    server_rows = data.get('purchase_servers')
    inbound_routes = data.get('purchase_inbound_routes')
    if policy_data is None and server_rows is None and inbound_routes is None:
        return
    if not isinstance(policy_data, dict):
        raise ValueError('purchase_policy must be an object')
    policy = _telegram_purchase_policy(bot, create=True)
    strategy = str(policy_data.get('assignment_strategy') or 'least_clients').strip().lower()
    if strategy not in TELEGRAM_PURCHASE_STRATEGIES:
        raise ValueError('Invalid Telegram purchase assignment strategy')
    name_mode = str(policy_data.get('account_name_mode') or 'generated').strip().lower()
    if name_mode not in TELEGRAM_ACCOUNT_NAME_MODES:
        raise ValueError('Invalid Telegram account naming mode')
    template = _validate_telegram_account_name_template(policy_data.get('account_name_template'))
    customer_selects = bool(policy_data.get('customer_selects_server', False))

    if not isinstance(server_rows, list) or len(server_rows) > 200:
        raise ValueError('purchase_servers must be a list of at most 200 rows')
    valid_servers = {
        server.id: server for server in Server.query.filter_by(enabled=True, hidden=False).all()
    }
    seen = set()
    visible_count = 0
    eligible_count = 0
    for raw in server_rows:
        if not isinstance(raw, dict):
            raise ValueError('Invalid purchase server rule')
        try:
            server_id = int(raw.get('server_id'))
        except (TypeError, ValueError):
            raise ValueError('Invalid purchase server ID')
        if server_id not in valid_servers or server_id in seen:
            raise ValueError('Purchase server is unavailable or duplicated')
        seen.add(server_id)
        rule = TelegramPurchaseServerRule.query.filter_by(
            bot_instance_id=bot.id, server_id=server_id,
        ).first()
        if rule is None:
            rule = TelegramPurchaseServerRule(bot_instance_id=bot.id, server_id=server_id)
            db.session.add(rule)
        rule.eligible = bool(raw.get('eligible', True))
        rule.customer_visible = bool(raw.get('customer_visible', False)) and rule.eligible
        rule.display_name = str(raw.get('display_name') or valid_servers[server_id].name).strip()[:120]
        rule.priority = max(0, min(100000, int(raw.get('priority') or 100)))
        rule.weight = max(1, min(10000, int(raw.get('weight') or 1)))
        eligible_count += int(rule.eligible)
        visible_count += int(rule.customer_visible)
    TelegramPurchaseServerRule.query.filter(
        TelegramPurchaseServerRule.bot_instance_id == bot.id,
        TelegramPurchaseServerRule.server_id.notin_(seen or {-1}),
    ).delete(synchronize_session=False)
    if not eligible_count:
        raise ValueError('Enable at least one server for Telegram purchases')
    if customer_selects and not visible_count:
        raise ValueError('Customer server selection needs at least one visible eligible server')

    policy.customer_selects_server = customer_selects
    policy.assignment_strategy = strategy
    policy.account_name_mode = name_mode
    policy.account_name_template = template
    policy.trial_enabled = bool(policy_data.get('trial_enabled', policy.trial_enabled))
    if 'trial_package_id' in policy_data:
        trial_package_id = policy_data.get('trial_package_id')
        if trial_package_id in (None, '', 0):
            policy.trial_package_id = None
        else:
            try:
                trial_package = db.session.get(Package, int(trial_package_id))
            except (TypeError, ValueError):
                trial_package = None
            if not trial_package or not getattr(trial_package, 'is_trial', False):
                raise ValueError('Trial package must reference a package marked as trial')
            policy.trial_package_id = trial_package.id
    policy.trial_requires_channel_membership = bool(policy_data.get(
        'trial_requires_channel_membership',
        policy.trial_requires_channel_membership,
    ))
    if 'trial_channel_chat_id' in policy_data:
        trial_channel_chat_id = policy_data.get('trial_channel_chat_id')
        if trial_channel_chat_id in (None, ''):
            policy.trial_channel_chat_id = None
        else:
            try:
                policy.trial_channel_chat_id = int(trial_channel_chat_id)
            except (TypeError, ValueError):
                raise ValueError('Trial channel chat ID must be a whole number')
    if 'trial_channels' in policy_data:
        raw_channels = policy_data.get('trial_channels') or []
        if not isinstance(raw_channels, list):
            raise ValueError('Trial channels must be a list')
        if len(raw_channels) > 10:
            raise ValueError('At most 10 trial channels are allowed')
        channels = []
        for item in raw_channels:
            if not isinstance(item, dict):
                raise ValueError('Each trial channel must be an object')
            try:
                chat_id = int(item.get('chat_id'))
            except (TypeError, ValueError):
                raise ValueError('Trial channel chat ID must be a whole number')
            title = str(item.get('title') or '').strip()[:200]
            invite_url = str(item.get('invite_url') or '').strip()[:200]
            channels.append({'chat_id': chat_id, 'title': title, 'invite_url': invite_url})
        policy.trial_channel_list_json = json.dumps(channels, separators=(',', ':'))
    policy.emergency_enabled = bool(policy_data.get('emergency_enabled', policy.emergency_enabled))
    try:
        policy.emergency_days = int(policy_data.get('emergency_days', policy.emergency_days or 1))
        policy.emergency_volume_gb = int(policy_data.get('emergency_volume_gb', policy.emergency_volume_gb or 1))
        policy.emergency_cooldown_days = int(policy_data.get(
            'emergency_cooldown_days', policy.emergency_cooldown_days or 30))
    except (TypeError, ValueError):
        raise ValueError('Emergency days, volume, and cooldown must be whole numbers')
    if not 1 <= policy.emergency_days <= 365:
        raise ValueError('Emergency days must be between 1 and 365')
    if not 0 <= policy.emergency_volume_gb <= 1024:
        raise ValueError('Emergency volume must be between 0 and 1024 GB')
    if not 1 <= policy.emergency_cooldown_days <= 365:
        raise ValueError('Emergency cooldown must be between 1 and 365 days')
    if policy.trial_enabled and not policy.trial_package_id:
        raise ValueError('Choose a trial package before enabling the trial')
    if policy.trial_requires_channel_membership and not (
            policy.trial_channel_chat_id or policy.trial_channels()):
        raise ValueError('Enter at least one Telegram channel before requiring trial membership')
    if inbound_routes is not None:
        _save_telegram_purchase_routes(bot, inbound_routes)


def _encrypt_telegram_secret(value: str) -> str:
    if not _get_server_password_fernet():
        raise RuntimeError('SERVER_PASSWORD_KEY is required before Telegram secrets can be saved')
    return encrypt_server_password(value)


def _decrypt_telegram_secret(value: str | None) -> str:
    return decrypt_server_password(value or '')


def _validate_telegram_token(token: str) -> bool:
    left, sep, right = (token or '').strip().partition(':')
    return bool(sep and left.isdigit() and 5 <= len(left) <= 20 and len(right) >= 20
                and re.fullmatch(r'[A-Za-z0-9_-]+', right))


def _telegram_proxy_mapping(endpoint: TelegramProxyEndpoint) -> dict:
    scheme = 'socks5h' if endpoint.proxy_type == 'socks5' else 'http'
    username = _decrypt_telegram_secret(endpoint.username_encrypted)
    password = _decrypt_telegram_secret(endpoint.password_encrypted)
    base = f'{scheme}://{endpoint.host}:{int(endpoint.port)}'
    url = _inject_proxy_credentials(base, username, password)
    return {'http': url, 'https': url}


def _telegram_bot_api_client(bot: TelegramBotInstance):
    """Build the interactive bot client with the same ordered routes as diagnostics."""
    from telegram_bot_runtime import TelegramBotApi, TelegramRoute

    token = _decrypt_telegram_secret(bot.token_encrypted)
    if not token or token.startswith(SERVER_PASSWORD_PREFIX):
        raise ValueError('Bot token is not configured or cannot be decrypted')
    proxy_rows = TelegramProxyEndpoint.query.filter_by(
        bot_instance_id=bot.id, enabled=True,
    ).order_by(TelegramProxyEndpoint.priority.asc(), TelegramProxyEndpoint.id.asc()).all()
    egress_rows = TelegramEgressProfile.query.filter_by(
        bot_instance_id=bot.id, enabled=True,
    ).filter(
        TelegramEgressProfile.source_type != 'telegram_backup_account',
    ).order_by(TelegramEgressProfile.priority.asc(), TelegramEgressProfile.id.asc()).all()
    managed = sorted(
        [('egress', row) for row in egress_rows] + [('proxy', row) for row in proxy_rows],
        key=lambda item: (
            int(item[1].priority or 0),
            0 if item[0] == 'egress' else 1,
            int(item[1].id or 0),
        ),
    )
    routes = []
    for kind, row in managed:
        if kind == 'egress':
            proxy_url = f'socks5h://127.0.0.1:{int(row.local_port)}'
            routes.append(TelegramRoute(
                f'xray://{row.name}', {'http': proxy_url, 'https': proxy_url},
            ))
        else:
            routes.append(TelegramRoute(
                f'{row.proxy_type}://{row.host}:{row.port}', _telegram_proxy_mapping(row),
            ))
    direct = TelegramRoute('direct')
    if bot.connection_mode == 'direct_only':
        routes = [direct]
    elif bot.connection_mode == 'proxy_first':
        routes.append(direct)
    elif bot.connection_mode == 'auto':
        routes.insert(0, direct)
    if not routes:
        raise ValueError('No usable Telegram route is configured')
    return TelegramBotApi(token, routes)


def _telegram_bot_attempt(token: str, route_name: str, proxies=None, proxy=None) -> dict:
    started = time.perf_counter()
    username = _decrypt_telegram_secret(proxy.username_encrypted) if proxy else None
    password = _decrypt_telegram_secret(proxy.password_encrypted) if proxy else None
    transport = probe_telegram_transport(
        proxy_type=proxy.proxy_type if proxy else None,
        host=proxy.host if proxy else None,
        port=proxy.port if proxy else None,
        username=username,
        password=password,
    )
    stages = list(transport.get('stages') or [])
    if not transport.get('success'):
        return {
            'success': False, 'route': route_name,
            'latency_ms': max(0, int((time.perf_counter() - started) * 1000)),
            'error': transport.get('error') or 'Transport diagnostic failed',
            'error_code': transport.get('error_code'), 'stages': stages,
        }
    api_started = time.perf_counter()
    api_attempts = 0
    while True:
        api_attempts += 1
        try:
            response = _telegram_get_me(token, proxies=proxies, timeout_sec=10)
            break
        except Exception as exc:
            error_code, safe_error = classify_telegram_connection_error(
                exc, (token, username, password),
            )
            if error_code in {'route_outbound_closed', 'telegram_api_timeout'} and api_attempts < 2:
                # A healthy Shadowsocks route can occasionally lose one upstream
                # TLS connection. Retry once with requests' fresh one-shot pool.
                time.sleep(0.25)
                continue
            latency = max(0, int((time.perf_counter() - started) * 1000))
            api_latency = max(0, int((time.perf_counter() - api_started) * 1000))
            stages.append({'name': 'telegram_api', 'status': 'failed', 'latency_ms': api_latency,
                           'error_code': error_code, 'message': safe_error})
            return {
                'success': False, 'route': route_name,
                'latency_ms': latency, 'error': safe_error,
                'error_code': error_code, 'api_attempts': api_attempts, 'stages': stages,
            }

    latency = max(0, int((time.perf_counter() - started) * 1000))
    api_latency = max(0, int((time.perf_counter() - api_started) * 1000))
    try:
        body = response.json() if response.content else {}
    except Exception:
        body = {}
    result = body.get('result') if isinstance(body, dict) else None
    if response.status_code == 200 and isinstance(body, dict) and body.get('ok') and isinstance(result, dict):
        stages.append({'name': 'telegram_api', 'status': 'passed', 'latency_ms': api_latency})
        return {
            'success': True, 'route': route_name, 'latency_ms': latency,
            'bot_user_id': result.get('id'), 'bot_username': result.get('username'),
            'bot_name': result.get('first_name'), 'api_attempts': api_attempts, 'stages': stages,
        }
    description = body.get('description') if isinstance(body, dict) else None
    safe_error = redact_connection_error(
        description or f'Telegram returned HTTP {response.status_code}',
        (token, username, password),
    )
    stages.append({'name': 'telegram_api', 'status': 'failed', 'latency_ms': api_latency,
                   'error_code': 'telegram_api_rejected', 'message': safe_error})
    return {
        'success': False, 'route': route_name, 'latency_ms': latency,
        'error': safe_error, 'error_code': 'telegram_api_rejected',
        'api_attempts': api_attempts, 'stages': stages,
    }


def _telegram_bot_diagnostic(bot: TelegramBotInstance, route='configured', only_proxy_id=None,
                             only_egress_id=None) -> dict:
    token = _decrypt_telegram_secret(bot.token_encrypted)
    if not token or token.startswith(SERVER_PASSWORD_PREFIX):
        return {'success': False, 'error': 'Bot token is not configured or cannot be decrypted', 'attempts': []}

    proxies = TelegramProxyEndpoint.query.filter_by(bot_instance_id=bot.id, enabled=True)
    if only_proxy_id is not None:
        proxies = proxies.filter_by(id=int(only_proxy_id))
    proxy_rows = proxies.order_by(TelegramProxyEndpoint.priority.asc(), TelegramProxyEndpoint.id.asc()).all()
    egresses = TelegramEgressProfile.query.filter_by(
        bot_instance_id=bot.id, enabled=True,
    ).filter(TelegramEgressProfile.source_type != 'telegram_backup_account')
    if only_egress_id is not None:
        egresses = egresses.filter_by(id=int(only_egress_id))
    egress_rows = egresses.order_by(
        TelegramEgressProfile.priority.asc(), TelegramEgressProfile.id.asc(),
    ).all()
    managed_rows = sorted(
        [('egress', row) for row in egress_rows] + [('proxy', row) for row in proxy_rows],
        key=lambda item: (
            int(item[1].priority or 0),
            0 if item[0] == 'egress' else 1,
            int(item[1].id or 0),
        ),
    )
    attempts = []
    if only_proxy_id is not None:
        order = [('proxy', row) for row in proxy_rows]
    elif only_egress_id is not None:
        order = [('egress', row) for row in egress_rows]
    elif route == 'direct':
        order = [('direct', None)]
    elif bot.connection_mode == 'direct_only':
        order = [('direct', None)]
    elif bot.connection_mode == 'proxy_only':
        order = managed_rows
    elif bot.connection_mode == 'proxy_first':
        order = managed_rows + [('direct', None)]
    else:
        order = [('direct', None)] + managed_rows

    for kind, endpoint in order:
        if kind == 'direct':
            proxy = None
            route_name = 'direct'
        elif kind == 'egress':
            proxy = SimpleNamespace(
                proxy_type='socks5', host='127.0.0.1', port=endpoint.local_port,
                username_encrypted=None, password_encrypted=None,
            )
            route_name = f'xray://{endpoint.name}'
        else:
            proxy = endpoint
            route_name = f'{proxy.proxy_type}://{proxy.host}:{proxy.port}'
        result = _telegram_bot_attempt(
            token, route_name, proxies=(_telegram_proxy_mapping(proxy) if proxy else None),
            proxy=proxy,
        )
        attempts.append(result)
        now = datetime.utcnow()
        if endpoint:
            endpoint.last_latency_ms = result.get('latency_ms')
            if result['success']:
                endpoint.health_status = 'healthy'
                endpoint.failure_count = 0
                endpoint.last_error = None
                endpoint.last_success_at = now
            else:
                endpoint.health_status = 'failed'
                endpoint.failure_count = int(endpoint.failure_count or 0) + 1
                endpoint.last_error = result.get('error')
                endpoint.last_failure_at = now
        if result['success']:
            conflict = _telegram_bot_token_conflict(
                bot, token=f"{result.get('bot_user_id')}:x") if result.get('bot_user_id') else None
            if conflict:
                result = {
                    'success': False,
                    'error': f"This token is already used by another bot ({conflict.display_name})",
                    'attempts': attempts,
                }
                bot.last_test_status = 'failed'
                bot.last_test_route = result.get('route') or attempts[-1].get('route')
                bot.last_test_latency_ms = None
                bot.last_test_error = result['error']
                bot.last_test_at = now
                db.session.commit()
                return result
            bot.bot_user_id = result.get('bot_user_id')
            bot.bot_username = result.get('bot_username')
            bot.last_test_status = 'healthy'
            bot.last_test_route = result.get('route')
            bot.last_test_latency_ms = result.get('latency_ms')
            bot.last_test_error = None
            bot.last_test_at = now
            db.session.commit()
            return {**result, 'attempts': attempts}

    bot.last_test_status = 'failed'
    bot.last_test_route = attempts[-1].get('route') if attempts else None
    bot.last_test_latency_ms = attempts[-1].get('latency_ms') if attempts else None
    bot.last_test_error = attempts[-1].get('error') if attempts else 'No usable route configured'
    bot.last_test_at = datetime.utcnow()
    db.session.commit()
    result = {'success': False, 'error': bot.last_test_error, 'attempts': attempts}
    if only_egress_id is not None and any(
            attempt.get('error_code') == 'route_outbound_closed' for attempt in attempts):
        result['runtime_hint'] = (
            'The local Xray SOCKS listener is running, but the selected outbound route closed '
            'outbound traffic. Choose an enabled inbound and active client, or paste a known-working URI.'
        )
    return result


def _telegram_proxy_from_payload(proxy, data):
    proxy_type = str(data.get('proxy_type') or proxy.proxy_type or 'socks5').strip().lower()
    if proxy_type not in ('socks5', 'http'):
        raise ValueError('Proxy type must be socks5 or http')
    host = str(data.get('host') or proxy.host or '').strip()
    if not host or len(host) > 255 or any(char in host for char in '/?#@'):
        raise ValueError('A valid proxy host is required')
    try:
        port = int(data.get('port') if data.get('port') is not None else proxy.port)
        priority = int(data.get('priority') if data.get('priority') is not None else proxy.priority)
    except (TypeError, ValueError):
        raise ValueError('Proxy port and priority must be whole numbers')
    if port < 1 or port > 65535:
        raise ValueError('Proxy port must be between 1 and 65535')
    proxy.proxy_type = proxy_type
    proxy.host = host
    proxy.port = port
    proxy.priority = max(0, min(priority, 10000))
    if 'enabled' in data:
        proxy.enabled = bool(data.get('enabled'))
    for field, attr in (('username', 'username_encrypted'), ('password', 'password_encrypted')):
        value = str(data.get(field) or '').strip()
        if value:
            setattr(proxy, attr, _encrypt_telegram_secret(value))
    return proxy


TELEGRAM_EGRESS_PROTOCOLS = {'vless', 'vmess', 'trojan', 'shadowsocks', 'wireguard'}


def _find_telegram_egress_candidate(server_id, inbound_id, client_id):
    load_snapshot_from_redis()
    server = db.session.get(Server, int(server_id))
    if not server:
        raise ValueError('Server not found')
    inbound = next((row for row in (GLOBAL_SERVER_DATA.get('inbounds') or [])
                    if int(row.get('server_id') or 0) == server.id
                    and int(row.get('id') or 0) == int(inbound_id)), None)
    if not inbound:
        raise ValueError('Inbound is not available in the current server snapshot')
    protocol = str(inbound.get('protocol') or '').lower()
    if protocol not in TELEGRAM_EGRESS_PROTOCOLS:
        raise ValueError(f'The selected {protocol or "unknown"} inbound cannot be used as an Xray outbound')
    inbound_enabled = inbound.get('enable', inbound.get('enabled', True))
    if str(inbound_enabled).strip().lower() in {'false', '0', 'no', 'off'}:
        raise ValueError('The selected inbound is disabled')
    wanted = str(client_id or '')
    client = next((row for row in (inbound.get('clients') or [])
                   if wanted in {str(row.get('id') or ''), str(row.get('uuid') or ''),
                                 str(row.get('email') or '')}), None)
    if not client:
        raise ValueError('Client was not found in the selected inbound')
    raw_client = client.get('raw_client') if isinstance(client.get('raw_client'), dict) else {}
    client_enabled = raw_client.get('enable', client.get('enable', True))
    if str(client_enabled).strip().lower() in {'false', '0', 'no', 'off'}:
        raise ValueError('The selected client is disabled')
    try:
        expiry_ms = int(raw_client.get('expiryTime') or 0)
    except (TypeError, ValueError):
        expiry_ms = 0
    if expiry_ms > 0 and expiry_ms <= int(time.time() * 1000):
        raise ValueError('The selected client has expired')
    try:
        total_bytes = int(raw_client.get('totalGB') or client.get('totalGB') or 0)
        used_bytes = int(client.get('up') or 0) + int(client.get('down') or 0)
    except (TypeError, ValueError):
        total_bytes = used_bytes = 0
    if total_bytes > 0 and used_bytes >= total_bytes:
        raise ValueError('The selected client has no remaining traffic')
    uri = generate_client_link(client, inbound, server.host)
    if not uri:
        raise ValueError('Eve could not generate a connection configuration for this client')
    build_xray_config_from_uri(uri, 12080)
    return server, inbound, client, uri


def _telegram_operations_admin():
    return db.session.get(Admin, session.get('admin_id'))


def _telegram_operation_identity(telegram_user_id, customer_id=None):
    query = TelegramIdentity.query.filter_by(telegram_user_id=telegram_user_id)
    if customer_id is not None:
        identity = query.filter_by(customer_id=customer_id).first()
        if identity:
            return identity
    return query.first()


def _telegram_purchase_visible_to(admin, row):
    if not admin:
        return False
    if admin.is_superadmin or str(admin.role or '').lower() in ('admin', 'superadmin'):
        return True
    bot = db.session.get(TelegramBotInstance, row.bot_instance_id)
    return str(admin.role or '').lower() == 'reseller' and bot and bot.owner_admin_id == admin.id


def _telegram_announcement_datetime(value):
    if value in (None, ''):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        raise ValueError('Invalid date filter')


def _telegram_announcement_recipients(filters):
    query = db.session.query(TelegramBotUserState, TelegramIdentity).outerjoin(
        TelegramIdentity, TelegramIdentity.telegram_user_id == TelegramBotUserState.telegram_user_id,
    ).join(TelegramBotInstance, TelegramBotInstance.id == TelegramBotUserState.bot_instance_id).filter(
        TelegramBotInstance.archived_at.is_(None), TelegramBotInstance.enabled.is_(True))
    scope = filters.get('bot_scope', 'all')
    if scope == 'central':
        query = query.filter(TelegramBotInstance.owner_type == 'system')
    elif scope == 'reseller':
        query = query.filter(TelegramBotInstance.owner_type == 'reseller')
    elif scope == 'selected':
        ids = [int(v) for v in filters.get('bot_ids') or []]
        query = query.filter(TelegramBotUserState.bot_instance_id.in_(ids or [-1]))
    rows = query.order_by(TelegramBotUserState.id.asc()).all()
    server_ids = {int(v) for v in filters.get('server_ids') or []}
    event_ranges = {}
    for event in ('started', 'purchased', 'renewed'):
        start = _telegram_announcement_datetime(filters.get(f'{event}_from'))
        end = _telegram_announcement_datetime(filters.get(f'{event}_to'))
        if start or end:
            event_ranges[event] = (start, end)
    customer_ids = {identity.customer_id for _, identity in rows if identity and identity.customer_id}
    telegram_user_ids = {int(state.telegram_user_id) for state, _ in rows}
    customers_on_servers = set()
    if server_ids and customer_ids:
        customers_on_servers = {int(r[0]) for r in db.session.query(ServiceOwnership.customer_id).filter(
            ServiceOwnership.customer_id.in_(customer_ids), ServiceOwnership.revoked_at.is_(None),
            ServiceOwnership.server_id.in_(server_ids)).distinct().all()}
    purchase_dates = defaultdict(list)
    if 'purchased' in event_ranges and telegram_user_ids:
        for telegram_user_id, created_at in db.session.query(
                TelegramPurchaseRequest.telegram_user_id, TelegramPurchaseRequest.created_at).filter(
                TelegramPurchaseRequest.telegram_user_id.in_(telegram_user_ids),
                TelegramPurchaseRequest.status == 'completed').all():
            purchase_dates[int(telegram_user_id)].append(created_at)
    renewal_dates = defaultdict(list)
    if 'renewed' in event_ranges and customer_ids:
        for customer_id, created_at in db.session.query(
                CustomerTransaction.customer_id, CustomerTransaction.created_at).filter(
                CustomerTransaction.customer_id.in_(customer_ids), CustomerTransaction.type == 'renewal',
                CustomerTransaction.status == 'completed').all():
            renewal_dates[int(customer_id)].append(created_at)
    recipients = []
    for state, identity in rows:
        chat_id = identity.telegram_chat_id if identity else state.telegram_user_id
        customer_id = identity.customer_id if identity else None
        if filters.get('linked_only') and not customer_id:
            continue
        if server_ids and customer_id not in customers_on_servers:
            continue
        matches = []
        for event, (start, end) in event_ranges.items():
            if event == 'started':
                dates = [state.created_at]
            elif event == 'purchased':
                dates = purchase_dates.get(int(state.telegram_user_id), [])
            else:
                dates = renewal_dates.get(int(customer_id), []) if customer_id else []
            matches.append(any((not start or d >= start) and (not end or d <= end) for d in dates if d))
        if matches and (all(matches) if filters.get('event_match') == 'all' else any(matches)) is False:
            continue
        recipients.append({
            'bot_instance_id': state.bot_instance_id,
            'telegram_user_id': int(state.telegram_user_id),
            'chat_id': int(chat_id), 'customer_id': customer_id,
        })
    return recipients


def _queue_telegram_announcement(row):
    if row.status not in ('draft', 'paused'):
        raise ValueError('Only draft or paused announcements can be queued')
    if row.status == 'draft':
        recipients = _telegram_announcement_recipients(row.filters())
        for item in recipients:
            db.session.add(TelegramAnnouncementDelivery(announcement_id=row.id, **item))
        row.total_count = len(recipients)
    row.status = 'queued'
    row.started_at = row.started_at or datetime.utcnow()
    row.finished_at = datetime.utcnow() if not row.total_count else None
    if not row.total_count:
        row.status = 'completed'


def _verify_required_channels(bot: TelegramBotInstance, channels: list[dict]) -> str | None:
    """Check the bot can see and administers every required channel.

    Returns an admin-facing error message, or None when all channels check out.
    Skipped entirely when the bot has no usable token/route yet. Network or
    route failures are reported as a soft 'could not verify' message instead of
    a 500.
    """
    if not channels:
        return None
    try:
        api = _telegram_bot_api_client(bot)
    except ValueError:
        return None  # no token/route configured yet — nothing to verify with
    from telegram_bot_runtime import TelegramApiError, is_chat_access_error
    try:
        bot_user_id = int(bot.bot_user_id) if bot.bot_user_id else None
        if bot_user_id is None:
            me, _route = api.call('getMe', {})
            bot_user_id = int((me or {}).get('id') or 0) or None
            if bot_user_id is None:
                return 'Could not read the bot identity from Telegram (getMe).'
            bot.bot_user_id = bot_user_id
            username = str((me or {}).get('username') or '').strip()[:64]
            if username:
                bot.bot_username = username
        for channel in channels:
            label = f"{channel.get('title') or channel['chat_id']} ({channel['chat_id']})"
            try:
                member, _route = api.call('getChatMember', {
                    'chat_id': int(channel['chat_id']), 'user_id': bot_user_id,
                })
            except TelegramApiError as exc:
                if is_chat_access_error(exc):
                    return (f'Bot is not an admin in channel {label}. Add the bot as an '
                            f'admin, then save again. Telegram said: {exc}')
                return ('Could not verify the required channels right now '
                        f'({exc}). Check the bot route/proxy and try again.')
            status = str((member or {}).get('status') or '')
            if status not in ('administrator', 'creator'):
                return (f'Bot is not an admin in channel {label} '
                        f'(current status: {status or "unknown"}). '
                        'Add the bot as an admin, then save again.')
        return None
    except TelegramApiError as exc:
        return ('Could not verify the required channels right now '
                f'({exc}). Check the bot route/proxy and try again.')
    except Exception:
        app.logger.exception('[telegram-settings] required-channel verification failed for bot %s', bot.id)
        return 'Could not verify the required channels right now. Check the bot route/proxy and try again.'


def _save_telegram_bot_settings(bot: TelegramBotInstance, data: dict):
    display_name = str(data.get('display_name') or bot.display_name or '').strip()[:120]
    if not display_name:
        return jsonify({'success': False, 'error': 'Bot display name is required'}), 400
    token = str(data.get('bot_token') or '').strip()
    if token and not _validate_telegram_token(token):
        return jsonify({'success': False, 'error': 'Bot token format is invalid'}), 400
    languages = data.get('enabled_languages')
    if not isinstance(languages, list):
        languages = bot.enabled_languages()
    languages = [lang for lang in ('fa', 'en') if lang in languages]
    if not languages:
        return jsonify({'success': False, 'error': 'At least one bot language must be enabled'}), 400
    default_language = str(data.get('default_language') or bot.default_language or 'fa').strip().lower()
    if default_language not in languages:
        return jsonify({'success': False, 'error': 'Default language must be enabled'}), 400
    connection_mode = str(data.get('connection_mode') or bot.connection_mode or 'proxy_first').strip().lower()
    if connection_mode not in ('auto', 'direct_only', 'proxy_first', 'proxy_only'):
        return jsonify({'success': False, 'error': 'Invalid connection mode'}), 400
    support_group_enabled = bool(data.get('support_group_enabled', bot.support_group_enabled))
    support_group_chat_id = data.get('support_group_chat_id')
    if support_group_chat_id in (None, ''):
        support_group_chat_id = None
    else:
        try:
            support_group_chat_id = int(str(support_group_chat_id).strip())
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Support group chat ID must be numeric'}), 400
        if support_group_chat_id >= 0:
            return jsonify({'success': False, 'error': 'Support group chat ID must be a negative Telegram group ID'}), 400
    if support_group_enabled and support_group_chat_id is None:
        return jsonify({'success': False, 'error': 'Support group chat ID is required when group routing is enabled'}), 400
    try:
        support_sla_minutes = int(data.get('support_sla_minutes', bot.support_sla_minutes or 60))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Support SLA must be a whole number of minutes'}), 400
    if support_sla_minutes < 0 or support_sla_minutes > 10080:
        return jsonify({'success': False, 'error': 'Support SLA must be between 0 and 10080 minutes'}), 400
    try:
        support_sla_warning_percent = int(data.get(
            'support_sla_warning_percent', bot.support_sla_warning_percent or 80,
        ))
        support_escalation_minutes = int(data.get(
            'support_escalation_minutes', bot.support_escalation_minutes or 30,
        ))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Support warning and escalation values must be whole numbers'}), 400
    if support_sla_warning_percent < 1 or support_sla_warning_percent > 99:
        return jsonify({'success': False, 'error': 'SLA warning must be between 1 and 99 percent'}), 400
    if support_escalation_minutes < 0 or support_escalation_minutes > 10080:
        return jsonify({'success': False, 'error': 'Support escalation must be between 0 and 10080 minutes'}), 400
    if token and _telegram_bot_token_conflict(bot, token=token):
        return jsonify({'success': False, 'error': 'This token is already used by another bot'}), 409
    try:
        if token:
            bot.token_encrypted = _encrypt_telegram_secret(token)
    except RuntimeError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    try:
        _save_telegram_purchase_settings(bot, data)
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 400
    bot.display_name = display_name
    bot.enabled = bool(data.get('enabled', bot.enabled))
    bot.test_mode = bool(data.get('test_mode', bot.test_mode))
    bot.enabled_languages_json = json.dumps(languages, separators=(',', ':'))
    bot.default_language = default_language
    bot.connection_mode = connection_mode
    bot.transport_mode = 'polling'
    bot.support_group_enabled = support_group_enabled
    bot.support_group_chat_id = support_group_chat_id
    bot.support_group_topics = bool(data.get('support_group_topics', True))
    bot.support_sla_minutes = support_sla_minutes
    bot.support_sla_warning_percent = support_sla_warning_percent
    bot.support_escalation_minutes = support_escalation_minutes
    channels = data.get('required_channels', bot.required_channels())
    if not isinstance(channels, list) or len(channels) > 20:
        return jsonify({'success': False, 'error': 'required_channels must be a list of at most 20 channels'}), 400
    cleaned_channels = []
    seen_chat_ids = set()
    for item in channels:
        if not isinstance(item, dict):
            return jsonify({'success': False, 'error': 'Each required channel must be an object'}), 400
        try:
            chat_id = int(str(item.get('chat_id') or '').strip())
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'Required channel chat ID must be numeric'}), 400
        title = str(item.get('title') or '').strip()[:80]
        invite_url = str(item.get('invite_url') or '').strip()[:500]
        if chat_id >= 0 or chat_id in seen_chat_ids:
            return jsonify({'success': False, 'error': 'Channel chat IDs must be unique negative IDs'}), 400
        if not re.match(r'^https://t\.me/(?:\+|joinchat/)?[^\s]+$', invite_url, re.I):
            return jsonify({'success': False, 'error': 'Each channel needs a valid https://t.me join URL'}), 400
        seen_chat_ids.add(chat_id)
        cleaned_channels.append({'chat_id': chat_id, 'title': title or str(chat_id), 'invite_url': invite_url})
    require_start = bool(data.get('require_membership_on_start', bot.require_membership_on_start))
    require_delivery = bool(data.get('require_membership_on_delivery', bot.require_membership_on_delivery))
    if (require_start or require_delivery) and not cleaned_channels:
        return jsonify({'success': False, 'error': 'Add at least one required channel before enabling membership checks'}), 400
    verification_error = _verify_required_channels(bot, cleaned_channels)
    if verification_error:
        return jsonify({'success': False, 'error': verification_error}), 400
    bot.required_channels_json = json.dumps(cleaned_channels, ensure_ascii=False, separators=(',', ':'))
    bot.require_membership_on_start = require_start
    bot.require_membership_on_delivery = require_delivery
    bot.phone_allow_international = bool(data.get(
        'phone_allow_international', bot.phone_allow_international))
    copy_overrides = data.get('copy_overrides')
    if copy_overrides is not None:
        if not isinstance(copy_overrides, dict):
            return jsonify({'success': False, 'error': 'copy_overrides must be an object'}), 400
        from telegram_bot_runtime import COPY as TELEGRAM_BOT_COPY
        cleaned_overrides = {}
        for raw_key, spec in copy_overrides.items():
            copy_key = str(raw_key).strip()
            if copy_key not in TELEGRAM_BOT_COPY['fa']:
                return jsonify({'success': False, 'error': f'Unknown copy key: {copy_key}'}), 400
            if not isinstance(spec, dict):
                return jsonify({'success': False, 'error': f'Copy override for {copy_key} must be an object'}), 400
            entry = {}
            for lang in ('fa', 'en'):
                value = spec.get(lang)
                if value is None:
                    continue
                if not isinstance(value, str):
                    return jsonify({'success': False, 'error': f'Copy override {copy_key}.{lang} must be text'}), 400
                value = value.strip()
                if len(value) > 500:
                    return jsonify({'success': False, 'error': f'Copy override {copy_key}.{lang} must be at most 500 characters'}), 400
                if value:
                    entry[lang] = value
            hidden = spec.get('hidden')
            if hidden is not None:
                if not isinstance(hidden, bool):
                    return jsonify({'success': False, 'error': f'Copy override {copy_key}.hidden must be a boolean'}), 400
                if hidden:
                    entry['hidden'] = True
            if entry:
                cleaned_overrides[copy_key] = entry
        bot.copy_overrides_json = (
            json.dumps(cleaned_overrides, ensure_ascii=False) if cleaned_overrides else ''
        )
    if bot.enabled and not bot.token_encrypted:
        return jsonify({'success': False, 'error': 'Configure a bot token before enabling the bot'}), 400
    _log_audit('telegram_bot.settings_update', bot,
               actor=db.session.get(Admin, session.get('admin_id')),
               meta={'bot_id': bot.id, 'scope_key': bot.scope_key})
    db.session.commit()
    return jsonify({'success': True, 'bot': bot.to_safe_dict()})


def _refresh_update_cache_async():
    """Populate UPDATE_CACHE from GitHub in a daemon thread (non-blocking).

    Overview page must not block on a network call. We kick this off and return
    immediately; the next page load reads the populated cache.
    """
    if UPDATE_CACHE.get('_refreshing'):
        return
    UPDATE_CACHE['_refreshing'] = True

    def _work():
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                timeout=5
            )
            if resp.status_code == 200:
                gh = resp.json()
                latest_raw = gh.get('tag_name', '').strip().lstrip('vV')
                update_available = False
                is_beta = False
                try:
                    cur_parts = [int(x) for x in APP_VERSION.split('.')]
                    lat_parts = [int(x) for x in latest_raw.split('.')]
                    while len(cur_parts) < 3: cur_parts.append(0)
                    while len(lat_parts) < 3: lat_parts.append(0)
                    update_available = lat_parts > cur_parts
                    is_beta = cur_parts > lat_parts
                except Exception:
                    pass
                UPDATE_CACHE['data'] = {
                    'current_version': APP_VERSION,
                    'latest_version': latest_raw,
                    'update_available': update_available,
                    'is_beta': is_beta,
                    'release_url': gh.get('html_url', ''),
                }
                UPDATE_CACHE['last_check'] = time.time()
        except Exception:
            pass
        finally:
            UPDATE_CACHE['_refreshing'] = False

    threading.Thread(target=_work, daemon=True).start()




# Background schedulers/watchdogs extracted to panel.jobs.schedulers (re-exported).
from panel.jobs.schedulers import (  # noqa: F401
    BACKGROUND_THREADS_STARTED,
    _run_snapshot_with_progress,
    inject_version,
    background_data_fetcher,
    snapshot_reader_worker,
    fetch_and_update_global_data,
    run_scheduler,
    update_session_lifetime,
    HEALTH_CHECK_INTERVAL,
    _CRITICAL_STATIC_FILES,
    _health_check_db,
    _health_check_static_files,
    _health_check_disk,
    _health_check_servers,
    _run_single_health_cycle,
    health_watchdog,
    _USAGE_HOURLY_RETENTION_HOURS,
    _USAGE_DAILY_RETENTION_DAYS,
    _USAGE_LEGACY_MIGRATION_KEY,
    _USAGE_LEGACY_MIGRATION_ID,
    _USAGE_TEHRAN_OFFSET,
    _usage_tehran_date,
    _seconds_until_next_usage_hour,
    _coerce_usage_datetime,
    _usage_account_points,
    _usage_delta,
    _renewal_from_counter_reset,
    _collect_usage_rollups,
    _take_usage_snapshots,
    _legacy_usage_table_name,
    _legacy_usage_table_exists,
    _usage_migration_record,
    get_usage_migration_status,
    _migrate_legacy_usage_snapshots_v248,
    _migrate_legacy_usage_snapshots,
    usage_snapshot_worker,
    _SINGLETON_LOCK_FDS,
    _claim_singleton,
    PULSE_WORKER_POLL_SECONDS,
    _pulse_maybe_alert,
    _pulse_send_telegram_alert,
    pulse_scheduler_tick,
    pulse_scheduler_worker,
    ensure_background_threads_started,
)
app.context_processor(inject_version)  # decorator cannot live in panel (needs app at import)
if __name__ == '__main__':
    # Create tables if not exist
    with app.app_context():
        db.create_all()
    
    update_session_lifetime()

    # Ensure background threads are running
    if not os.environ.get('DISABLE_BACKGROUND_THREADS'):
        ensure_background_threads_started()
    
    app.run(host='0.0.0.0', port=5000, debug=True)
