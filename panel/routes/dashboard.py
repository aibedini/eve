"""Dashboard data API routes (extracted from app.py)."""
import copy
import os
import time
from datetime import datetime

from flask import Blueprint, jsonify, make_response, request, session

from panel.extensions import db, limiter
from panel.models import Admin, ClientOwnership, Server
from panel.routes.common import login_required

bp = Blueprint('dashboard', __name__)


@bp.route('/api/refresh')
@login_required
def api_refresh():
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, _ensure_snapshot_enriched, _get_refresh_job,
        _parse_bool, _summarize_job, enqueue_refresh_job,
        ensure_background_threads_started, format_bytes,
        get_reseller_access_maps, is_inbound_accessible,
    )
    # Make sure background threads are running (covers gunicorn/uwsgi workers)
    if not os.environ.get('DISABLE_BACKGROUND_THREADS'):
        ensure_background_threads_started()

    force = _parse_bool(request.args.get('force'))
    wait = _parse_bool(request.args.get('wait'))
    server_id = request.args.get('server_id')
    mode = (request.args.get('mode') or 'cache').strip().lower()
    enqueue = request.args.get('enqueue')
    enqueue = _parse_bool(enqueue) if enqueue is not None else (mode in ('full', 'status'))
    wait_timeout = 2.0

    job = None
    if enqueue and mode in ('full', 'status'):
        job = enqueue_refresh_job(mode=mode, server_id=server_id, force=force)

    if wait and job:
        start = time.time()
        while (time.time() - start) < wait_timeout:
            cur = _get_refresh_job(job.get('id'))
            if cur and cur.get('state') in ('done', 'error'):
                job = cur
                break
            time.sleep(0.15)

    debug_timing = _parse_bool(request.args.get('debug_timing'))
    t0 = time.perf_counter() if debug_timing else None

    never_fetched = not GLOBAL_SERVER_DATA.get('last_update')

    # Kick off a refresh job only when cache has never been populated (app start / first load)
    if not GLOBAL_SERVER_DATA.get('inbounds') and never_fetched and not GLOBAL_SERVER_DATA.get('is_updating') and not job:
        job = enqueue_refresh_job(mode='full', server_id=server_id, force=False)

    # Return early with skeleton response only when data was never fetched yet.
    # If last_update is set but inbounds is empty (server has 0 inbounds), fall through
    # so the full (empty) payload is returned and the UI can clear its skeleton.
    if not GLOBAL_SERVER_DATA.get('inbounds') and never_fetched:
        return jsonify({
            "success": True,
            "inbounds": [],
            "stats": {"total_inbounds": 0, "active_inbounds": 0, "total_clients": 0,
                      "online_clients": 0, "active_clients": 0, "inactive_clients": 0, "not_started_clients": 0, "unlimited_expiry_clients": 0, "unlimited_volume_clients": 0, "total_traffic": "0 B",
                      "total_upload": "0 B", "total_download": "0 B"},
            "servers": (GLOBAL_SERVER_DATA.get('servers_status') or []),
            "server_count": len(GLOBAL_SERVER_DATA.get('servers_status') or []),
            "is_updating": bool(GLOBAL_SERVER_DATA.get('is_updating')),
            "refresh_job": _summarize_job(job)
        }), (202 if job and job.get('state') in ('queued', 'running') else 200)

    user = db.session.get(Admin, session['admin_id'])

    # === حالت سوپرادمین (یا ادمین معمولی غیر ریسلر) ===
    if user.role != 'reseller':
        # Enrich the shared snapshot once per version, then serve the shared lists
        # directly — no per-request deepcopy (that copy was the dominant latency at
        # scale and made the skeleton linger). Read-only response, additive fields.
        _ensure_snapshot_enriched()

        resp = {
            "success": True,
            "inbounds": GLOBAL_SERVER_DATA.get('inbounds') or [],
            "stats": GLOBAL_SERVER_DATA.get('stats') or {},
            "servers": GLOBAL_SERVER_DATA.get('servers_status') or [],
            "server_count": len(GLOBAL_SERVER_DATA.get('servers_status') or []),
            "last_update": GLOBAL_SERVER_DATA.get('last_update'),
            "is_updating": bool(GLOBAL_SERVER_DATA.get('is_updating')),
            "refresh_job": _summarize_job(job),
        }
        if debug_timing and t0 is not None:
            resp['timing_ms'] = {
                'total': round((time.perf_counter() - t0) * 1000.0, 2),
            }
        return jsonify(resp), (202 if job and job.get('state') in ('queued', 'running') else 200)

    # === حالت ریسلر ===
    # The reseller path filters/annotates per-user, so it works on a private copy.
    data = copy.deepcopy(GLOBAL_SERVER_DATA)

    # 1. دریافت دسترسی‌های سرور و اینباند
    allowed_map, assignments = get_reseller_access_maps(user)
    
    # 2. دریافت لیست کلاینت‌های Assign شده به این ریسلر از دیتابیس
    owned_ownerships = (
        db.session.query(
            ClientOwnership.server_id,
            ClientOwnership.inbound_id,
            ClientOwnership.client_email,
            ClientOwnership.client_uuid,
        )
        .filter(ClientOwnership.reseller_id == user.id)
        .all()
    )
    
    exact_matches = set()
    loose_matches = set()
    exact_uuid_matches = set()
    loose_uuid_matches = set()
    # UUID alone, independent of server_id/inbound_id. A reseller still only sees
    # clients inside inbounds they have access to (gated separately below), so
    # matching their owned UUIDs globally is safe and survives a server re-add or
    # an inbound rebuild that changes the numeric inbound_id.
    owned_uuid_any = set()

    for server_id_val, inbound_id_val, client_email_val, client_uuid_val in owned_ownerships:
        c_email = (client_email_val or '').lower()
        c_uuid = (client_uuid_val or '').strip().lower()
        sid = int(server_id_val)
        if c_uuid:
            owned_uuid_any.add(c_uuid)

        if inbound_id_val is not None:
            exact_matches.add((sid, int(inbound_id_val), c_email))
            if c_uuid:
                exact_uuid_matches.add((sid, int(inbound_id_val), c_uuid))
        else:
            loose_matches.add((sid, c_email))
            if c_uuid:
                loose_uuid_matches.add((sid, c_uuid))

    filtered_inbounds = []
    unique_server_ids = set()
    
    # متغیرهای آمار مخصوص ریسلر
    reseller_stats = {
        "total_inbounds": 0,
        "active_inbounds": 0,
        "total_clients": 0,     # فقط کلاینت‌های Assign شده
        "online_clients": 0,
        "active_clients": 0,    # فقط کلاینت‌های Assign شده فعال
        "inactive_clients": 0,  # فقط کلاینت‌های Assign شده غیرفعال
        "not_started_clients": 0,
        "unlimited_expiry_clients": 0,
        "unlimited_volume_clients": 0,
        "upload_raw": 0,        # فقط مصرف کلاینت‌های Assign شده
        "download_raw": 0
    }

    for inbound in data['inbounds']:
        sid = inbound['server_id']
        iid = inbound['id']
        
        # شرط ۱: دسترسی به اینباند (از طریق Allowed Server یا Assignment)
        if not is_inbound_accessible(sid, iid, allowed_map, assignments):
            continue
            
        # اینباند مجاز است
        unique_server_ids.add(sid)
        
        # برای نمایش در لیست: آمار کل اینباند را برای ریسلر صفر می‌کنیم (طبق درخواست)
        inbound['total_up'] = "---"
        inbound['total_down'] = "---"
        
        # شمارش اینباند
        reseller_stats["total_inbounds"] += 1
        if inbound.get('enable'):
            reseller_stats["active_inbounds"] += 1

        # پردازش کلاینت‌ها برای آمار دقیق و فیلتر کردن لیست نمایش
        clients_in_inbound = inbound.get('clients', [])
        filtered_clients_list = []
        
        for client in clients_in_inbound:
            c_email = client.get('email', '').lower()
            c_uuid = str(client.get('id') or '').strip().lower()
            
            # چک می‌کنیم آیا این کلاینت به ریسلر Assign شده؟
            # 1. تطابق دقیق (سرور، اینباند، ایمیل)
            # 2. تطابق بدون اینباند (سرور، ایمیل) - برای رکوردهای قدیمی یا ناقص
            is_assigned = False
            if c_uuid:
                is_assigned = (
                    c_uuid in owned_uuid_any
                    or (sid, iid, c_uuid) in exact_uuid_matches
                    or (sid, c_uuid) in loose_uuid_matches
                )
            if not is_assigned:
                is_assigned = (sid, iid, c_email) in exact_matches or (sid, c_email) in loose_matches
            
            if is_assigned:
                # اضافه کردن به لیست فیلتر شده برای نمایش
                filtered_clients_list.append(client)
                
                # محاسبه آمار
                reseller_stats["total_clients"] += 1
                if client.get('is_online'):
                    reseller_stats["online_clients"] += 1
                if client.get('enable'):
                    reseller_stats["active_clients"] += 1
                else:
                    reseller_stats["inactive_clients"] += 1

                if client.get('expiryType') == 'start_after_use':
                    reseller_stats["not_started_clients"] += 1

                if client.get('expiryType') == 'unlimited':
                    reseller_stats["unlimited_expiry_clients"] += 1

                # totalGB_formatted is already normalized in cached payload
                if (client.get('totalGB_formatted') == 'Unlimited'):
                    reseller_stats["unlimited_volume_clients"] += 1
                
                # جمع زدن ترافیک کلاینت‌های خودش
                reseller_stats["upload_raw"] += client.get('up', 0)
                reseller_stats["download_raw"] += client.get('down', 0)

        # جایگزینی لیست کلاینت‌های اینباند با لیست فیلتر شده
        inbound['clients'] = filtered_clients_list
        # آپدیت تعداد کلاینت‌های نمایش داده شده در اینباند
        inbound['client_count'] = len(filtered_clients_list)

        filtered_inbounds.append(inbound)

    # فرمت کردن آمار نهایی
    reseller_stats["total_traffic"] = format_bytes(reseller_stats["upload_raw"] + reseller_stats["download_raw"])
    reseller_stats["total_upload"] = format_bytes(reseller_stats["upload_raw"])
    reseller_stats["total_download"] = format_bytes(reseller_stats["download_raw"])
    
    # فیلتر کردن وضعیت سرورها
    filtered_servers_status = [
        s for s in data['servers_status'] 
        if s['server_id'] in unique_server_ids
    ]

    resp = {
        "success": True,
        "inbounds": filtered_inbounds,
        "stats": reseller_stats,
        "servers": filtered_servers_status,
        "server_count": len(unique_server_ids),
        "last_update": data['last_update'],
        "is_updating": bool(GLOBAL_SERVER_DATA.get('is_updating')),
        "refresh_job": _summarize_job(job),
    }
    if debug_timing and t0 is not None and t_after_copy is not None:
        resp['timing_ms'] = {
            'deepcopy': round((t_after_copy - t0) * 1000.0, 2),
            'total': round((time.perf_counter() - t0) * 1000.0, 2),
        }
    return jsonify(resp), (202 if job and job.get('state') in ('queued', 'running') else 200)


