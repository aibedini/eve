"""Usage snapshot / traffic rollup and settings-overview API routes (extracted from app.py)."""
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, session
from sqlalchemy import func

from panel.extensions import db
from panel.models import (
    Admin, ClientOwnership, Server, UsageCounterState, UsageDaily, UsageHourly,
)
from panel.routes.common import login_required, user_management_required

bp = Blueprint('usage', __name__)


@bp.route('/api/settings/overview', methods=['GET'])
@login_required
def get_settings_overview():
    from app import (  # deferred: app-level helper, avoids circular import
        APP_START_TS, APP_VERSION, UPDATE_CACHE, _autodetect_ssl_paths,
        _get_system_setting_value, _refresh_update_cache_async, app,
    )
    result = {}

    # Uptime
    result['uptime_seconds'] = int(time.time() - APP_START_TS)

    # Last auto backup
    result['last_backup'] = _get_system_setting_value('last_auto_backup', '') or ''

    # Last Telegram backup
    result['last_telegram_backup'] = _get_system_setting_value('telegram_backup_last_run', '') or ''

    # Database type
    db_url_cfg = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if db_url_cfg.startswith('postgresql'):
        result['db_type'] = 'PostgreSQL'
        try:
            parsed = urlparse(db_url_cfg)
            result['db_info'] = f"{parsed.hostname}/{(parsed.path or '').lstrip('/')}"
        except Exception:
            result['db_info'] = 'PostgreSQL'
    else:
        result['db_type'] = 'SQLite'
        result['db_info'] = 'Local SQLite'

    # Versions
    result['current_version'] = APP_VERSION
    result['latest_version'] = None
    result['update_available'] = False
    result['is_beta'] = False
    result['release_url'] = ''
    try:
        if UPDATE_CACHE.get('data'):
            result['latest_version'] = UPDATE_CACHE['data'].get('latest_version')
            result['update_available'] = bool(UPDATE_CACHE['data'].get('update_available'))
            result['is_beta'] = bool(UPDATE_CACHE['data'].get('is_beta'))
            result['release_url'] = UPDATE_CACHE['data'].get('release_url', '')
        else:
            # Cache empty (e.g. fresh restart) — refresh in background, don't block
            # the page on a GitHub round-trip. Version badge shows "Unknown" until
            # the next load picks up the populated cache.
            _refresh_update_cache_async()
    except Exception:
        pass

    # SSL info
    cert_path = _get_system_setting_value('ssl_cert_path', '') or ''
    if not cert_path:
        cert_path, _ = _autodetect_ssl_paths()
    ssl_type = 'none'
    ssl_expiry = None
    ssl_issuer = None

    if cert_path and os.path.isfile(cert_path) and os.access(cert_path, os.R_OK):
        # Provisional from path; refined below using the parsed cert.
        if '/etc/letsencrypt/' in cert_path:
            ssl_type = 'letsencrypt'
        elif '/etc/ssl/eve-manager/' in cert_path:
            ssl_type = 'self_signed'
        else:
            ssl_type = 'custom'
        try:
            from cryptography import x509 as _x509
            from cryptography.hazmat.backends import default_backend as _default_backend
            from cryptography.x509.oid import NameOID as _NameOID
            with open(cert_path, 'rb') as f:
                cert = _x509.load_pem_x509_certificate(f.read(), _default_backend())
            # not_valid_after_utc is preferred in newer cryptography; fall back to not_valid_after
            expiry_dt = getattr(cert, 'not_valid_after_utc', None) or cert.not_valid_after
            ssl_expiry = expiry_dt.isoformat()
            try:
                ssl_issuer = cert.issuer.get_attributes_for_oid(_NameOID.COMMON_NAME)[0].value
            except Exception:
                ssl_issuer = None
            # Classify by the cert, not the path (LE certs are copied into the
            # eve-manager dir): self-signed iff issuer DN == subject DN.
            if cert.issuer == cert.subject:
                ssl_type = 'self_signed'
            else:
                _issuer_org = ''
                try:
                    _issuer_org = (cert.issuer.get_attributes_for_oid(_NameOID.ORGANIZATION_NAME)[0].value or '')
                except Exception:
                    _issuer_org = ''
                if '/etc/letsencrypt/' in cert_path or "let's encrypt" in _issuer_org.lower():
                    ssl_type = 'letsencrypt'
                else:
                    ssl_type = 'custom'
        except Exception as exc:
            app.logger.debug(f"SSL cert parse error: {exc}")

    result['ssl_type'] = ssl_type
    result['ssl_expiry'] = ssl_expiry
    result['ssl_issuer'] = ssl_issuer

    # Last compact usage rollup
    try:
        last_at = db.session.query(func.max(UsageCounterState.observed_at)).scalar()
        result['last_snapshot_at'] = (last_at.isoformat() + 'Z') if last_at else None
        result['total_snapshots'] = UsageDaily.query.count() + UsageHourly.query.count()
    except Exception:
        result['last_snapshot_at'] = None
        result['total_snapshots'] = 0

    return jsonify({'success': True, **result})


