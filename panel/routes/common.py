"""Shared route decorators (session auth guards) extracted from app.py."""
from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for

from panel.extensions import db
from panel.models import Admin
from panel.services.permissions import has_permission, permissions_for
from panel.services.sessions import (
    SESSION_TOKEN_KEY, resolve_session, role_of, step_up_fresh,
)


def current_admin():
    """Return the authoritative enabled admin for this request, if any.

    When the browser carries a server-side session token (every login after
    Phase 3), the registry row is authoritative: revoked, expired or idle
    sessions are rejected even though the signed cookie is still valid.
    Sessions created before the registry existed (and the test suite, which
    seeds the Flask session directly) fall back to the admin row.
    """
    admin_id = session.get('admin_id')
    admin = db.session.get(Admin, admin_id) if admin_id is not None else None
    if not admin or not bool(admin.enabled):
        return None
    token = session.get(SESSION_TOKEN_KEY)
    if token:
        row = resolve_session(token)
        if row is None or row.admin_id != admin.id:
            return None
        g._admin_session = row
        try:
            if row in db.session.dirty:
                db.session.commit()
        except Exception:
            db.session.rollback()
    return admin


def current_session():
    """Return the registry row for this request, if it has one."""
    cached = getattr(g, '_admin_session', None)
    if cached is not None:
        return cached
    token = session.get(SESSION_TOKEN_KEY)
    if not token:
        return None
    row = resolve_session(token)
    g._admin_session = row
    return row


def admin_is_superadmin(admin) -> bool:
    return bool(admin and (admin.role == 'superadmin' or admin.is_superadmin))


def mfa_required_for(admin) -> bool:
    """True when the account must complete MFA before a session is granted."""
    if not admin or not bool(admin.enabled):
        return False
    raw = (__import__('os').environ.get('EVE_MFA_REQUIRED_ROLES') or 'superadmin').strip().lower()
    if raw in ('', 'none', 'off', 'false', '0'):
        return False
    roles = {part.strip() for part in raw.split(',') if part.strip()}
    return role_of(admin) in roles


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


def step_up_required(scope: str | None = None):
    """Require a fresh MFA re-check for a sensitive mutation.

    Enforced for registry-backed sessions. Legacy sessions that predate the
    registry (no token in the cookie) are grandfathered so a rolling deploy
    cannot lock operators out; every new login is registry-backed.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            admin = current_admin()
            if not admin:
                session.clear()
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            token = session.get(SESSION_TOKEN_KEY)
            if token:
                row = current_session()
                if not step_up_fresh(row):
                    return jsonify({
                        "success": False,
                        "code": "step_up_required",
                        "error": "Re-authentication required for this action",
                        "scope": scope,
                    }), 403
            _sync_session_authority(admin)
            return f(*args, **kwargs)
        return wrapper
    return decorator




def current_permissions() -> frozenset:
    """Effective permission set for the signed-in admin (empty when anonymous)."""
    admin = current_admin()
    if admin is None:
        return frozenset()
    return permissions_for(admin)


def permission_required(permission: str):
    """Server-side permission gate.

    Runs after the session guard so an unauthenticated caller is still a 401 and
    a missing permission is a 403. The permission catalog and role defaults live
    in panel/services/permissions.py.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            admin = current_admin()
            if not admin:
                session.clear()
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            # Refresh presentation fields from the authoritative admin row before
            # the decision, so a tampered session role is corrected even on a 403.
            _sync_session_authority(admin)
            if not has_permission(admin, permission):
                return jsonify({
                    "success": False,
                    "code": "forbidden",
                    "error": f"Permission required: {permission}",
                }), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


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