@bp.route('/api/refresh/job/<job_id>')
@login_required
@limiter.exempt
def api_refresh_job(job_id):
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, _get_refresh_job, _summarize_job,
    )
    job_copy = _get_refresh_job(job_id)
    if not job_copy:
        return jsonify({"success": False, "error": "Job not found"}), 404
    resp = make_response(jsonify({
        "success": True,
        "job": _summarize_job(job_copy),
        "is_updating": bool(GLOBAL_SERVER_DATA.get('is_updating')),
        "last_update": GLOBAL_SERVER_DATA.get('last_update')
    }))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@bp.route('/api/servers/list')
@login_required
def api_servers_list():
    from app import get_accessible_servers  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    servers = get_accessible_servers(user)
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'panel_type': s.panel_type
    } for s in servers])

@bp.route('/api/traffic_check')
@login_required
def api_traffic_check():
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, format_bytes, get_accessible_servers,
    )
    user = db.session.get(Admin, session['admin_id'])
    server_id_param = request.args.get('server_id', 'all')
    end_date_param = request.args.get('end_date')  # YYYY-MM-DD

    end_ts_ms = None
    if end_date_param:
        try:
            dt = datetime.strptime(end_date_param, '%Y-%m-%d')
            dt = dt.replace(hour=23, minute=59, second=59)
            end_ts_ms = int(dt.timestamp() * 1000)
        except Exception:
            return jsonify({"success": False, "error": "Invalid end_date format"}), 400

    accessible_servers = get_accessible_servers(user)
    accessible_ids = {s.id for s in accessible_servers}
    server_names = {s.id: s.name for s in accessible_servers}

    inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    result_clients = []
    total_remaining_bytes = 0

    for client in inbounds:
        try:
            sid = int(client.get('server_id', -1))
        except Exception:
            continue
        if sid not in accessible_ids:
            continue
        if server_id_param != 'all':
            try:
                if sid != int(server_id_param):
                    continue
            except Exception:
                continue
        if not client.get('enable', True):
            continue
        remaining_bytes = client.get('remaining_bytes', -1)
        if remaining_bytes is None or remaining_bytes <= 0:
            continue
        expiry_ts = int(client.get('expiryTimestamp') or 0)
        if end_ts_ms is not None:
            if expiry_ts <= 0:
                continue
            if expiry_ts > end_ts_ms:
                continue
        total_remaining_bytes += remaining_bytes
        result_clients.append({
            "email": client.get('email', ''),
            "server_id": sid,
            "server_name": server_names.get(sid, f"Server {sid}"),
            "expiry_text": client.get('expiryTime', ''),
            "expiry_timestamp": expiry_ts,
            "remaining": format_bytes(remaining_bytes),
            "remaining_bytes": remaining_bytes,
            "total": client.get('totalGB_formatted', ''),
            "used": format_bytes(int(client.get('up', 0) or 0) + int(client.get('down', 0) or 0)),
        })

    result_clients.sort(key=lambda x: x['remaining_bytes'], reverse=True)
    return jsonify({
        "success": True,
        "total_remaining_bytes": total_remaining_bytes,
        "total_remaining": format_bytes(total_remaining_bytes),
        "client_count": len(result_clients),
        "clients": result_clients,
    })

