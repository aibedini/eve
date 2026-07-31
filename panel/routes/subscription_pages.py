"""Public subscription page, usage-history, and direct-link routes (extracted from app.py)."""
import base64
import json
import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

from flask import (
    Blueprint, g, jsonify, make_response, render_template, request, session,
)
from sqlalchemy import func, or_

from panel.adapters.xui import fetch_inbounds, get_xui_session, persist_detected_panel_type
from panel.extensions import db, limiter
from panel.models import (
    Admin, Announcement, BackupConfig, ClientOwnership, FAQ, OnlineChatScript,
    RenewalEvent, Server, SubAppConfig, SystemConfig, UsageCounterState,
    UsageDaily,
)
from panel.routes.common import login_required
from panel.services.backup import _parse_int
from panel.services.billing import (
    _build_sub_page_packages, _build_subscription_package_recommendation,
)
from panel.services.subscription import (
    build_subscription_profile_title,
    build_subscription_configs,
    ensure_subscription_identity,
    fetch_authoritative_subscription_configs,
    fetch_subscription_profile_metadata,
    find_subscription_client_email,
    sort_subscription_configs,
)

bp = Blueprint('subscription_pages', __name__)


def _subscription_inbound_clients(inbound):
    settings = inbound.get('settings') or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except (TypeError, ValueError):
            settings = {}
    if not isinstance(settings, dict):
        return []
    clients = settings.get('clients') or []
    return clients if isinstance(clients, list) else []


def _find_live_subscription_client(inbounds, sub_id):
    normalized_sub_id = str(sub_id or '').strip()
    for inbound in inbounds or []:
        for client in _subscription_inbound_clients(inbound):
            client_sub_id = str(client.get('subId') or '').strip()
            client_uuid = str(client.get('id') or '').strip()
            if normalized_sub_id and (
                normalized_sub_id == client_sub_id
                or (not client_sub_id and normalized_sub_id == client_uuid)
            ):
                return client, inbound
    return None, None


def _fetch_live_subscription_context(server, sub_id):
    """Read credentials from X-UI for every subscription request."""
    session_obj, login_error = get_xui_session(server)
    if login_error or not session_obj:
        return None, [], None, None, login_error or 'Panel authentication failed'

    inbounds, fetch_error, detected_type = fetch_inbounds(
        session_obj, server.host, server.panel_type,
    )
    if fetch_error:
        return session_obj, [], None, None, fetch_error

    persist_detected_panel_type(server, detected_type)
    target_client, target_inbound = _find_live_subscription_client(inbounds, sub_id)
    return session_obj, inbounds or [], target_client, target_inbound, None


@bp.route('/api/client/direct-link/<int:server_id>/<sub_id>')
@login_required
@limiter.limit("60 per minute")
def get_client_direct_link(server_id, sub_id):
    """
    Build direct config links from a fresh X-UI API read.

    The upstream subscription endpoint is deliberately not proxied because its
    payload can lag behind credential changes.
    """
    from app import app  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    server = db.session.get(Server, server_id)

    normalized_sub_id = str(sub_id).strip()

    # Basic hardening: keep the identifier path-safe (the upstream URL embeds it in the path)
    if any(c in normalized_sub_id for c in ('/', '\\', '?', '#', '@', ':')) or '..' in normalized_sub_id:
        return jsonify({"success": False, "error": "Invalid subscription ID"}), 400
    
    if not server:
        return jsonify({"success": False, "error": "Server not found"}), 404

    live_session, live_inbounds, target_client, target_inbound, live_error = (
        _fetch_live_subscription_context(server, normalized_sub_id)
    )
    if live_error:
        app.logger.warning(
            f"Direct subscription fetch failed for server {server_id}: {live_error}"
        )
        return jsonify({
            "success": False,
            "error": "Unable to load live subscription data",
        }), 502
    if not target_client or not target_inbound:
        return jsonify({"success": False, "error": "Subscription not found"}), 404

    resolved_client_uuid = str(target_client.get('id') or '').strip() or None
    resolved_client_email = str(target_client.get('email') or '').strip() or None
    
    # Check permission
    if user.role == 'reseller':
        ownership = None

        lookup_uuid = resolved_client_uuid or normalized_sub_id
        if lookup_uuid:
            ownership = ClientOwnership.query.filter_by(
                reseller_id=user.id,
                server_id=server_id,
                client_uuid=lookup_uuid
            ).first()

        if not ownership and resolved_client_email:
            ownership = ClientOwnership.query.filter(
                ClientOwnership.reseller_id == user.id,
                ClientOwnership.server_id == server_id,
                func.lower(ClientOwnership.client_email) == resolved_client_email.lower()
            ).first()

        if not ownership:
            return jsonify({"success": False, "error": "Access denied"}), 403
    
    # Build subscription URL
    host_value = server.host
    if host_value and not host_value.startswith(('http://', 'https://')):
        host_value = f"http://{host_value}"
    parsed_host = urlparse(host_value or '')
    hostname = parsed_host.hostname or parsed_host.path or ''
    scheme = parsed_host.scheme or 'http'
    final_port = server.sub_port if server.sub_port else parsed_host.port
    port_str = f":{final_port}" if final_port else ''
    sub_path = (server.sub_path or '/sub/').strip('/')
    base_sub = f"{scheme}://{hostname}{port_str}"
    safe_sub_id = quote(normalized_sub_id)
    sub_url = f"{base_sub}/{sub_path}/{safe_sub_id}" if sub_path else f"{base_sub}/{safe_sub_id}"
    
    configs = build_subscription_configs(
        server,
        normalized_sub_id,
        target_client,
        target_inbound,
        live_session=live_session,
        live_inbounds=live_inbounds,
    )
    configs = ensure_subscription_identity(configs, resolved_client_email)
    return jsonify({
        "success": True,
        "configs": configs,
        "direct_link": configs[0] if configs else None,
        "sub_url": sub_url,
    })