@bp.route('/api/usage-snapshot/trigger', methods=['POST'])
@user_management_required
def trigger_usage_snapshot():
    """Queue a usage snapshot in the dedicated background process."""
    from app import (  # deferred: app-level helper, avoids circular import
        _read_snap_progress, _set_snap_progress, enqueue_refresh_job,
    )
    current = _read_snap_progress()
    if current.get('status') == 'running':
        return jsonify({'success': False, 'error': 'A snapshot task is already running.'}), 409
    _set_snap_progress({
        'status': 'running',
        'step': 0, 'total': 0,
        'current_server': '',
        'message': '',
        'inbound_count': 0,
        'fetched_fresh': False,
        'error': None,
    })
    job = enqueue_refresh_job(mode='usage_snapshot', force=True)
    return jsonify({'success': True, 'status': 'started', 'job_id': job.get('id')})


@bp.route('/api/usage-snapshot/progress', methods=['GET'])
@user_management_required
def snapshot_progress():
    """Return current progress of the running/last snapshot task (cross-worker via shared file)."""
    from app import _read_snap_progress  # deferred: app-level helper, avoids circular import
    return jsonify(_read_snap_progress())


def _traffic_check_from_rollups(period, from_dt, to_dt, sub_email):
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, _usage_tehran_date, format_bytes,
        get_reseller_access_maps,
    )
    role = session.get('role', 'admin')
    is_superadmin = session.get('is_superadmin', False)
    admin_id = session.get('admin_id')
    allowed_server_ids = None
    allowed_sub_ids = None

    if not is_superadmin and role == 'reseller':
        user = db.session.get(Admin, admin_id)
        if not user:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        allowed_map, assignments = get_reseller_access_maps(user)
        if allowed_map != '*':
            allowed_server_ids = set()
            for raw in list(allowed_map.keys()) + list(assignments.keys()):
                try:
                    allowed_server_ids.add(int(raw))
                except (TypeError, ValueError):
                    pass
        ownerships = ClientOwnership.query.filter_by(reseller_id=admin_id).all()
        owned_emails = {(o.client_email or '').lower() for o in ownerships if o.client_email}
        owned_uuids = {(o.client_uuid or '').lower() for o in ownerships if o.client_uuid}
        allowed_sub_ids = set()
        for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
            sid = inbound.get('server_id')
            if allowed_server_ids is not None and sid not in allowed_server_ids:
                continue
            for client in inbound.get('clients') or []:
                if ((client.get('email') or '').lower() in owned_emails
                        or (client.get('id') or '').lower() in owned_uuids):
                    sub_id = str(client.get('subId') or client.get('id') or '').strip()
                    if sub_id:
                        allowed_sub_ids.add(sub_id)

    live = {}
    inbound_info = {}
    email_matches = set()
    resolved_email = None
    for inbound in GLOBAL_SERVER_DATA.get('inbounds') or []:
        try:
            server_id = int(inbound.get('server_id'))
        except (TypeError, ValueError):
            continue
        tag = (inbound.get('remark') or '').strip()
        inbound_info[(server_id, tag)] = {
            'port': str(inbound.get('port') or ''), 'id': str(inbound.get('id') or '')
        }
        for client in inbound.get('clients') or []:
            sub_id = str(client.get('subId') or client.get('id') or '').strip()
            if not sub_id:
                continue
            email = (client.get('email') or '').strip()
            remaining = client.get('remaining_bytes', -1)
            live[(server_id, sub_id)] = {
                'email': email, 'tag': tag,
                'remaining': int(remaining) if remaining is not None and remaining >= 0 else None,
                'status': 'disabled' if not client.get('enable', True) else None,
            }
            if sub_email and email.lower() == sub_email:
                email_matches.add(sub_id)
                resolved_email = email

    if sub_email and not email_matches:
        return jsonify({'success': True, 'servers': [], 'period': period,
                        'from': from_dt.isoformat() + 'Z', 'to': to_dt.isoformat() + 'Z',
                        'message': f'No client found with email: {sub_email}'})

    from_date = _usage_tehran_date(from_dt)
    # Range endpoints such as "yesterday" end exactly at Tehran midnight and
    # must not pull the next day's bucket.
    to_date = _usage_tehran_date(to_dt - timedelta(microseconds=1))
    q = UsageDaily.query.filter(UsageDaily.usage_date >= from_date,
                                UsageDaily.usage_date <= to_date)
    if allowed_server_ids is not None:
        q = q.filter(UsageDaily.server_id.in_(list(allowed_server_ids)))
    if sub_email:
        q = q.filter(UsageDaily.sub_id.in_(list(email_matches)))
    elif allowed_sub_ids is not None:
        q = q.filter(UsageDaily.sub_id.in_(list(allowed_sub_ids)))
    rows = q.all()

    server_names = {s.id: s.name for s in Server.query.all()
                    if allowed_server_ids is None or s.id in allowed_server_ids}
    result = {}
    first_seen = {}
    for row in rows:
        current = live.get((row.server_id, row.sub_id), {})
        tag = row.inbound_tag or current.get('tag') or '(unknown)'
        group = result.setdefault(row.server_id, {}).setdefault(tag, {
            'download': 0, 'upload': 0, '_clients': {}
        })
        up, down = int(row.upload_bytes or 0), int(row.download_bytes or 0)
        group['upload'] += up
        group['download'] += down
        client = group['_clients'].setdefault(row.sub_id, {
            'email': current.get('email') or (row.sub_id[:12] + '…'),
            'download': 0, 'upload': 0, 'total': 0,
            'remaining': current.get('remaining'),
            'status': current.get('status') if current else 'deleted',
        })
        client['upload'] += up
        client['download'] += down
        client['total'] += up + down
        seen = first_seen.get(row.server_id)
        if seen is None or row.first_observed_at < seen:
            first_seen[row.server_id] = row.first_observed_at

    servers_out = []
    for server_id, groups in result.items():
        inbounds = []
        for tag, values in groups.items():
            clients = sorted(values['_clients'].values(), key=lambda item: -item['total'])
            remaining_raw = sum(int(item['remaining'] or 0) for item in clients)
            info = inbound_info.get((server_id, tag), {})
            total = values['upload'] + values['download']
            inbounds.append({
                'inbound_tag': tag, 'port': info.get('port', ''), 'inbound_id': info.get('id', ''),
                'download': values['download'], 'upload': values['upload'], 'total': total,
                'clients': len(clients), 'remaining_raw': remaining_raw,
                'remaining': format_bytes(remaining_raw) if remaining_raw > 0 else None,
                'client_list': clients,
            })
        inbounds.sort(key=lambda item: -item['total'])
        remaining_raw = sum(item['remaining_raw'] for item in inbounds)
        first = first_seen.get(server_id)
        servers_out.append({
            'server_id': server_id, 'server_name': server_names.get(server_id, f'Server {server_id}'),
            'total': sum(item['total'] for item in inbounds),
            'download': sum(item['download'] for item in inbounds),
            'upload': sum(item['upload'] for item in inbounds),
            'remaining_raw': remaining_raw,
            'remaining': format_bytes(remaining_raw) if remaining_raw > 0 else None,
            'inbounds': inbounds,
            'first_snapshot_at': (first.isoformat() + 'Z') if first else None,
            'effective_from': from_dt.isoformat() + 'Z',
        })
    servers_out.sort(key=lambda item: -item['total'])
    return jsonify({
        'success': True, 'period': period, 'from': from_dt.isoformat() + 'Z',
        'to': to_dt.isoformat() + 'Z', 'servers': servers_out,
        'sub_email': sub_email or None, 'email_resolved_name': resolved_email,
    })