@bp.route('/api/server/<int:server_id>/refresh')
@login_required
def api_refresh_single_server(server_id):
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, _get_refresh_job, _parse_bool, _summarize_job,
        enqueue_refresh_job, enrich_inbounds_with_ownership,
        load_snapshot_from_redis,
    )
    user = db.session.get(Admin, session['admin_id'])
    server = Server.query.get_or_404(server_id)
    
    # Check access
    if user.role != 'superadmin':
        if user.allowed_servers != '*' and str(server.id) not in user.allowed_servers.split(','):
             return jsonify({"success": False, "error": "Access denied"}), 403

    # Non-blocking: optionally enqueue a refresh job and return cached data immediately.
    force = _parse_bool(request.args.get('force'))
    wait = _parse_bool(request.args.get('wait'))
    mode = (request.args.get('mode') or 'full').strip().lower()
    enqueue = request.args.get('enqueue')
    enqueue = _parse_bool(enqueue) if enqueue is not None else (mode != 'cache')

    # Multi-worker: pull the freshest shared snapshot so a write-through edit made
    # on another worker is visible here immediately (cheap version check; no-op
    # without Redis). This is what keeps a post-edit cache read from reverting.
    if mode == 'cache':
        try:
            load_snapshot_from_redis()
        except Exception:
            pass

    job = None
    if enqueue and mode in ('full', 'status'):
        job = enqueue_refresh_job(mode='full', server_id=server.id, force=force)

        # Optional short wait for UI actions; cap to keep endpoint snappy.
        if wait and job:
            start = time.time()
            while (time.time() - start) < 2.0:
                cur = _get_refresh_job(job.get('id'))
                if cur and cur.get('state') in ('done', 'error'):
                    job = cur
                    break
                time.sleep(0.15)

    # Pull this server's cached block (if present)
    cached_inbounds = []
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id', -1)) == int(server.id):
                cached_inbounds.append(inbound)
        except Exception:
            continue

    cached_stats = None
    cached_status = None
    for st in (GLOBAL_SERVER_DATA.get('servers_status') or []):
        try:
            if int(st.get('server_id', -1)) == int(server.id):
                cached_stats = st.get('stats')
                cached_status = st
                break
        except Exception:
            continue

    global_stats = GLOBAL_SERVER_DATA.get('stats') or {}
    server_count = len(GLOBAL_SERVER_DATA.get('servers_status') or [])
    if user.role != 'reseller':
        cached_inbounds = copy.deepcopy(cached_inbounds)
        enrich_inbounds_with_ownership(cached_inbounds)

    return jsonify({
        "success": True,
        "server_id": server.id,
        "server_name": server.name,
        "inbounds": cached_inbounds,
        "stats": global_stats,
        "server_stats": cached_stats or {},
        "server_status": cached_status or {},
        "server_count": server_count,
        "panel_type": server.panel_type,
        "last_update": GLOBAL_SERVER_DATA.get('last_update'),
        "is_updating": bool(GLOBAL_SERVER_DATA.get('is_updating')),
        "refresh_job": _summarize_job(job)
    }), (202 if job and job.get('state') in ('queued', 'running') else 200)


