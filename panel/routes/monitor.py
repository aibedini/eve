"""Monitor settings/alerts API routes (extracted from app.py)."""
import json
import re
from collections import defaultdict
from datetime import datetime

from flask import Blueprint, jsonify, request, session
from sqlalchemy import text

from panel.extensions import db
from panel.models import Admin, ClientOwnership, MonitorMessageLog
from panel.routes.common import login_required

bp = Blueprint('monitor', __name__)


@bp.route('/api/monitor/settings', methods=['GET'])
@login_required
def get_monitor_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        _get_app_timezone_name, _get_monitor_settings,
        _get_standard_timezone_options,
    )
    return jsonify({
        'success': True,
        'settings': _get_monitor_settings(),
        'timezone': _get_app_timezone_name(),
        'timezone_options': _get_standard_timezone_options(),
    })


@bp.route('/api/monitor/settings', methods=['POST'])
@login_required
def save_monitor_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        GENERAL_TIMEZONE_SETTING_KEY, MONITOR_SETTINGS_KEY,
        _get_app_timezone_name, _get_standard_timezone_options,
        _is_valid_timezone_name, _normalize_monitor_settings,
        _set_system_setting_value, app,
    )
    try:
        payload = request.get_json() or {}
    except Exception:
        payload = {}

    timezone_name = (payload.get('timezone') or '').strip()
    if timezone_name and not _is_valid_timezone_name(timezone_name):
        return jsonify({'success': False, 'error': 'Invalid timezone. Example: Asia/Tehran'}), 400

    normalized = _normalize_monitor_settings(payload)
    try:
        if timezone_name:
            _set_system_setting_value(GENERAL_TIMEZONE_SETTING_KEY, timezone_name)
        _set_system_setting_value(
            MONITOR_SETTINGS_KEY,
            json.dumps(normalized, ensure_ascii=False)
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.error('save_monitor_settings: DB commit failed: %s', exc)
        return jsonify({'success': False, 'error': f'Database error: {exc}'}), 500
    return jsonify({
        'success': True,
        'settings': normalized,
        'timezone': _get_app_timezone_name(),
        'timezone_options': _get_standard_timezone_options(),
    })


@bp.route('/api/monitor/alerts', methods=['GET'])
@login_required
def get_monitor_alerts():
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, _get_app_timezone_name, _get_monitor_settings,
        format_jalali, format_remaining_days, get_reseller_access_maps,
        is_inbound_accessible,
    )
    user = db.session.get(Admin, session['admin_id'])
    settings = _get_monitor_settings()
    filters = settings.get('filters', {})

    now_utc = datetime.utcnow()

    warning_days = int(filters.get('warning_days', 3) or 3)
    warning_gb = float(filters.get('warning_gb', 2.0) or 2.0)
    hide_days = int(filters.get('hide_days', 7) or 7)
    show_unlimited_volume = bool(filters.get('show_unlimited_volume', True))
    show_unlimited_time = bool(filters.get('show_unlimited_time', True))
    show_fully_unlimited = bool(filters.get('show_fully_unlimited', False))
    debug = bool(filters.get('debug'))

    inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []

    allowed_map, assignments = ('*', {})
    owned_emails_by_server = defaultdict(set)
    reseller_owned_email_pairs = set()
    reseller_owned_uuid_pairs = set()
    if user and user.role == 'reseller':
        allowed_map, assignments = get_reseller_access_maps(user)
        ownerships = ClientOwnership.query.filter_by(reseller_id=user.id).all()
        for own in ownerships:
            try:
                sid = int(own.server_id)
            except Exception:
                continue
            email_l = (own.client_email or '').strip().lower()
            if email_l:
                owned_emails_by_server[sid].add(email_l)
    elif user:
        server_ids = set()
        for inbound in inbounds:
            try:
                sid = int(inbound.get('server_id'))
            except Exception:
                continue
            server_ids.add(sid)

        if server_ids:
            ownerships = ClientOwnership.query.filter(ClientOwnership.server_id.in_(list(server_ids))).all()
            for own in ownerships:
                try:
                    sid = int(own.server_id)
                except Exception:
                    continue

                email_l = (own.client_email or '').strip().lower()
                if email_l:
                    reseller_owned_email_pairs.add((sid, email_l))

                client_uuid_l = str(own.client_uuid or '').strip().lower()
                if client_uuid_l:
                    reseller_owned_uuid_pairs.add((sid, client_uuid_l))

    status_labels = {
        'ended': 'Ended',
        'expired': 'Expired',
        'low': 'Low data',
        'soon': 'Expiring soon',
        'disabled': 'Disabled (manual)',
        'ok': 'OK'
    }
    status_order = {
        'ended': 0,
        'expired': 1,
        'low': 2,
        'soon': 3,
        'disabled': 4,
        'ok': 5
    }

    # Pre-load message send counts per (server_id, email, channel) from the log table.
    try:
        msg_rows = db.session.execute(
            text('SELECT server_id, email, channel, COUNT(*) as cnt FROM monitor_message_log GROUP BY server_id, email, channel')
        ).fetchall()
        msg_counts = {}
        for r in msg_rows:
            key = (int(r[0]), str(r[1]).lower())
            if key not in msg_counts:
                msg_counts[key] = {}
            msg_counts[key][str(r[2])] = int(r[3])
    except Exception:
        msg_counts = {}

    alerts = []
    # A v3 client assigned to several inbounds appears once per inbound in the
    # snapshot. Those copies are the same account (same UUID), so collapse them to
    # one alert card — keyed by (server_id, UUID), falling back to (server_id,
    # email) for protocols without a UUID (e.g. Shadowsocks).
    seen_clients = set()

    for inbound in inbounds:
        sid = inbound.get('server_id')
        inbound_id = inbound.get('id')

        if sid is None:
            continue

        if user and user.role == 'reseller':
            if not is_inbound_accessible(sid, inbound_id, allowed_map, assignments):
                continue

        for client in (inbound.get('clients') or []):
            email = (client.get('email') or '').strip()
            email_l = email.lower()
            client_uuid_l = str(client.get('id') or '').strip().lower()

            try:
                sid_norm = int(sid)
            except Exception:
                sid_norm = None

            is_reseller_owned = False
            if sid_norm is not None:
                if (sid_norm, email_l) in reseller_owned_email_pairs:
                    is_reseller_owned = True
                elif client_uuid_l and (sid_norm, client_uuid_l) in reseller_owned_uuid_pairs:
                    is_reseller_owned = True

            if user and user.role == 'reseller':
                if not owned_emails_by_server:
                    continue
                if sid_norm is None:
                    continue
                if email_l not in owned_emails_by_server.get(sid_norm, set()):
                    continue

            # One card per user per server: same email = same user regardless of
            # inbound count or UUID (catches both v3 multi-inbound and same email
            # across different inbounds with distinct UUIDs).
            if email_l:
                dedupe_key = ('e', sid_norm, email_l)
            elif client_uuid_l:
                dedupe_key = ('u', sid_norm, client_uuid_l)
            else:
                dedupe_key = None
            if dedupe_key is not None and dedupe_key in seen_clients:
                continue
            if dedupe_key is not None:
                seen_clients.add(dedupe_key)

            enabled = bool(client.get('enable', True))
            comment = (client.get('comment') or '').strip()
            comment_l = comment.lower()
            no_sms = bool(re.search(r'#\s*nosms', comment_l))
            no_pm = bool(re.search(r'#\s*nopm', comment_l))
            contact_opted_out = no_sms or no_pm
            # IMPORTANT: We do NOT skip disabled clients here. Sanaei-style panels
            # auto-disable a client the instant its time or traffic runs out, so
            # filtering by the enable flag alone would hide exactly the users we
            # most need to follow up with. Instead, every client is categorized by
            # the REAL reason below (ended / expired / manual-disable).

            total_bytes = int(client.get('totalGB') or 0)
            try:
                used_bytes = int(client.get('up') or 0) + int(client.get('down') or 0)
            except Exception:
                used_bytes = 0
            zero_usage = used_bytes < (1 * 1024 * 1024)  # < 1 MB means never connected

            remaining_bytes = client.get('remaining_bytes')
            if remaining_bytes is None or remaining_bytes == -1:
                if total_bytes > 0:
                    remaining_bytes = max(total_bytes - used_bytes, 0)
                else:
                    remaining_bytes = None

            remaining_gb = None
            if remaining_bytes is not None:
                try:
                    remaining_gb = float(remaining_bytes) / (1024 ** 3)
                except Exception:
                    remaining_gb = None

            expiry_ts = int(client.get('expiryTimestamp') or 0)
            expiry_info = format_remaining_days(expiry_ts)

            volume_unlimited = total_bytes <= 0
            time_unlimited = expiry_ts <= 0

            status = None
            status_rank = -1

            # Traffic-based reason (applies whether or not the panel already disabled it)
            if total_bytes > 0 and remaining_bytes is not None:
                if remaining_bytes <= 0:
                    status = 'ended'
                    status_rank = 3
                elif remaining_gb is not None and remaining_gb < warning_gb:
                    status = 'low'
                    status_rank = 2

            # Time-based reason — time takes priority over volume.
            # expired (rank 4) always beats ended (rank 3) and low (rank 2).
            if expiry_ts and expiry_info.get('type') == 'expired':
                if status_rank < 4:
                    status = 'expired'
                    status_rank = 4
            elif expiry_ts and expiry_info.get('type') in ('today', 'soon'):
                if int(expiry_info.get('days') or 0) <= warning_days and status_rank < 1:
                    status = 'soon'
                    status_rank = 1

            # 'disabled' only applies when time AND volume are both still fine —
            # meaning the operator explicitly shut the client off. Auto-disabled
            # accounts (panel killed them because time or traffic ran out) are
            # already captured as 'expired' or 'ended' above, and those labels
            # must survive even when enable=False.
            if not enabled and status in (None, 'low', 'soon'):
                status = 'disabled'
                status_rank = 0

            # Unlimited visibility is independent from classification. Keep the
            # real reason above so a panel auto-disable never becomes a misleading
            # manual-disable label, then apply the operator's display policy.
            if not contact_opted_out:
                if volume_unlimited and time_unlimited and not show_fully_unlimited:
                    continue
                if volume_unlimited and status in ('expired', 'soon') and not show_unlimited_volume:
                    continue
                if time_unlimited and status in ('ended', 'low') and not show_unlimited_time:
                    continue

            # Hide long-expired garbage (date-based, independent of enable flag).
            if expiry_info.get('type') == 'expired' and not debug and not contact_opted_out:
                try:
                    days_ago = abs(int(expiry_info.get('days') or 0))
                except Exception:
                    days_ago = 0
                if hide_days and days_ago > hide_days:
                    continue

            if volume_unlimited and time_unlimited and show_fully_unlimited and not status:
                status = 'ok'

            # Opted-out accounts must remain discoverable even while otherwise healthy.
            if not status and contact_opted_out:
                status = 'ok'

            if not status and not debug:
                continue

            if not status:
                status = 'ok'

            expiry_date = None
            if expiry_ts and expiry_ts > 0:
                try:
                    expiry_dt = datetime.utcfromtimestamp(expiry_ts / 1000)
                    expiry_date = format_jalali(expiry_dt)
                except Exception:
                    expiry_date = None

            sub_id_value = client.get('subId') or client.get('id') or ''
            alerts.append({
                'server_id': sid,
                'server_name': inbound.get('server_name'),
                'inbound_id': inbound_id,
                'email': email,
                'comment': comment,
                'no_sms': no_sms,
                'no_pm': no_pm,
                'contact_opted_out': contact_opted_out,
                'volume_unlimited': volume_unlimited,
                'time_unlimited': time_unlimited,
                'status': status,
                'status_label': status_labels.get(status, status),
                'remaining': client.get('remaining_formatted') or 'Unlimited',
                'time_left': expiry_info.get('text'),
                'expiry_date': expiry_date,
                'enabled': enabled,
                'is_reseller_owned': is_reseller_owned,
                'zero_usage': zero_usage,
                'sms_count': msg_counts.get((sid_norm or 0, email_l), {}).get('sms', 0),
                'wa_count': msg_counts.get((sid_norm or 0, email_l), {}).get('whatsapp', 0),
                'sub_url': client.get('sub_url') or '',
                'dash_sub_url': client.get('dash_sub_url') or '',
                'sub_id': sub_id_value,
            })

    alerts.sort(key=lambda row: (
        status_order.get(row.get('status') or 'ok', 9),
        str(row.get('server_name') or ''),
        str(row.get('email') or '')
    ))

    return jsonify({
        'success': True,
        'settings': settings,
        'timezone': _get_app_timezone_name(),
        'generated_at': now_utc.isoformat(),
        'generated_at_jalali': format_jalali(now_utc),
        'alerts': alerts
    })


