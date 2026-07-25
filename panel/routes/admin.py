"""Admin, server, and reseller management API routes (extracted from app.py)."""
import io
import re
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file, session
from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from panel.extensions import db
from panel.models import (
    Admin, ClientOwnership, PriceTier, RenewalEvent, Server, SystemConfig,
    Transaction, UsageCounterState, UsageDaily, UsageHourly,
    announcement_servers,
)
from panel.routes.common import (
    login_required, superadmin_required, user_management_required,
)

bp = Blueprint('admin', __name__)


@bp.route('/api/admins', methods=['GET'])
@user_management_required
def get_admins():
    admins = Admin.query.all()
    return jsonify([a.to_dict() for a in admins])

@bp.route('/api/admins', methods=['POST'])
@superadmin_required
def add_admin():
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html, serialize_allowed_servers, validate_password_strength,
    )
    data = request.json
    username = data.get('username', '').strip().lower()
    
    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400
    
    if ' ' in username:
        return jsonify({"success": False, "error": "Username cannot contain spaces"}), 400
        
    # Check for Persian characters
    if any(u'\u0600' <= c <= u'\u06FF' for c in username):
        return jsonify({"success": False, "error": "Persian characters are not allowed"}), 400

    if Admin.query.filter_by(username=username).first():
        return jsonify({"success": False, "error": "Username exists"}), 400
    
    password = data.get('password')
    is_valid, error_msg = validate_password_strength(password)
    if not is_valid:
        return jsonify({"success": False, "error": error_msg}), 400

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

    def _clean_url(v: str | None, *, limit: int = 1000) -> str:
        return (v or '').strip()[:limit]

    def _opt_int(v):
        """Parse an optional integer field; '' / None ⇒ None, bad value ⇒ error."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return int(v)

    role = (data.get('role') or 'reseller').strip()
    if role not in ('reseller', 'admin', 'superadmin'):
        return jsonify({"success": False, "error": f"Invalid role '{role}'."}), 400

    # Parse numeric fields up front so a bad value gives a clear, field-named
    # message instead of a generic 500 from int() blowing up mid-build.
    try:
        credit = int(data.get('credit') or 0)
        negative_credit_limit = max(0, int(data.get('negative_credit_limit') or 0))
        discount_percent = int(data.get('discount_percent') or 0)
        custom_cost_per_day = _opt_int(data.get('custom_cost_per_day'))
        custom_cost_per_gb = _opt_int(data.get('custom_cost_per_gb'))
    except (TypeError, ValueError):
        return jsonify({"success": False,
                        "error": "Credit, discount and cost fields must be whole numbers."}), 400

    new_admin = Admin(
        username=username,
        role=role,
        is_superadmin=(role == 'superadmin'),
        credit=credit,
        allow_negative_credit=bool(data.get('allow_negative_credit', False)),
        negative_credit_limit=negative_credit_limit,
        allow_free_creation=bool(data.get('allow_free_creation', False)),
        whatsapp_automation_enabled=bool(data.get('whatsapp_automation_enabled', False)),
        allowed_servers=serialize_allowed_servers(data.get('allowed_servers', [])),
        enabled=data.get('enabled', True),
        discount_percent=discount_percent,
        custom_cost_per_day=custom_cost_per_day,
        custom_cost_per_gb=custom_cost_per_gb,
        telegram_id=sanitize_html(data.get('telegram_id')),
        support_telegram=_clean_telegram_username(data.get('support_telegram')),
        support_whatsapp=_clean_whatsapp_number(data.get('support_whatsapp')),
        support_sms=_clean_whatsapp_number(data.get('support_sms')),
        channel_telegram=_clean_url(data.get('channel_telegram')),
        channel_whatsapp=_clean_url(data.get('channel_whatsapp')),
    )
    new_admin.set_password(password)
    try:
        db.session.add(new_admin)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('[add_admin] failed to save reseller')
        return jsonify({"success": False,
                        "error": f"Could not save user: {exc}"}), 500
    return jsonify({"success": True})

@bp.route('/api/admins/<int:admin_id>', methods=['PUT'])
@user_management_required
def update_admin(admin_id):
    from app import (  # deferred: app-level helper, avoids circular import
        _normalize_username, _validate_username, sanitize_html, serialize_allowed_servers,
        validate_password_strength,
    )
    admin = Admin.query.get_or_404(admin_id)
    data = request.json

    editor = db.session.get(Admin, session.get('admin_id'))
    editor_is_super = bool(editor and (editor.role == 'superadmin' or editor.is_superadmin))
    target_is_super = bool(admin and (admin.role == 'superadmin' or admin.is_superadmin))

    if not editor_is_super and target_is_super:
        return jsonify({"success": False, "error": "Access Denied"}), 403

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

    def _clean_url(v: str | None, *, limit: int = 1000) -> str:
        return (v or '').strip()[:limit]

    if 'username' in data:
        new_username = _normalize_username(data.get('username'))
        # Important: if the only difference is casing (e.g. "Salar" -> "salar"),
        # we still want to persist the normalized value so login works.
        if new_username and new_username != (admin.username or ''):
            err = _validate_username(new_username)
            if err:
                return jsonify({"success": False, "error": err}), 400
            existing = Admin.query.filter(
                func.lower(Admin.username) == new_username,
                Admin.id != admin.id
            ).first()
            if existing:
                return jsonify({"success": False, "error": "Username exists"}), 400
            admin.username = new_username

    if data.get('password'):
        is_valid, error_msg = validate_password_strength(data['password'])
        if not is_valid:
            return jsonify({"success": False, "error": error_msg}), 400
        admin.set_password(data['password'])
    if data.get('role'):
        new_role = (data.get('role') or '').strip().lower()
        if new_role:
            if new_role == 'superadmin' and not editor_is_super:
                return jsonify({"success": False, "error": "Access Denied"}), 403
            admin.role = new_role
            admin.is_superadmin = (new_role == 'superadmin')
    if 'credit' in data: admin.credit = int(data['credit'])
    if 'allow_negative_credit' in data: admin.allow_negative_credit = bool(data['allow_negative_credit'])
    if 'negative_credit_limit' in data: admin.negative_credit_limit = max(0, int(data['negative_credit_limit'] or 0))
    if 'allow_free_creation' in data: admin.allow_free_creation = bool(data['allow_free_creation'])
    if 'whatsapp_automation_enabled' in data: admin.whatsapp_automation_enabled = bool(data['whatsapp_automation_enabled'])
    if 'allowed_servers' in data: admin.allowed_servers = serialize_allowed_servers(data['allowed_servers'])
    if 'enabled' in data: admin.enabled = data['enabled']
    if 'discount_percent' in data: admin.discount_percent = int(data['discount_percent'])
    if 'custom_cost_per_day' in data: 
        admin.custom_cost_per_day = int(data['custom_cost_per_day']) if data['custom_cost_per_day'] is not None else None
    if 'custom_cost_per_gb' in data: 
        admin.custom_cost_per_gb = int(data['custom_cost_per_gb']) if data['custom_cost_per_gb'] is not None else None
    if 'telegram_id' in data: admin.telegram_id = sanitize_html(data['telegram_id'])
    if 'support_telegram' in data: admin.support_telegram = _clean_telegram_username(data.get('support_telegram'))
    if 'support_whatsapp' in data: admin.support_whatsapp = _clean_whatsapp_number(data.get('support_whatsapp'))
    if 'support_sms' in data: admin.support_sms = _clean_whatsapp_number(data.get('support_sms'))
    if 'channel_telegram' in data: admin.channel_telegram = _clean_url(data.get('channel_telegram'))
    if 'channel_whatsapp' in data: admin.channel_whatsapp = _clean_url(data.get('channel_whatsapp'))
    db.session.commit()

    # Keep session consistent if user edited self
    try:
        if editor and int(editor.id) == int(admin.id):
            session['admin_username'] = admin.username
            session['role'] = admin.role
            session['is_superadmin'] = (admin.role == 'superadmin' or admin.is_superadmin)
    except Exception:
        pass
    return jsonify({"success": True})

@bp.route('/api/admins/<int:admin_id>', methods=['DELETE'])
@superadmin_required
def delete_admin(admin_id):
    if admin_id == session['admin_id']:
        return jsonify({"success": False, "error": "Self-delete not allowed"}), 400
    admin = Admin.query.get_or_404(admin_id)
    db.session.delete(admin)
    db.session.commit()
    return jsonify({"success": True})


@bp.route('/api/servers', methods=['GET'])
@login_required
def get_servers():
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, get_accessible_servers,
    )
    user = db.session.get(Admin, session['admin_id'])
    if user.role == 'reseller':
        servers = get_accessible_servers(user)
    else:
        servers = Server.query.all()
    status_map = {}
    for st in (GLOBAL_SERVER_DATA.get('servers_status') or []):
        try:
            sid = int(st.get('server_id', -1))
        except Exception:
            continue
        if sid > 0:
            status_map[sid] = st

    payload = []
    for s in servers:
        item = s.to_dict()
        st = status_map.get(int(s.id)) or {}
        item.update({
            'online_count': st.get('online_count'),
            'xui_version': st.get('xui_version'),
            'xray_version': st.get('xray_version'),
            'xray_state': st.get('xray_state'),
            'xray_core': st.get('xray_core'),
            'panel_status_error': st.get('panel_status_error'),
            'panel_status_checked_at': st.get('panel_status_checked_at'),
            'reachable': st.get('reachable'),
            'reachable_error': st.get('reachable_error')
        })
        payload.append(item)

    # Never cache the server list — edits must show immediately, not a stale
    # browser/proxy copy.
    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@bp.route('/api/servers', methods=['POST'])
@login_required
def add_server():
    from app import (  # deferred: app-level helper, avoids circular import
        encrypt_server_password, sanitize_html,
    )
    if session.get('role') == 'reseller':
        return jsonify({"success": False, "error": "Only admins can add servers"}), 403
    
    data = request.json
    server_password = (data.get('password') or '').strip()
    if not server_password:
        return jsonify({"success": False, "error": "Password is required"}), 400
    _api_token = (data.get('api_token') or '').strip()
    server = Server(
        name=sanitize_html(data['name']),
        host=sanitize_html(data['host']),
        username=sanitize_html(data['username']),
        password=encrypt_server_password(server_password),
        panel_type=data.get('panel_type', 'auto'),
        sub_path=data.get('sub_path', '/sub/'),
        json_path=data.get('json_path', '/json/'),
        sub_port=data.get('sub_port'),
        api_token=encrypt_server_password(_api_token) if _api_token else None,
    )
    db.session.add(server)
    db.session.commit()
    return jsonify({"success": True, "id": server.id})

@bp.route('/api/servers/<int:server_id>', methods=['PUT'])
@login_required
def update_server(server_id):
    from app import (  # deferred: app-level helper, avoids circular import
        XUI_CAPABILITY_CACHE, XUI_SESSION_CACHE, encrypt_server_password, sanitize_html,
    )
    if session.get('role') == 'reseller':
        return jsonify({"success": False, "error": "Only admins can update servers"}), 403
    
    server = Server.query.get_or_404(server_id)
    data = request.json
    server.name = sanitize_html(data.get('name', server.name))
    server.host = sanitize_html(data.get('host', server.host))
    server.username = sanitize_html(data.get('username', server.username))
    if 'password' in data:
        new_password = (data.get('password') or '').strip()
        if new_password:
            server.password = encrypt_server_password(new_password)
    server.panel_type = data.get('panel_type', server.panel_type)
    server.sub_path = data.get('sub_path', server.sub_path)
    server.json_path = data.get('json_path', server.json_path)
    server.sub_port = data.get('sub_port', server.sub_port)
    server.enabled = data.get('enabled', server.enabled)
    if 'hidden' in data:
        server.hidden = bool(data['hidden'])
    if 'api_token' in data:
        _tok = (data.get('api_token') or '').strip()
        # Non-empty → set/replace; explicit empty string → clear it.
        server.api_token = encrypt_server_password(_tok) if _tok else None
    db.session.commit()
    # Token change alters auth — drop any cached session so the next call re-auths.
    XUI_SESSION_CACHE.pop(server_id, None)
    XUI_CAPABILITY_CACHE.pop(server_id, None)
    return jsonify({"success": True})


@bp.route('/api/servers/<int:server_id>/hidden', methods=['POST'])
@login_required
def toggle_server_hidden(server_id):
    """Toggle server hidden flag. Hidden servers are skipped in fetching/dashboard but still backed up."""
    from app import GLOBAL_SERVER_DATA  # deferred: app-level helper, avoids circular import
    if session.get('role') == 'reseller':
        return jsonify({"success": False, "error": "Only admins can toggle server visibility"}), 403
    server = Server.query.get_or_404(server_id)
    server.hidden = not bool(server.hidden)
    db.session.commit()
    if server.hidden:
        # Remove from in-memory cache so it disappears from dashboard immediately
        GLOBAL_SERVER_DATA['inbounds'] = [
            item for item in (GLOBAL_SERVER_DATA.get('inbounds') or [])
            if str(item.get('server_id') or '') != str(server_id)
        ]
        GLOBAL_SERVER_DATA['servers_status'] = [
            item for item in (GLOBAL_SERVER_DATA.get('servers_status') or [])
            if str(item.get('server_id') or '') != str(server_id)
        ]
    return jsonify({"success": True, "hidden": server.hidden})


@bp.route('/api/servers/<int:server_id>', methods=['DELETE'])
@login_required
def delete_server(server_id):
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, REFRESH_BACKOFF, XUI_CAPABILITY_CACHE, XUI_SESSION_CACHE, app,
        invalidate_ownership_cache,
    )
    if session.get('role') == 'reseller':
        return jsonify({"success": False, "error": "Only admins can delete servers"}), 403

    server = Server.query.get_or_404(server_id)

    try:
        db.session.execute(
            announcement_servers.delete().where(announcement_servers.c.server_id == server_id)
        )
        ClientOwnership.query.filter_by(server_id=server_id).delete(synchronize_session=False)
        UsageCounterState.query.filter_by(server_id=server_id).delete(synchronize_session=False)
        UsageHourly.query.filter_by(server_id=server_id).delete(synchronize_session=False)
        UsageDaily.query.filter_by(server_id=server_id).delete(synchronize_session=False)
        RenewalEvent.query.filter_by(server_id=server_id).delete(synchronize_session=False)
        PriceTier.query.filter_by(server_id=server_id).delete(synchronize_session=False)
        Transaction.query.filter_by(server_id=server_id).update(
            {Transaction.server_id: None},
            synchronize_session=False
        )

        db.session.delete(server)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        app.logger.exception("Failed to delete server %s", server_id)
        return jsonify({
            "success": False,
            "error": "Server could not be deleted. Please check related records and try again.",
            "details": str(exc)
        }), 500

    XUI_SESSION_CACHE.pop(server_id, None)
    XUI_CAPABILITY_CACHE.pop(server_id, None)
    REFRESH_BACKOFF.pop(server_id, None)
    invalidate_ownership_cache()
    GLOBAL_SERVER_DATA['inbounds'] = [
        item for item in (GLOBAL_SERVER_DATA.get('inbounds') or [])
        if str(item.get('server_id') or '') != str(server_id)
    ]
    GLOBAL_SERVER_DATA['servers_status'] = [
        item for item in (GLOBAL_SERVER_DATA.get('servers_status') or [])
        if str(item.get('id') or item.get('server_id') or '') != str(server_id)
    ]
    return jsonify({"success": True})

@bp.route('/api/servers/<int:server_id>/test', methods=['POST'])
@login_required
def test_server_connection(server_id):
    from app import (  # deferred: app-level helper, avoids circular import
        _autoupgrade_http_to_https, fetch_inbounds, get_server_api_token, get_xui_session,
    )
    server = Server.query.get_or_404(server_id)
    # Self-heal the common "http:// stored for an HTTPS-only panel" foot-gun, which
    # otherwise fails with a bare ConnectionError ("Error testing connection").
    _autoupgrade_http_to_https(server)
    session_obj, error = get_xui_session(server)
    if error:
        return jsonify({"success": False, "error": error}), 400
    # Actually read data so a wrong API token / unreachable panel is caught here
    # (Bearer auth sets a header without contacting the panel, so we must probe).
    inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
    if fetch_err:
        return jsonify({"success": False, "error": fetch_err}), 400
    return jsonify({
        "success": True,
        "panel_type": detected_type or server.panel_type,
        "inbound_count": len(inbounds or []),
        "auth": "token" if get_server_api_token(server) else "login",
    })


@bp.route('/api/servers/<int:server_id>/xui-backup', methods=['GET'])
@login_required
def download_server_xui_backup(server_id):
    """Download the X-UI database backup for a single server.

    Used by the bulk-action confirmation modal so the operator can grab a
    safety backup of the panel DB before applying changes.
    """
    from app import (  # deferred: app-level helper, avoids circular import
        _fetch_xui_backup, get_xui_session,
    )
    server = Server.query.get_or_404(server_id)
    # Resellers cannot pull raw panel DB backups
    if session.get('role') == 'reseller':
        return jsonify({"success": False, "error": "Only admins can download panel backups"}), 403

    session_obj, error = get_xui_session(server)
    if error:
        return jsonify({"success": False, "error": error}), 400

    payload, ext, err = _fetch_xui_backup(session_obj, server)
    if not payload:
        return jsonify({"success": False, "error": err or "Backup failed"}), 502

    safe_name = secure_filename(server.name) or f"server_{server.id}"
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"xui_{safe_name}_{ts}{ext or '.db'}"
    return send_file(
        io.BytesIO(payload),
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=filename,
    )


@bp.route('/api/servers/<int:server_id>/panel-info', methods=['GET'])
@login_required
def get_server_panel_info(server_id):
    """Quick fetch: login → status endpoint → return version/state info.
    Does NOT fetch inbounds. Designed to be called right after adding a server."""
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, _normalize_server_status_payload, fetch_server_status,
        get_xui_session, persist_detected_panel_type,
    )
    server = Server.query.get_or_404(server_id)

    session_obj, login_error = get_xui_session(server)
    if login_error:
        return jsonify({"success": False, "error": login_error}), 400

    status_payload, status_error, detected_type = fetch_server_status(
        session_obj, server.host, server.panel_type
    )

    if detected_type and detected_type != 'auto':
        persist_detected_panel_type(server, detected_type)

    info = {
        "success": True,
        "server_id": server.id,
        "panel_type": server.panel_type or detected_type or "auto",
        "xui_version": None,
        "xray_version": None,
        "xray_state": None,
        "xray_core": None,
        "status_error": status_error,
    }

    if status_payload:
        normalized = _normalize_server_status_payload(status_payload)
        info.update({
            "xui_version": normalized.get("xui_version"),
            "xray_version": normalized.get("xray_version"),
            "xray_state": normalized.get("xray_state"),
            "xray_core": normalized.get("xray_core"),
        })

    # Also update in-memory cache so GET /api/servers reflects this immediately
    existing = GLOBAL_SERVER_DATA.get('servers_status') or []
    updated = False
    for st in existing:
        if isinstance(st, dict) and st.get('server_id') == server.id:
            st.update({k: v for k, v in info.items() if k not in ('success', 'status_error')})
            st['panel_status_checked_at'] = datetime.utcnow().isoformat()
            updated = True
            break
    if not updated:
        entry = {k: v for k, v in info.items() if k not in ('success', 'status_error')}
        entry['panel_status_checked_at'] = datetime.utcnow().isoformat()
        entry['panel_status_error'] = status_error
        GLOBAL_SERVER_DATA.setdefault('servers_status', []).append(entry)

    return jsonify(info)


@bp.route('/api/assign-client', methods=['POST'])
@user_management_required
def assign_client():
    from app import (  # deferred: app-level helper, avoids circular import
        ensure_reseller_allowed_for_assignment, invalidate_ownership_cache,
    )
    data = request.json
    server_id = data.get('server_id')
    email = (data.get('email') or '').strip()
    reseller_id = data.get('reseller_id')
    inbound_id = data.get('inbound_id')
    client_uuid = (data.get('client_uuid') or '').strip()

    try:
        server_id = int(server_id)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "server_id required"}), 400

    if not email:
        return jsonify({"success": False, "error": "email required"}), 400

    # Treat reseller_id=0 / null as "unassign to system"
    try:
        reseller_id_int = int(reseller_id) if reseller_id is not None else 0
    except (TypeError, ValueError):
        reseller_id_int = 0

    email_l = email.lower()

    match_filters = [ClientOwnership.server_id == server_id]
    match_key_filters = []
    if client_uuid:
        match_key_filters.append(ClientOwnership.client_uuid == client_uuid)
    if email_l:
        match_key_filters.append(func.lower(ClientOwnership.client_email) == email_l)
    if match_key_filters:
        match_filters.append(or_(*match_key_filters))

    if reseller_id_int <= 0:
        # Unassign: delete any ownership records for this server+email
        try:
            q = ClientOwnership.query
            for f in match_filters:
                q = q.filter(f)
            q.delete(synchronize_session=False)
            db.session.commit()
            return jsonify({"success": True})
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": str(e)}), 500

    reseller = db.session.get(Admin, reseller_id_int)
    if not reseller or reseller.role != 'reseller':
        return jsonify({"success": False, "error": "Invalid reseller"}), 400

    try:
        inbound_id_int = int(inbound_id) if inbound_id is not None and str(inbound_id).strip() != '' else None
    except (TypeError, ValueError):
        inbound_id_int = None

    # Reassign: ensure uniqueness by removing previous owners for this server+email
    try:
        q = ClientOwnership.query
        for f in match_filters:
            q = q.filter(f)
        q.delete(synchronize_session=False)

        ownership = ClientOwnership(
            reseller_id=reseller_id_int,
            server_id=server_id,
            inbound_id=inbound_id_int,
            client_email=email,
            client_uuid=client_uuid if client_uuid else None
        )
        db.session.add(ownership)

        # Keep reseller "Allowed Servers" in sync with assignments
        try:
            ensure_reseller_allowed_for_assignment(reseller, server_id, inbound_id_int)
        except Exception:
            pass

        db.session.commit()
        invalidate_ownership_cache()
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route('/api/resellers/<int:reseller_id>/bulk-assign-inbound', methods=['POST'])
@user_management_required
def bulk_assign_inbound(reseller_id):
    """Assign all existing clients in a cached inbound to a reseller."""
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, ensure_reseller_allowed_for_assignment, invalidate_ownership_cache,
    )
    data = request.json or {}
    try:
        server_id  = int(data['server_id'])
        inbound_id = int(data['inbound_id'])
    except (KeyError, TypeError, ValueError):
        return jsonify({"success": False, "error": "server_id and inbound_id required"}), 400

    reseller = db.session.get(Admin, reseller_id)
    if not reseller or reseller.role != 'reseller':
        return jsonify({"success": False, "error": "Invalid reseller"}), 400

    # Find the inbound in the in-memory cache
    cached_inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    target_inbound = None
    for inb in cached_inbounds:
        try:
            if int(inb.get('server_id', -1)) == server_id and int(inb.get('id', -1)) == inbound_id:
                target_inbound = inb
                break
        except (TypeError, ValueError):
            continue

    if not target_inbound:
        return jsonify({"success": False,
                        "error": "Inbound not in cache — refresh server data first"}), 404

    clients = target_inbound.get('clients') or []

    assigned = 0
    skipped  = 0
    try:
        for client in clients:
            email       = (client.get('email') or '').strip()
            client_uuid = (client.get('id')    or '').strip()
            if not email and not client_uuid:
                skipped += 1
                continue

            # Skip if already owned by this reseller on this inbound
            q = ClientOwnership.query.filter_by(
                reseller_id=reseller_id,
                server_id=server_id,
                inbound_id=inbound_id
            )
            if email:
                q = q.filter(func.lower(ClientOwnership.client_email) == email.lower())
            if q.first():
                skipped += 1
                continue

            # Remove any prior ownership of this client on this server
            del_q = ClientOwnership.query.filter(ClientOwnership.server_id == server_id)
            if email:
                del_q = del_q.filter(func.lower(ClientOwnership.client_email) == email.lower())
            elif client_uuid:
                del_q = del_q.filter(ClientOwnership.client_uuid == client_uuid)
            del_q.delete(synchronize_session=False)

            db.session.add(ClientOwnership(
                reseller_id=reseller_id,
                server_id=server_id,
                inbound_id=inbound_id,
                client_email=email or client_uuid,
                client_uuid=client_uuid or None,
            ))
            assigned += 1

        if assigned > 0:
            try:
                ensure_reseller_allowed_for_assignment(reseller, server_id, inbound_id)
            except Exception:
                pass

        db.session.commit()
        invalidate_ownership_cache()
        return jsonify({"success": True, "assigned": assigned, "skipped": skipped})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route('/admin/config', methods=['POST'])
@user_management_required
def update_config():
    data = request.json
    for key, value in data.items():
        config = db.session.get(SystemConfig, key)
        if config:
            config.value = str(value)
        else:
            db.session.add(SystemConfig(key=key, value=str(value)))
    db.session.commit()
    return jsonify({'success': True})