@bp.route('/api/server/<int:server_id>/last-users')
@login_required
def api_server_last_users(server_id):
    """Recent users per inbound (most recent first), from cache only (fast path).
    Resellers only see clients THEY own; admins/superadmins see all."""
    from app import GLOBAL_SERVER_DATA  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    owned_emails = None  # None = no ownership filter (admin); set = reseller filter
    if user and user.role == 'reseller':
        owned_emails = {
            (o.client_email or '').strip().lower()
            for o in ClientOwnership.query.filter_by(reseller_id=user.id, server_id=server_id).all()
            if o.client_email
        }

    RECENT_N = 8
    recent_users = {}
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id', -1)) != int(server_id):
                continue
        except Exception:
            continue
        inbound_id = inbound.get('id')
        if inbound_id is None:
            continue

        emails = []
        clients = inbound.get('clients') or []
        if isinstance(clients, list):
            for c in reversed(clients):  # most recent first
                if not isinstance(c, dict):
                    continue
                em = c.get('email')
                if not em:
                    continue
                if owned_emails is not None and str(em).strip().lower() not in owned_emails:
                    continue
                emails.append(em)
                if len(emails) >= RECENT_N:
                    break
        recent_users[str(inbound_id)] = emails

    return jsonify({
        'success': True,
        'server_id': server_id,
        'recent_users': recent_users,
        'last_users': {k: (v[0] if v else None) for k, v in recent_users.items()},
        'last_update': GLOBAL_SERVER_DATA.get('last_update')
    })