@bp.route('/api/monitor/log_message', methods=['POST'])
@login_required
def monitor_log_message():
    """Record that a monitor message was sent to a client."""
    data = request.get_json(silent=True) or {}
    email = str(data.get('email') or '').strip().lower()
    try:
        server_id = int(data.get('server_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'server_id required'}), 400
    channel = str(data.get('channel') or 'sms').strip().lower()
    if not email:
        return jsonify({'success': False, 'error': 'email required'}), 400
    try:
        log = MonitorMessageLog(
            email=email,
            server_id=server_id,
            channel=channel,
            sent_by=session.get('admin_id'),
        )
        db.session.add(log)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500


@bp.route('/api/monitor/reset_msg_count', methods=['POST'])
@login_required
def monitor_reset_msg_count():
    """Clear the message send log for a client (called after renewal)."""
    from app import _clear_message_cooldown  # deferred: app-level helper, avoids circular import
    data = request.get_json(silent=True) or {}
    email = str(data.get('email') or '').strip().lower()
    try:
        server_id = int(data.get('server_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'server_id required'}), 400
    if not email:
        return jsonify({'success': False, 'error': 'email required'}), 400
    try:
        _clear_message_cooldown(email, server_id)
        return jsonify({'success': True})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500


@bp.route('/api/monitor/refresh', methods=['POST'])
@login_required
def trigger_monitor_refresh():
    from app import enqueue_refresh_job  # deferred: app-level helper, avoids circular import
    # Force refresh of all servers (global mode)
    job = enqueue_refresh_job(mode='full', force=True)
    return jsonify({'success': True, 'job_id': job['id']})


@bp.route('/api/monitor/job/<job_id>', methods=['GET'])
@login_required
def get_monitor_job_status(job_id):
    from app import _get_refresh_job  # deferred: app-level helper, avoids circular import
    job = _get_refresh_job(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Job not found'})
    return jsonify({'success': True, 'job': job})
