"""Shared route decorators (session auth guards) extracted from app.py."""
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from panel.extensions import db
from panel.models import Admin


def current_admin():
    """Return the authoritative enabled admin for this request, if any."""
    admin_id = session.get('admin_id')
    admin = db.session.get(Admin, admin_id) if admin_id is not None else None
    return admin if admin and bool(admin.enabled) else None


def admin_is_superadmin(admin) -> bool:
    return bool(admin and (admin.role == 'superadmin' or admin.is_superadmin))


def _sync_session_authority(admin) -> None:
    """Keep presentation fields current; authorization always uses ``admin``."""
    session['role'] = admin.role
    session['is_superadmin'] = admin_is_superadmin(admin)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        admin = current_admin()
        if not admin:
            session.clear()
            # For API endpoints, AJAX/XHR requests, or requests that accept JSON, return JSON errors
            is_api_path = request.path.startswith('/api/')
            accepts_json = 'application/json' in (request.headers.get('Accept') or '')
            is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json
            if is_api_path or accepts_json or is_xhr:
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            return redirect(url_for('auth.login'))
        _sync_session_authority(admin)
        return f(*args, **kwargs)
    return decorated_function


def client_portal_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'client_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


def superadmin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        admin = current_admin()
        if not admin:
            session.clear()
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        _sync_session_authority(admin)
        if not admin_is_superadmin(admin):
            return jsonify({"success": False, "error": "Access Denied: SuperAdmin only"}), 403
        return f(*args, **kwargs)
    return decorated_function


def user_management_required(f):
    """Allow admins and superadmins to manage users.

    Blocks reseller accounts.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        editor = current_admin()
        if not editor:
            session.clear()
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        _sync_session_authority(editor)
        if editor.role == 'reseller':
            return jsonify({"success": False, "error": "Access Denied"}), 403
        return f(*args, **kwargs)

    return decorated_function