@bp.route('/api/add-client/inbounds/<int:server_id>')
@login_required
def api_add_client_inbounds(server_id):
    """Lightweight, cache-only inbound list for the Add/Renew client modal.

    Returns minimal per-inbound fields (no client arrays) + the last user, read
    straight from the in-memory snapshot. Tiny payload → the dropdown is ready
    instantly, independent of the heavy dashboard data load.
    """
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, get_reseller_access_maps, is_inbound_accessible,
    )
    user = db.session.get(Admin, session['admin_id'])
    is_reseller = bool(user and user.role == 'reseller')
    allowed_map, assignments = ('*', {})
    owned_emails = None
    if is_reseller:
        allowed_map, assignments = get_reseller_access_maps(user)
        owned_emails = {
            (o.client_email or '').strip().lower()
            for o in ClientOwnership.query.filter_by(reseller_id=user.id, server_id=server_id).all()
            if o.client_email
        }

    RECENT_N = 8
    items = []
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id', -1)) != int(server_id):
                continue
        except Exception:
            continue

        inbound_id = inbound.get('id')
        if inbound_id is None:
            continue

        if is_reseller:
            try:
                if not is_inbound_accessible(int(server_id), int(inbound_id), allowed_map, assignments):
                    continue
            except Exception:
                continue

        clients = inbound.get('clients') or []
        try:
            active_count = inbound.get('active_count')
            if active_count is None:
                active_count = sum(1 for c in clients if isinstance(c, dict) and c.get('enable'))
        except Exception:
            active_count = 0

        # Recent users (most recent first), role-filtered for resellers.
        recent = []
        if isinstance(clients, list):
            for c in reversed(clients):
                if not isinstance(c, dict):
                    continue
                em = c.get('email')
                if not em:
                    continue
                if owned_emails is not None and str(em).strip().lower() not in owned_emails:
                    continue
                recent.append(em)
                if len(recent) >= RECENT_N:
                    break

        items.append({
            'id': inbound_id,
            'server_id': server_id,
            'remark': inbound.get('remark') or f'Inbound {inbound_id}',
            'protocol': inbound.get('protocol') or '',
            'port': inbound.get('port'),
            'client_count': inbound.get('client_count', len(clients)),
            'active_count': active_count,
            'last_user': (recent[0] if recent else None),
            'recent_users': recent,
        })

    return jsonify({
        'success': True,
        'server_id': server_id,
        'inbounds': items,
        'last_update': GLOBAL_SERVER_DATA.get('last_update'),
    })


