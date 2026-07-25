"""Shared route decorators (session auth guards) extracted from app.py."""
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from panel.extensions import db
from panel.models import Admin


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            # For API endpoints, AJAX/XHR requests, or requests that accept JSON, return JSON errors
            is_api_path = request.path.startswith('/api/')
            accepts_json = 'application/json' in (request.headers.get('Accept') or '')
            is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json
            if is_api_path or accepts_json or is_xhr:
                return jsonify({"success": False, "error": "Unauthorized"}), 401
            return redirect(url_for('auth.login'))
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
        if 'admin_id' not in session:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        admin = db.session.get(Admin, session['admin_id'])
        if not admin or (admin.role != 'superadmin' and not admin.is_superadmin):
            return jsonify({"success": False, "error": "Access Denied: SuperAdmin only"}), 403
        return f(*args, **kwargs)
    return decorated_function


def user_management_required(f):
    """Allow admins and superadmins to manage users.

    Blocks reseller accounts.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        editor = db.session.get(Admin, session['admin_id'])
        if not editor:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        if editor.role == 'reseller':
            return jsonify({"success": False, "error": "Access Denied"}), 403
        return f(*args, **kwargs)

    return decorated_function