def _history_cursor_encode(period: str, boundary_date) -> str:
    payload = json.dumps({'v': 1, 'p': period, 'd': boundary_date.isoformat()}, separators=(',', ':'))
    return base64.urlsafe_b64encode(payload.encode('utf-8')).decode('ascii').rstrip('=')


def _history_cursor_decode(raw: str | None, period: str):
    if not raw:
        return None
    try:
        padded = str(raw) + ('=' * (-len(str(raw)) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8'))
        if payload.get('v') != 1 or payload.get('p') != period:
            return None
        return datetime.strptime(str(payload.get('d') or ''), '%Y-%m-%d').date()
    except Exception:
        return None


def _subscription_rollup_history(server_id: int, sub_id: str, period: str,
                                 cursor: str | None = None, page_size: int = 10):
    from app import _USAGE_TEHRAN_OFFSET  # deferred: app-level helper, avoids circular import
    cursor_date = _history_cursor_decode(cursor, period)
    query = UsageDaily.query.filter_by(server_id=server_id, sub_id=sub_id)
    if cursor:
        if cursor_date is None:
            return jsonify({'success': False, 'error': 'Invalid history cursor'}), 400
        query = query.filter(UsageDaily.usage_date < cursor_date)

    # Daily pages need N+1 rows. Monthly pages read at most N+1 complete months
    # worth of compact daily rollups, still bounded and independent of history age.
    row_limit = (page_size + 1) if period == 'day' else ((page_size + 1) * 32)
    rows = (query.order_by(UsageDaily.usage_date.desc()).limit(row_limit).all())

    buckets = {}
    for row in rows:
        key = row.usage_date.strftime('%Y-%m') if period == 'month' else row.usage_date.isoformat()
        item = buckets.setdefault(key, {
            'delta_upload': 0, 'delta_download': 0, 'delta_total': 0,
            'remaining': None, 'volume_limit': None, 'date': row.usage_date,
        })
        item['delta_upload'] += int(row.upload_bytes or 0)
        item['delta_download'] += int(row.download_bytes or 0)
        item['delta_total'] += int(row.upload_bytes or 0) + int(row.download_bytes or 0)
        item['remaining'] = row.remaining_bytes
        item['volume_limit'] = row.volume_limit_bytes
        item['date'] = row.usage_date

    history = []
    for key, item in buckets.items():
        local_midnight = datetime.combine(item['date'], datetime.min.time())
        recorded_at = local_midnight - _USAGE_TEHRAN_OFFSET
        history.append({
            'period_key': key, 'recorded_at': recorded_at.isoformat() + 'Z',
            'delta_upload': item['delta_upload'], 'delta_download': item['delta_download'],
            'delta_total': item['delta_total'], 'remaining': item['remaining'],
            'volume_limit': item['volume_limit'], 'is_cumulative': False,
        })
    history.sort(key=lambda item: item['recorded_at'], reverse=True)
    has_more = len(history) > page_size
    history = history[:page_size]

    end_cursor = None
    if history:
        boundary = (datetime.strptime(history[-1]['period_key'], '%Y-%m').date().replace(day=1)
                    if period == 'month'
                    else datetime.strptime(history[-1]['period_key'], '%Y-%m-%d').date())
        end_cursor = _history_cursor_encode(period, boundary)

    renew_query = RenewalEvent.query.filter_by(server_id=server_id, sub_id=sub_id)
    if history:
        if period == 'month':
            newest_date = datetime.strptime(history[0]['period_key'], '%Y-%m').date().replace(day=1)
            oldest_date = datetime.strptime(history[-1]['period_key'], '%Y-%m').date().replace(day=1)
            upper_date = (newest_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            newest_date = datetime.strptime(history[0]['period_key'], '%Y-%m-%d').date()
            oldest_date = datetime.strptime(history[-1]['period_key'], '%Y-%m-%d').date()
            upper_date = newest_date + timedelta(days=1)
        lower = datetime.combine(oldest_date, datetime.min.time()) - _USAGE_TEHRAN_OFFSET
        upper = datetime.combine(upper_date, datetime.min.time()) - _USAGE_TEHRAN_OFFSET
        renew_query = renew_query.filter(
            RenewalEvent.renewed_at >= lower,
            RenewalEvent.renewed_at < upper,
        )
    renewals = renew_query.order_by(RenewalEvent.renewed_at.desc()).limit(30).all()
    renewal_rows, seen = [], set()
    for row in renewals:
        key = (row.renewed_at.replace(second=0, microsecond=0), row.volume_bytes, row.days,
               row.is_unlimited_volume, row.is_unlimited_time)
        if key in seen:
            continue
        seen.add(key)
        renewal_rows.append({
            'renewed_at': row.renewed_at.isoformat() + 'Z', 'volume_bytes': row.volume_bytes,
            'days': row.days, 'is_unlimited_volume': row.is_unlimited_volume,
            'is_unlimited_time': row.is_unlimited_time,
        })

    state = UsageCounterState.query.filter_by(server_id=server_id, sub_id=sub_id).first()
    return jsonify({
        'success': True, 'period': period, 'history': history, 'renewals': renewal_rows,
        'snapshot_count': len(rows),
        'page_info': {
            'has_more': has_more,
            'end_cursor': end_cursor,
            'page_size': page_size,
        },
        'latest_remaining': state.remaining_bytes if state else None,
        'latest_volume_limit': state.volume_limit_bytes if state else None,
        'latest_recorded_at': (state.observed_at.isoformat() + 'Z') if state else None,
    })


@bp.route('/sub/history/<int:server_id>/<sub_id>')
def sub_usage_history(server_id, sub_id):
    """Return compact daily/monthly usage rollups and renewal events.

    Query params:
      period: day | month
      cursor: opaque cursor returned by page_info.end_cursor
      limit: page size (5..31, default 10)
    """
    normalized_sub_id = str(sub_id or '').strip()
    if not normalized_sub_id or any(c in normalized_sub_id for c in ('/', '\\', '?', '#', '@', ':')):
        return jsonify({'success': False, 'error': 'Invalid subscription ID'}), 400

    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({'success': False, 'error': 'Server not found'}), 404

    period = (request.args.get('period') or 'day').strip().lower()
    if period not in ('day', 'month'):
        period = 'day'

    cursor = (request.args.get('cursor') or '').strip() or None
    page_size = _parse_int(request.args.get('limit'), 10, min_value=5, max_value=31)

    return _subscription_rollup_history(
        server_id, normalized_sub_id, period, cursor=cursor, page_size=page_size,
    )


def _fa_digits(value) -> str:
    """Convert ASCII digits in `value` to Persian digits."""
    return str(value).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))


def _build_status_config_line(state: dict, expiry_info: dict, remaining_bytes, total_limit: int, lang: str = 'fa') -> str | None:
    """Return a single non-routable vmess:// 'status' config.

    Its display name (the vmess `ps` field) summarizes the customer's service
    state, remaining days and remaining volume. It is appended as the LAST entry
    of a subscription so customers can read their status from inside their VPN
    app (each config shows by name in the server list) without ever opening the
    subscription page. It points at 127.0.0.1:1 and never carries traffic.
    Recomputed on every request, so it always reflects the latest status.
    """
    from app import _normalize_ui_lang, app  # deferred: app-level helper, avoids circular import
    try:
        fa = _normalize_ui_lang(lang, default='en') == 'fa'
        key = (state or {}).get('key') or 'active'
        emoji = (state or {}).get('emoji') or ''
        label = (state or {}).get('label') or ('فعال' if fa else 'Active')

        parts = [f"{emoji} {label}".strip()]

        if key in ('expired', 'volume_ended', 'inactive'):
            # Terminal states: the label already says it; add a renew nudge.
            parts.append('لطفا تمدید کنید' if fa else 'Please renew')
        else:
            etype = str((expiry_info or {}).get('type') or '').lower()
            days = (expiry_info or {}).get('days')
            if etype in ('unlimited', 'start_after_use'):
                parts.append('زمان نامحدود' if fa else 'Unlimited time')
            elif isinstance(days, (int, float)) and days > 0:
                d = int(days)
                parts.append(f"{_fa_digits(d)} روز مانده" if fa else f"{d} days left")

            if total_limit and total_limit > 0:
                gb = max(int(remaining_bytes or 0), 0) / (1024 ** 3)
                gb_str = f"{gb:.1f}".rstrip('0').rstrip('.')
                parts.append(f"{_fa_digits(gb_str)} گیگ مانده" if fa else f"{gb_str} GB left")
            else:
                parts.append('حجم نامحدود' if fa else 'Unlimited data')

        # Make clear this entry is informational only — it must not be connected to.
        parts.append('🚫 انتخاب نکنید' if fa else '🚫 Do not select')

        name = ' | '.join(p for p in parts if p)
        vmess_obj = {
            "v": "2", "ps": name, "add": "127.0.0.1", "port": "1",
            "id": "00000000-0000-0000-0000-000000000000", "aid": "0",
            "scy": "auto", "net": "tcp", "type": "none",
            "host": "", "path": "", "tls": "",
        }
        payload = base64.b64encode(json.dumps(vmess_obj, ensure_ascii=False).encode()).decode()
        return f"vmess://{payload}"
    except Exception:
        app.logger.exception("status config build failed")
        return None


@bp.route('/s/<int:server_id>/<sub_id>')
def client_subscription(server_id, sub_id):
    from app import _compute_client_service_state, _get_dashboard_status_thresholds, _get_or_create_system_setting, app, format_bytes, format_remaining_days  # deferred: app-level helper, avoids circular import
    server = db.session.get(Server, server_id)
    if not server:
        return "Subscription not found", 404

    wants_html_view = str(request.args.get('view', '')).strip().lower() in ('1', 'true', 'yes')

    # Normalize subscription identifier early
    normalized_sub_id = str(sub_id).strip()
    
    # SSRF/Path Traversal Protection: Ensure sub_id doesn't contain characters that could manipulate the URL
    if any(c in normalized_sub_id for c in ('/', '\\', '?', '#', '@', ':', '..')):
        app.logger.warning(f"Potential SSRF/Traversal attempt with sub_id: {normalized_sub_id}")
        return "Invalid subscription ID", 400

    # v2rayNG has a hard 15-second HTTP timeout and only needs a Base64 list of
    # parseable URIs. Avoid the slower usage/status fetch and the synthetic
    # status node for this client; one live v3 API read is authoritative.
    request_user_agent = (request.headers.get('User-Agent') or '').lower()
    if 'v2rayng' in request_user_agent:
        fast_session, fast_login_error = get_xui_session(server)
        if not fast_login_error and fast_session:
            fast_configs = fetch_authoritative_subscription_configs(
                server,
                normalized_sub_id,
                session_obj=fast_session,
            )
            if fast_configs:
                fast_configs = sort_subscription_configs(
                    fast_configs,
                    server,
                    sub_id=normalized_sub_id,
                )
                fast_email = find_subscription_client_email(
                    server,
                    normalized_sub_id,
                    session_obj=fast_session,
                )
                fast_configs = ensure_subscription_identity(
                    fast_configs,
                    fast_email,
                )
                profile_metadata = fetch_subscription_profile_metadata(
                    server,
                    session_obj=fast_session,
                )
                fast_blob = '\n'.join(fast_configs)
                encoded_fast_blob = base64.b64encode(
                    fast_blob.encode('utf-8')
                ).decode('ascii')
                profile_title_raw = build_subscription_profile_title(
                    profile_metadata.get('sub_title'),
                    server.name,
                )
                profile_title = base64.b64encode(
                    profile_title_raw.encode('utf-8')
                ).decode('ascii')
                return encoded_fast_blob, 200, {
                    'Content-Type': 'text/plain; charset=utf-8',
                    'Profile-Title': f'base64:{profile_title}',
                    'Profile-Update-Interval': profile_metadata.get('update_interval', '24'),
                    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                    'Pragma': 'no-cache',
                }

    live_session, live_inbounds, target_client, target_inbound, live_error = (
        _fetch_live_subscription_context(server, normalized_sub_id)
    )
    if live_error:
        app.logger.warning(
            f"Live subscription fetch failed for server {server_id}: {live_error}"
        )
        return "Unable to load live subscription", 502

    if not target_client or not target_inbound:
        return "Subscription not found", 404

    client_email = target_client.get('email') or f"user-{normalized_sub_id}"

    def _to_int_or_none(value):
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        try:
            return int(value)
        except Exception:
            try:
                return int(float(str(value).strip()))
            except Exception:
                return None

    up = _to_int_or_none(target_client.get('up'))
    down = _to_int_or_none(target_client.get('down'))

    if up is None or down is None:
        client_stats = target_inbound.get('clientStats') or []
        for stat in client_stats:
            if stat.get('email') == target_client.get('email'):
                up = _to_int_or_none(stat.get('up'))
                down = _to_int_or_none(stat.get('down'))
                break

    up = up or 0
    down = down or 0

    total_used = (up or 0) + (down or 0)
    try:
        total_limit = int(target_client.get('totalGB') or 0)
    except (TypeError, ValueError):
        total_limit = 0
    remaining = max(total_limit - total_used, 0) if total_limit > 0 else None
    percentage_used = round((total_used / total_limit) * 100, 2) if total_limit else 0

    page_lang = (_get_or_create_system_setting('subscription_page_lang', 'en') or 'en').strip().lower()
    if page_lang not in ('fa', 'en'):
        page_lang = 'en'

    expiry_raw_ms = target_client.get('expiryTime', 0)
    expiry_info = format_remaining_days(expiry_raw_ms, lang=page_lang)

    try:
        expiry_ts_norm = int(expiry_raw_ms or 0)
    except Exception:
        expiry_ts_norm = 0

    subscription_state = _compute_client_service_state(
        enabled=bool(target_client.get('enable', True)),
        total_bytes=int(total_limit or 0),
        remaining_bytes=(None if remaining is None else int(remaining)),
        expiry_ts=expiry_ts_norm,
        expiry_info=expiry_info,
        thresholds=_get_dashboard_status_thresholds(),
        lang=page_lang,
    )

    # Prepare fallback headers for client apps (used for both upstream-proxy and manual generation)
    expiry_time_ms = expiry_raw_ms or 0
    expiry_time_sec = int(expiry_time_ms / 1000) if expiry_time_ms and expiry_time_ms > 0 else 0
    
    # Fix: If expire is 0 (unlimited), set to far future to prevent v2rayNG from hanging/looping
    # v2rayNG interprets expire=0 incorrectly, causing subscription loading issues
    if expiry_time_sec == 0:
        import time as _time
        expiry_time_sec = int(_time.time()) + 315360000  # 10 years in the future
    
    user_info_header = f"upload={up}; download={down}; total={total_limit}; expire={expiry_time_sec}"
    profile_metadata = fetch_subscription_profile_metadata(
        server,
        session_obj=live_session,
    )
    _profile_title_raw = build_subscription_profile_title(
        profile_metadata.get('sub_title'),
        server.name,
    )
    _profile_title_b64 = base64.b64encode(_profile_title_raw.encode('utf-8')).decode('utf-8')
    fallback_headers = {
        'Subscription-Userinfo': user_info_header,
        'Profile-Update-Interval': profile_metadata.get('update_interval', '24'),
        'Content-Type': 'text/plain; charset=utf-8',
        'Profile-Title': f"base64:{_profile_title_b64}",
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Pragma': 'no-cache',
    }

    # Prepare User-Agent check
    user_agent = (request.headers.get('User-Agent') or '').lower()
    # Comprehensive list of V2Ray/Xray client user-agents
    agent_tokens = [
        # --- Universal / Cross Platform ---
        'v2ray', 'xray', 'shadowsocks', 'clash', 'sing-box', 'tuic', 'hysteria',
        'hiddify', 'happ', 'karing',

        # --- iOS Clients ---
        'shadowrocket', 'streisand', 'v2box', 'kitsunebi', 'quantumult',
        'surge', 'loon', 'stash', 'fair', 'pepi', 'i2ray', 'foxray', 'potatso',
        'oneclick', 'v2rayu', 'spectre', 'shadowlink',

        # --- Android Clients ---
        'v2rayng', 'sagernet', 'nekobox', 'matsuri', 'bifrostv',
        'igniter', 'anxray', 'surfboard', 'v2raytun', 'mahsa', 'napsternetv', 'npv',
        'invizible', 'karimg',

        # --- Desktop (Windows, Mac, Linux) ---
        'nekoray', 'v2rayn', 'v2raya', 'qv2ray', 'mellow', 'flclash', 'furious',
        'clash-verge', 'clashverge', 'v2rayx', 'musedaq',
    ]
    wants_b64 = request.args.get('format', '').lower() == 'b64'
    accept = (request.headers.get('Accept') or '').lower()
    accept_prefers_html = ('text/html' in accept) or ('application/xhtml+xml' in accept)
    # A request is browser-like only if it BOTH sends text/html in Accept AND has a browser UA.
    # Everything else (unknown UA, no Accept, Accept:*/*, known V2Ray UA) is treated as a client app.
    is_browser_like = accept_prefers_html and ('mozilla' in user_agent)
    is_client_app = (
        wants_b64 or
        any(token in user_agent for token in agent_tokens) or
        (accept and not accept_prefers_html) or
        not is_browser_like
    )

    # v2rayNG is more reliable when the subscription response is base64.
    # Some versions can appear to hang/spin when the server returns plain text.
    if 'v2rayng' in user_agent:
        wants_b64 = True

    configs = build_subscription_configs(
        server,
        normalized_sub_id,
        target_client,
        target_inbound,
        live_session=live_session,
        live_inbounds=live_inbounds,
    )
    configs = ensure_subscription_identity(configs, client_email)

    subscription_entries = [entry for entry in configs if entry]
    # Append the live status config as the last entry (client-app payload only;
    # the HTML view uses `configs`, which we keep clean).
    _status_line = _build_status_config_line(subscription_state, expiry_info, remaining, total_limit, page_lang)
    if _status_line:
        subscription_entries.append(_status_line)
    # Do NOT fall back to sub_url — returning a URL as a "config" causes
    # "Subscription does not contain valid configurations" in every V2Ray client.
    subscription_blob = '\n'.join(subscription_entries)
    encoded_blob = base64.b64encode(subscription_blob.encode('utf-8')).decode('utf-8')

    if is_client_app and not wants_html_view:
        headers = dict(fallback_headers)
        headers['Content-Type'] = 'text/plain; charset=utf-8'
        return encoded_blob, 200, headers

    client_payload = {
        "email": client_email,
        "is_active": target_client.get('enable', True),
        "service_state": subscription_state.get('key', 'active'),
        "service_state_label": subscription_state.get('label', ('فعاله' if page_lang == 'fa' else 'Active')),
        "service_state_emoji": subscription_state.get('emoji', '✅'),
        "service_state_tag": subscription_state.get('tag', 'ok'),
        "total_used": format_bytes(total_used),
        "total_limit": format_bytes(total_limit) if total_limit > 0 else "Unlimited",
        "percentage_used": percentage_used,
        "expiry": expiry_info['text'],
        "expiry_days": expiry_info.get('days', 0),
        "expiry_type": expiry_info.get('type', 'normal'),
        "remaining": format_bytes(remaining) if remaining is not None else None,
        "subscription_url": f"{request.base_url}",
        "configs": configs,
        "server_name": server.name
    }

    apps = SubAppConfig.query.filter_by(is_enabled=True).all()
    apps_payload = [app.to_dict() for app in apps]

    # Get FAQs
    faqs = FAQ.query.filter_by(is_enabled=True).all()
    faqs_payload = [faq.to_dict() for faq in faqs]
    
    # Get support info
    support_telegram = db.session.get(SystemConfig, 'support_telegram')
    support_whatsapp = db.session.get(SystemConfig, 'support_whatsapp')
    support_sms_cfg = db.session.get(SystemConfig, 'support_sms')

    # Get channel links
    channel_telegram = db.session.get(SystemConfig, 'channel_telegram')
    channel_whatsapp = db.session.get(SystemConfig, 'channel_whatsapp')

    def _normalize_url(raw: str, *, default_prefix: str | None = None) -> str:
        val = (raw or '').strip()
        if not val:
            return ''
        if val.startswith('@'):
            val = val[1:].strip()
        if val.startswith('http://') or val.startswith('https://'):
            return val
        if val.startswith('t.me/') or val.startswith('telegram.me/'):
            return f"https://{val}"
        if default_prefix:
            return f"{default_prefix}{val}"
        return f"https://{val}"
    
    support_info = {
        'telegram': support_telegram.value if support_telegram else '',
        'whatsapp': support_whatsapp.value if support_whatsapp else '',
        'sms': (support_sms_cfg.value if support_sms_cfg else '').strip() or '',
    }

    channels_info = {
        'telegram': _normalize_url(channel_telegram.value if channel_telegram else '', default_prefix='https://t.me/'),
        'whatsapp': _normalize_url(channel_whatsapp.value if channel_whatsapp else '')
    }

    # If this client is assigned to a reseller, use reseller-defined support/channels instead of global SystemConfig.
    sub_owner_reseller = None  # the account's reseller owner (if any), used for sub-page packages
    try:
        cu = (target_client.get('id') or '').strip() if isinstance(target_client, dict) else ''
        email_l = (client_email or '').strip().lower()
        ownership = ClientOwnership.query.filter(
            ClientOwnership.server_id == int(server.id),
            or_(
                ClientOwnership.client_uuid == cu,
                func.lower(ClientOwnership.client_email) == email_l,
            )
        ).first()
        reseller = ownership.reseller if ownership else None
        if reseller and getattr(reseller, 'role', None) == 'reseller':
            sub_owner_reseller = reseller
            def _clean_telegram_username(v: str | None) -> str:
                val = (v or '').strip()
                if not val:
                    return ''
                if val.startswith('@'):
                    val = val[1:].strip()
                val = re.sub(r'^(https?://)?(t\.me/|telegram\.me/)', '', val, flags=re.IGNORECASE)
                val = val.strip('/').strip()
                val = re.sub(r'[^0-9a-zA-Z_]', '', val)
                return (val or '')[:100]

            def _clean_whatsapp_number(v: str | None) -> str:
                val = (v or '').strip()
                if not val:
                    return ''
                val = re.sub(r'^(https?://)?wa\.me/', '', val, flags=re.IGNORECASE)
                val = val.strip('/').strip()
                val = re.sub(r'[^0-9+]', '', val)
                return (val or '')[:64]

            support_info = {
                'telegram': _clean_telegram_username(getattr(reseller, 'support_telegram', None)),
                'whatsapp': _clean_whatsapp_number(getattr(reseller, 'support_whatsapp', None)),
                'sms': _clean_whatsapp_number(getattr(reseller, 'support_sms', None)),
            }

            channels_info = {
                'telegram': _normalize_url(getattr(reseller, 'channel_telegram', '') or '', default_prefix='https://t.me/'),
                'whatsapp': _normalize_url(getattr(reseller, 'channel_whatsapp', '') or ''),
            }
    except Exception:
        pass

    # Announcements (server+inbound scoped) for subscription page
    announcements_payload = []
    try:
        def _parse_int_or_none(val):
            if val is None:
                return None
            try:
                return int(val)
            except Exception:
                try:
                    return int(float(str(val).strip()))
                except Exception:
                    return None

        inbound_id = None
        try:
            inbound_id = _parse_int_or_none((target_inbound or {}).get('id'))
            if inbound_id is None:
                inbound_id = _parse_int_or_none((target_inbound or {}).get('inbound_id'))
        except Exception:
            inbound_id = None

        def _normalize_targets(raw_targets, *, fallback_all_servers: bool, fallback_server_ids: list[int]):
            if raw_targets is None:
                if fallback_all_servers:
                    return '*'
                return [{'server_id': int(sid), 'inbounds': '*'} for sid in (fallback_server_ids or [])]

            if raw_targets == '*':
                return '*'

            if isinstance(raw_targets, str):
                trimmed = raw_targets.strip()
                if not trimmed:
                    if fallback_all_servers:
                        return '*'
                    return [{'server_id': int(sid), 'inbounds': '*'} for sid in (fallback_server_ids or [])]
                if trimmed == '*':
                    return '*'
                try:
                    parsed = json.loads(trimmed)
                    return _normalize_targets(parsed, fallback_all_servers=fallback_all_servers, fallback_server_ids=fallback_server_ids)
                except Exception:
                    # Back-compat: comma-separated server ids
                    ids = []
                    for part in trimmed.split(','):
                        try:
                            ids.append(int(part.strip()))
                        except Exception:
                            continue
                    return [{'server_id': int(sid), 'inbounds': '*'} for sid in ids]

            entries = raw_targets if isinstance(raw_targets, list) else [raw_targets]
            merged: dict[int, str | set[int]] = {}
            for item in entries:
                server_id = None
                inbounds: str | list[int] = '*'

                if isinstance(item, (int, float)):
                    server_id = _parse_int_or_none(item)
                    inbounds = '*'
                elif isinstance(item, str):
                    if item.strip() == '*':
                        return '*'
                    server_id = _parse_int_or_none(item)
                    inbounds = '*'
                elif isinstance(item, dict):
                    server_id = _parse_int_or_none(item.get('server_id') or item.get('server') or item.get('id'))
                    raw_inb = item.get('inbounds')
                    if raw_inb == '*' or (isinstance(raw_inb, str) and raw_inb.strip() == '*') or raw_inb is None:
                        inbounds = '*'
                    elif isinstance(raw_inb, list):
                        inbounds = [v for v in (_parse_int_or_none(x) for x in raw_inb) if v is not None]
                    else:
                        one = _parse_int_or_none(raw_inb)
                        inbounds = [] if one is None else [one]

                if not server_id:
                    continue

                if server_id not in merged:
                    merged[server_id] = '*' if inbounds == '*' else set(inbounds)
                else:
                    if merged[server_id] == '*' or inbounds == '*':
                        merged[server_id] = '*'
                    else:
                        for v in inbounds:
                            merged[server_id].add(int(v))

            return [
                {
                    'server_id': sid,
                    'inbounds': '*' if inb == '*' else sorted(list(inb)),
                }
                for sid, inb in merged.items()
            ]

        def _announcement_allows(ann: Announcement, *, server_id: int, inbound_id: int | None) -> bool:
            try:
                rules = _normalize_targets(
                    ann.targets,
                    fallback_all_servers=bool(ann.all_servers),
                    fallback_server_ids=[s.id for s in (ann.servers or [])],
                )
                if rules == '*':
                    return True
                for rule in rules:
                    try:
                        if int(rule.get('server_id')) != int(server_id):
                            continue
                    except Exception:
                        continue

                    inb = rule.get('inbounds')
                    if inb == '*':
                        return True
                    if inbound_id is None:
                        return True
                    if isinstance(inb, list) and any(int(x) == int(inbound_id) for x in inb if x is not None):
                        return True
                return False
            except Exception:
                # Fail closed (do not show announcement) on malformed targeting
                return False

        now_utc = datetime.utcnow()
        q = Announcement.query.filter(Announcement.start_at <= now_utc, Announcement.end_at >= now_utc)
        q = q.order_by(Announcement.created_at.desc())
        active = q.all()
        announcements_payload = [
            a.to_dict() for a in active
            if _announcement_allows(a, server_id=server.id, inbound_id=inbound_id)
            and not (getattr(a, 'hide_from_resellers', False) and sub_owner_reseller is not None)
        ]
    except Exception:
        announcements_payload = []

    # Active Online Chat snippet for subscription page
    active_online_chat_script = ''
    try:
        active_chat = OnlineChatScript.query.filter_by(is_active=True).order_by(OnlineChatScript.id.desc()).first()
        if active_chat and active_chat.script_code:
            nonce = getattr(g, 'csp_nonce', '') or ''
            snippet = (active_chat.script_code or '').strip()
            if nonce and snippet:
                snippet = re.sub(r'<script(?![^>]*\bnonce=)', f'<script nonce="{nonce}"', snippet, flags=re.IGNORECASE)
            active_online_chat_script = snippet
            if active_online_chat_script:
                g.allow_external_chat_widget = True
    except Exception:
        active_online_chat_script = ''

    backup_configs_payload = []
    try:
        from sqlalchemy import or_ as _or
        _bcs = BackupConfig.query.filter(
            BackupConfig.is_enabled == True,
            _or(BackupConfig.server_id == server.id, BackupConfig.server_id == None)
        ).order_by(BackupConfig.sort_order, BackupConfig.id).all()
        backup_configs_payload = [bc.to_dict() for bc in _bcs]
    except Exception:
        backup_configs_payload = []

    # Renewal packages to show on the sub page, based on the account owner.
    sub_packages_payload = _build_sub_page_packages(sub_owner_reseller)
    renewal_recommendation = _build_subscription_package_recommendation(
        server.id,
        normalized_sub_id,
        sub_packages_payload,
        terminal=subscription_state.get('tag') in ('expired', 'ended'),
        live_usage={
            'total_bytes': total_used,
            'volume_limit_bytes': total_limit,
            'expiry_ts_ms': expiry_ts_norm,
            'observed_at': datetime.utcnow(),
        },
    )
    if renewal_recommendation:
        recommended_id = renewal_recommendation.get('package_id')
        comfort_id = renewal_recommendation.get('comfort_package_id')
        for package in sub_packages_payload:
            package['is_personalized'] = package.get('id') == recommended_id
            package['is_comfort'] = bool(comfort_id and package.get('id') == comfort_id)

    _sub_html = render_template(
        'subscription.html',
        client=client_payload,
        apps=apps_payload,
        faqs=faqs_payload,
        support=support_info,
        channels=channels_info,
        announcements=announcements_payload,
        active_online_chat_script=active_online_chat_script,
        backup_configs=backup_configs_payload,
        sub_packages=sub_packages_payload,
        renewal_recommendation=renewal_recommendation,
        page_lang=page_lang,
        server_id=server_id,
        sub_id=normalized_sub_id,
    )
    # The subscription page must be LIVE (status/usage/expiry) — never let a
    # browser or proxy serve a stale cached copy.
    _resp = make_response(_sub_html)
    _resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    _resp.headers['Pragma'] = 'no-cache'
    _resp.headers['Expires'] = '0'
    return _resp