@bp.route('/api/traffic-check', methods=['GET'])
@login_required
def traffic_check():
    """Aggregate compact daily traffic rollups by server and inbound.
    Optional ?sub_email=x filters to a single client's usage."""
    _TEHRAN_OFFSET = timedelta(hours=3, minutes=30)

    period = (request.args.get('period') or 'today').strip().lower()
    from_ts = request.args.get('from_ts')
    to_ts = request.args.get('to_ts')
    sub_email = (request.args.get('sub_email') or '').strip().lower()

    now_utc = datetime.utcnow()
    now_teh = now_utc + _TEHRAN_OFFSET

    if period == 'custom' and from_ts and to_ts:
        try:
            from_dt = datetime.utcfromtimestamp(int(from_ts) / 1000)
            to_dt = datetime.utcfromtimestamp(int(to_ts) / 1000)
        except Exception:
            return jsonify({'success': False, 'error': 'Invalid timestamps'}), 400
    elif period == 'today':
        teh_midnight = now_teh.replace(hour=0, minute=0, second=0, microsecond=0)
        from_dt = teh_midnight - _TEHRAN_OFFSET
        to_dt = now_utc
    elif period == 'yesterday':
        teh_midnight = now_teh.replace(hour=0, minute=0, second=0, microsecond=0)
        from_dt = (teh_midnight - timedelta(days=1)) - _TEHRAN_OFFSET
        to_dt = teh_midnight - _TEHRAN_OFFSET
    elif period == '7d':
        from_dt = now_utc - timedelta(days=7)
        to_dt = now_utc
    elif period == '30d':
        from_dt = now_utc - timedelta(days=30)
        to_dt = now_utc
    else:
        from_dt = now_teh.replace(hour=0, minute=0, second=0, microsecond=0) - _TEHRAN_OFFSET
        to_dt = now_utc

    return _traffic_check_from_rollups(period, from_dt, to_dt, sub_email)