@bp.route('/api/client/inbound-assignments/<int:server_id>')
@login_required
def api_client_inbound_assignments(server_id):
    """Authoritative source for the Edit-Client "Assigned inbounds" picker.

    Computes v3 capability server-side (Bearer or cookie authentication) and
    returns the role-filtered
    inbound list for the server plus the set of inbound ids the given client is
    currently a member of. This makes the picker work even if the dashboard's
    in-memory inbound cache for this server hasn't been populated yet.
    """
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, get_reseller_access_maps, get_xui_session,
        is_inbound_accessible, server_is_v3,
    )
    user = db.session.get(Admin, session['admin_id'])
    email = (request.args.get('email') or '').strip()
    email_l = email.lower()

    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({'success': False, 'error': 'Server not found'}), 404

    panel_session, panel_error = get_xui_session(server)
    is_v3 = bool(panel_session and not panel_error
                 and server_is_v3(server, panel_session))

    is_reseller = bool(user and user.role == 'reseller')
    allowed_map, assignments = ('*', {})
    owned_emails = None
    if is_reseller:
        allowed_map, assignments = get_reseller_access_maps(user)
        owned_emails = {
            (o.client_email or '').strip().lower()
            for o in ClientOwnership.query.filter_by(reseller_id=user.id, server_id=server_id).all()
            if o.client_email
        }

    items = []
    assigned_ids = []
    for inbound in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(inbound.get('server_id', -1)) != int(server_id):
                continue
        except Exception:
            continue

        inbound_id = inbound.get('id')
        if inbound_id is None:
            continue

        if is_reseller:
            try:
                if not is_inbound_accessible(int(server_id), int(inbound_id), allowed_map, assignments):
                    continue
            except Exception:
                continue

        items.append({
            'id': inbound_id,
            'server_id': server_id,
            'remark': inbound.get('remark') or f'Inbound {inbound_id}',
            'port': inbound.get('port'),
        })

        if email_l:
            for c in (inbound.get('clients') or []):
                if not isinstance(c, dict):
                    continue
                if (c.get('email') or '').strip().lower() != email_l:
                    continue
                if owned_emails is not None and email_l not in owned_emails:
                    continue
                assigned_ids.append(inbound_id)
                break

    return jsonify({
        'success': True,
        'server_id': server_id,
        'is_v3': bool(is_v3),
        'inbounds': items,
        'assigned_ids': assigned_ids,
    })
