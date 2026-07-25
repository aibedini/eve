"""Authentication and client-portal routes (extracted from app.py)."""
from datetime import datetime

from flask import (
    Blueprint, jsonify, redirect, render_template, request, session, url_for,
)
from sqlalchemy import func

from panel.core.phone import _extract_iran_mobile_from_text
from panel.extensions import db, limiter
from panel.models import Admin, ClientPortalUser
from panel.routes.common import client_portal_required

bp = Blueprint('auth', __name__)


def _login_fail(msg: str):
    """Return appropriate login failure response."""
    if request.is_json:
        return jsonify({"success": False, "error": msg})
    return render_template('login.html', error=msg)


@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    from app import _normalize_username, app  # deferred: app-level helper, avoids circular import
    if 'admin_id' in session:
        return redirect(url_for('pages.dashboard'))
    if 'client_id' in session:
        return redirect(url_for('auth.client_portal'))

    if request.method == 'POST':
        data = request.form if request.form else request.json
        raw_input = (data.get('username') or '').strip()
        password = data.get('password') or ''

        # Determine auth path: Iranian mobile → client portal, otherwise → admin
        mobile = _extract_iran_mobile_from_text(raw_input)

        if mobile:
            # ── Client portal auth ─────────────────────────────
            client = ClientPortalUser.query.filter_by(mobile=mobile, enabled=True).first()
            if not client:
                app.logger.warning(f"Login — unknown client mobile {mobile} from {request.remote_addr}")
                return _login_fail("Invalid credentials")

            if client.is_locked():
                remaining = max(1, int((client.locked_until - datetime.utcnow()).total_seconds() / 60) + 1)
                return _login_fail(f"Account locked. Try again in {remaining} minute(s).")

            if not client.check_password(password):
                client.record_failed()
                db.session.commit()
                app.logger.warning(f"Login — wrong password for client {mobile} from {request.remote_addr} (attempt {client.failed_attempts})")
                if client.is_locked():
                    return _login_fail("Account locked after 5 failed attempts. Try again in 15 minutes.")
                left = 5 - (client.failed_attempts or 0)
                return _login_fail(f"Invalid credentials ({left} attempts remaining)")

            client.reset_failed()
            client.last_login = datetime.utcnow()
            db.session.commit()
            session.permanent = True
            session['client_id'] = client.id
            session['client_mobile'] = client.mobile
            session['client_display_name'] = client.display_name or client.mobile

            if client.must_change_password:
                dest = url_for('auth.client_change_password')
            else:
                dest = url_for('auth.client_portal')
            return jsonify({"success": True, "redirect": dest}) if request.is_json else redirect(dest)

        else:
            # ── Admin auth ─────────────────────────────────────
            username = _normalize_username(raw_input)
            admin = Admin.query.filter(
                func.lower(Admin.username) == username,
                Admin.enabled == True
            ).first()
            if admin and admin.check_password(password):
                session.permanent = True
                session['admin_id'] = admin.id
                session['admin_username'] = admin.username
                session['role'] = admin.role
                session['is_superadmin'] = (admin.role == 'superadmin' or admin.is_superadmin)
                admin.last_login = datetime.utcnow()
                db.session.commit()
                return jsonify({"success": True}) if request.is_json else redirect(url_for('pages.dashboard'))

            app.logger.warning(f"Failed login for '{raw_input}' from {request.remote_addr}")
            return _login_fail("Invalid credentials")

    return render_template('login.html')

@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


# ── Client Portal ─────────────────────────────────────────────────────────────

@bp.route('/client-login')
def client_login_page():
    return redirect(url_for('auth.login'))


@bp.route('/client/logout')
def client_logout():
    session.pop('client_id', None)
    session.pop('client_mobile', None)
    session.pop('client_display_name', None)
    return redirect(url_for('auth.login'))


@bp.route('/client/change-password', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def client_change_password():
    from app import _get_panel_ui_lang  # deferred: app-level helper, avoids circular import
    if 'client_id' not in session:
        return redirect(url_for('auth.login'))
    user = db.session.get(ClientPortalUser, session['client_id'])
    if not user or not user.enabled:
        session.pop('client_id', None)
        return redirect(url_for('auth.login'))

    error = None
    if request.method == 'POST':
        is_fa = _get_panel_ui_lang() == 'fa'
        new_pw = request.form.get('new_password', '')
        confirm_pw = request.form.get('confirm_password', '')
        if len(new_pw) < 8:
            error = 'رمز عبور باید حداقل ۸ کاراکتر باشد' if is_fa else 'Password must be at least 8 characters.'
        elif new_pw != confirm_pw:
            error = 'تکرار رمز عبور مطابقت ندارد' if is_fa else 'Password confirmation does not match.'
        elif new_pw in (user.mobile, user.mobile.lstrip('+'), user.mobile[3:]):
            error = 'رمز عبور نمی‌تواند همان شماره موبایل باشد' if is_fa else 'Password cannot be the same as the mobile number.'
        else:
            user.set_password(new_pw)
            user.must_change_password = False
            db.session.commit()
            return redirect(url_for('auth.client_portal'))

    return render_template('change_password_client.html', error=error, mobile=user.mobile)


@bp.route('/client/portal')
@client_portal_required
def client_portal():
    from app import format_app_datetime  # deferred: app-level helper, avoids circular import
    user = db.session.get(ClientPortalUser, session['client_id'])
    if not user or not user.enabled:
        session.pop('client_id', None)
        return redirect(url_for('auth.login'))
    return render_template(
        'client_portal.html', user=user,
        last_login_display=format_app_datetime(user.last_login) if user.last_login else None,
    )
