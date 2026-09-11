"""Authentication, MFA, session management and client-portal routes."""
import base64
import io
import os
from datetime import datetime

import qrcode
from flask import (
    Blueprint, jsonify, redirect, render_template, request, session, url_for,
)
from sqlalchemy import func

from panel.core.phone import _extract_iran_mobile_from_text
from panel.extensions import db, limiter
from panel.models import (
    Admin, AdminMFABackupCode, AdminMFASetting, AdminSession,
    AdminWebAuthnCredential, ClientPortalUser,
)
from panel.routes.common import (
    client_portal_required, current_admin, current_session, login_required,
    mfa_required_for,
)
from panel.security import client_ip, hash_bearer_token
from panel.services.mfa import (
    generate_backup_codes, generate_totp_secret, normalize_backup_code,
    provisioning_uri, verify_totp,
)
from panel.services.sessions import (
    SESSION_TOKEN_KEY, create_session, list_sessions, mark_step_up,
    revoke_other_sessions, revoke_session,
)
from panel.services.webauthn import (
    COSE_ES256, COSE_RS256, WebAuthnError, b64url_decode, b64url_encode,
    new_challenge as webauthn_new_challenge, verify_assertion, verify_registration,
)

bp = Blueprint('auth', __name__)

# A login that passed the password check but still owes MFA is kept pending for
# a short time; no admin_id is written to the cookie until MFA succeeds.
MFA_PENDING_TTL_SECONDS = 300


def _login_fail(msg: str):
    """Return appropriate login failure response."""
    if request.is_json:
        return jsonify({"success": False, "error": msg})
    return render_template('login.html', error=msg)


def _audit(action, admin, meta=None) -> None:
    try:
        from app import _log_audit
        _log_audit(action, admin, actor=admin, meta=meta)
    except Exception:
        pass


# ── MFA helpers ──────────────────────────────────────────────────────────────

def _mfa_setting(admin_id) -> AdminMFASetting:
    setting = AdminMFASetting.query.filter_by(admin_id=admin_id).first()
    if setting is None:
        setting = AdminMFASetting(admin_id=admin_id)
        db.session.add(setting)
        db.session.flush()
    return setting


def _mfa_confirmed(admin_id) -> bool:
    setting = AdminMFASetting.query.filter_by(admin_id=admin_id).first()
    return bool(setting and setting.enabled and setting.confirmed_at and setting.totp_secret)


def _mfa_configured(admin_id) -> bool:
    """True when the account has at least one usable factor (TOTP or a passkey)."""
    if _mfa_confirmed(admin_id):
        return True
    return AdminWebAuthnCredential.query.filter_by(admin_id=admin_id).first() is not None


def _qr_data_uri(text: str) -> str:
    try:
        image = qrcode.make(text)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    except Exception:
        return ''


def _issue_backup_codes(admin_id) -> list[str]:
    AdminMFABackupCode.query.filter_by(admin_id=admin_id).delete()
    codes = generate_backup_codes(10)
    for code in codes:
        db.session.add(AdminMFABackupCode(
            admin_id=admin_id,
            code_hash=hash_bearer_token(code, f'mfa-backup:{admin_id}'),
        ))
    return codes


def _consume_mfa_code(admin, code) -> bool:
    """Accept a TOTP (advancing the replay watermark) or a single-use backup code."""
    setting = AdminMFASetting.query.filter_by(admin_id=admin.id).first()
    if not setting or not setting.enabled or not setting.totp_secret:
        return False
    ok, counter = verify_totp(setting.totp_secret, code, last_counter=setting.last_counter)
    if ok:
        setting.last_counter = counter
        return True
    normalized = normalize_backup_code(code)
    if not normalized:
        return False
    digest = hash_bearer_token(normalized, f'mfa-backup:{admin.id}')
    row = AdminMFABackupCode.query.filter_by(
        admin_id=admin.id, code_hash=digest, used_at=None,
    ).first()
    if row is None:
        return False
    row.used_at = datetime.utcnow()
    return True


def _pending_start(admin, *, enrolled: bool) -> None:
    session['mfa_pending'] = {
        'admin_id': admin.id,
        'at': datetime.utcnow().isoformat(),
        'enrolled': bool(enrolled),
    }


def _pending_admin():
    data = session.get('mfa_pending')
    if not isinstance(data, dict):
        return None
    try:
        started = datetime.fromisoformat(str(data.get('at')))
    except (TypeError, ValueError):
        session.pop('mfa_pending', None)
        return None
    if (datetime.utcnow() - started).total_seconds() > MFA_PENDING_TTL_SECONDS:
        session.pop('mfa_pending', None)
        return None
    admin = db.session.get(Admin, data.get('admin_id'))
    if admin is None or not admin.enabled:
        session.pop('mfa_pending', None)
        return None
    return admin


def _establish_session(admin, *, mfa_verified: bool, commit: bool = True) -> str:
    token, _row = create_session(
        admin,
        ip=client_ip(),
        user_agent=request.headers.get('User-Agent'),
        mfa_verified=mfa_verified,
    )
    session.clear()
    session.permanent = False  # the registry row, not the cookie age, is authoritative
    session[SESSION_TOKEN_KEY] = token
    session['admin_id'] = admin.id
    session['admin_username'] = admin.username
    session['role'] = admin.role
    session['is_superadmin'] = bool(admin.role == 'superadmin' or admin.is_superadmin)
    if commit:
        db.session.commit()
    return token


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
                app.logger.warning("Login — unknown client mobile %s from %s", mobile, client_ip())
                return _login_fail("Invalid credentials")

            if client.is_locked():
                remaining = max(1, int((client.locked_until - datetime.utcnow()).total_seconds() / 60) + 1)
                return _login_fail(f"Account locked. Try again in {remaining} minute(s).")

            if not client.check_password(password):
                client.record_failed()
                db.session.commit()
                app.logger.warning("Login — wrong password for client %s from %s (attempt %s)", mobile, client_ip(), client.failed_attempts)
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
                admin.last_login = datetime.utcnow()
                # MFA gate: a superadmin without a confirmed factor must enrol
                # before a session is granted; a confirmed factor must be
                # presented. Only then is admin_id written to the cookie.
                if mfa_required_for(admin) and not _mfa_configured(admin.id):
                    _pending_start(admin, enrolled=False)
                    _audit('auth.login.mfa_enrollment_required', admin)
                    db.session.commit()
                    dest = url_for('auth.mfa_setup_page')
                    return (jsonify({"success": True, "mfa_enrollment_required": True, "redirect": dest})
                            if request.is_json else redirect(dest))
                if _mfa_configured(admin.id):
                    _pending_start(admin, enrolled=True)
                    _audit('auth.login.mfa_required', admin)
                    db.session.commit()
                    dest = url_for('auth.mfa_challenge')
                    return (jsonify({"success": True, "mfa_required": True, "redirect": dest})
                            if request.is_json else redirect(dest))
                _establish_session(admin, mfa_verified=False)
                _audit('auth.login.success', admin, meta={'mfa': 'not_required'})
                db.session.commit()
                return jsonify({"success": True}) if request.is_json else redirect(url_for('pages.dashboard'))

            app.logger.warning("Failed login for '%s' from %s", raw_input, client_ip())
            _audit('auth.login.failed', None, meta={'username': raw_input[:64], 'ip': client_ip()})
            db.session.commit()
            return _login_fail("Invalid credentials")

    return render_template('login.html')

@bp.route('/logout')
def logout():
    token = session.get(SESSION_TOKEN_KEY)
    if token:
        revoke_session(token)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    session.clear()
    return redirect(url_for('auth.login'))


# ── MFA challenge / enrollment / step-up / sessions ──────────────────────────

@bp.route('/mfa')
def mfa_challenge():
    admin = _pending_admin()
    if not admin:
        return redirect(url_for('auth.login'))
    if not _mfa_configured(admin.id):
        return redirect(url_for('auth.mfa_setup_page'))
    return render_template(
        'mfa.html', error=None,
        passkey=AdminWebAuthnCredential.query.filter_by(admin_id=admin.id).first() is not None,
    )


@bp.route('/mfa/verify', methods=['POST'])
@limiter.limit("10 per minute")
def mfa_verify():
    admin = _pending_admin()
    if not admin:
        return redirect(url_for('auth.login'))
    payload = (request.get_json(silent=True) or {}) if request.is_json else request.form
    code = (payload.get('code') or '').strip()
    if not _consume_mfa_code(admin, code):
        _audit('auth.mfa.failed', admin)
        db.session.commit()
        if request.is_json:
            return jsonify({"success": False, "error": "Invalid code"}), 401
        return render_template('mfa.html', error='Invalid code. Try again.'), 401
    _establish_session(admin, mfa_verified=True)
    _audit('auth.mfa.success', admin)
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True, "redirect": url_for('pages.dashboard')})
    return redirect(url_for('pages.dashboard'))


@bp.route('/mfa/setup')
def mfa_setup_page():
    admin = _pending_admin()
    if not admin:
        return redirect(url_for('auth.login'))
    setting = _mfa_setting(admin.id)
    if setting.confirmed_at:
        return redirect(url_for('auth.mfa_challenge'))
    if not setting.totp_secret:
        setting.totp_secret = generate_totp_secret()
        db.session.commit()
    uri = provisioning_uri(setting.totp_secret, admin.username)
    return render_template(
        'mfa_setup.html', secret=setting.totp_secret, uri=uri,
        qr=_qr_data_uri(uri), error=None,
    )


@bp.route('/mfa/confirm', methods=['POST'])
@limiter.limit("10 per minute")
def mfa_confirm():
    admin = _pending_admin()
    if not admin:
        return redirect(url_for('auth.login'))
    payload = (request.get_json(silent=True) or {}) if request.is_json else request.form
    code = (payload.get('code') or '').strip()
    setting = _mfa_setting(admin.id)
    if not setting.totp_secret:
        setting.totp_secret = generate_totp_secret()
        db.session.flush()
    ok, counter = verify_totp(setting.totp_secret, code)
    if not ok:
        _audit('auth.mfa.enroll_failed', admin)
        db.session.commit()
        uri = provisioning_uri(setting.totp_secret, admin.username)
        if request.is_json:
            return jsonify({"success": False, "error": "Invalid code"}), 400
        return render_template(
            'mfa_setup.html', secret=setting.totp_secret, uri=uri,
            qr=_qr_data_uri(uri), error='Invalid code. Try again.',
        ), 400
    setting.enabled = True
    setting.confirmed_at = datetime.utcnow()
    setting.last_counter = counter
    codes = _issue_backup_codes(admin.id)
    _establish_session(admin, mfa_verified=True, commit=False)
    _audit('auth.mfa.enrolled', admin, meta={'backup_codes': len(codes)})
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True, "backup_codes": codes})
    return render_template('mfa_backup_codes.html', codes=codes)


@bp.route('/api/mfa/step-up', methods=['POST'])
@limiter.limit("10 per minute")
@login_required
def mfa_step_up():
    admin = current_admin()
    payload = (request.get_json(silent=True) or {}) if request.is_json else request.form
    code = (payload.get('code') or '').strip()
    if not _mfa_confirmed(admin.id):
        return jsonify({"success": False, "error": "MFA is not configured for this account"}), 400
    if not _consume_mfa_code(admin, code):
        _audit('auth.mfa.step_up_failed', admin)
        db.session.commit()
        return jsonify({"success": False, "error": "Invalid code"}), 401
    mark_step_up(current_session())
    _audit('auth.mfa.step_up', admin)
    db.session.commit()
    return jsonify({"success": True})


@bp.route('/api/sessions', methods=['GET'])
@login_required
def api_list_sessions():
    admin = current_admin()
    rows = list_sessions(admin.id, session.get(SESSION_TOKEN_KEY))
    return jsonify({
        "success": True,
        "sessions": [{**row.to_safe_dict(), 'current': is_current}
                     for row, is_current in rows],
    })


@bp.route('/api/sessions/<int:session_id>/revoke', methods=['POST'])
@login_required
def api_revoke_session(session_id):
    admin = current_admin()
    row = db.session.get(AdminSession, session_id)
    if row is None or row.admin_id != admin.id:
        return jsonify({"success": False, "error": "Session not found"}), 404
    row.revoked_at = datetime.utcnow()
    _audit('auth.session.revoke', admin, meta={'session_id': session_id})
    db.session.commit()
    return jsonify({"success": True})


@bp.route('/api/sessions/revoke-others', methods=['POST'])
@login_required
def api_revoke_other_sessions():
    admin = current_admin()
    count = revoke_other_sessions(admin.id, session.get(SESSION_TOKEN_KEY))
    _audit('auth.session.revoke_others', admin, meta={'revoked': count})
    db.session.commit()
    return jsonify({"success": True, "revoked": count})


@bp.route('/security')
@login_required
def security_page():
    """Authenticated page for passkey registration and session management."""
    return render_template('security.html')


# ── WebAuthn / passkeys ──────────────────────────────────────────────────────

WEBAUTHN_CHALLENGE_TTL_SECONDS = 300


def _webauthn_rp_id() -> str:
    configured = (os.environ.get('EVE_WEBAUTHN_RP_ID') or '').strip()
    return configured or (request.host or 'localhost').split(':')[0]


def _webauthn_origin() -> str:
    configured = (os.environ.get('EVE_WEBAUTHN_ORIGIN') or '').strip()
    return configured or request.host_url.rstrip('/')


def _webauthn_challenge_start(kind: str) -> str:
    challenge = webauthn_new_challenge()
    session['webauthn_challenge'] = {
        'value': challenge, 'at': datetime.utcnow().isoformat(), 'kind': kind,
    }
    return challenge


def _webauthn_challenge_read(kind: str):
    data = session.pop('webauthn_challenge', None)
    if not isinstance(data, dict) or data.get('kind') != kind:
        return None
    try:
        started = datetime.fromisoformat(str(data.get('at')))
    except (TypeError, ValueError):
        return None
    if (datetime.utcnow() - started).total_seconds() > WEBAUTHN_CHALLENGE_TTL_SECONDS:
        return None
    return str(data.get('value') or '') or None


def _admin_credentials(admin_id):
    return (AdminWebAuthnCredential.query
            .filter_by(admin_id=admin_id)
            .order_by(AdminWebAuthnCredential.id.asc())
            .all())


@bp.route('/api/webauthn/register/begin', methods=['POST'])
@login_required
def webauthn_register_begin():
    admin = current_admin()
    challenge = _webauthn_challenge_start('register')
    exclude = [{'type': 'public-key', 'id': row.credential_id} for row in _admin_credentials(admin.id)]
    return jsonify({'success': True, 'publicKey': {
        'challenge': challenge,
        'rp': {'name': 'Eve', 'id': _webauthn_rp_id()},
        'user': {
            'id': b64url_encode(str(admin.id).encode('utf-8')),
            'name': admin.username,
            'displayName': admin.username,
        },
        'pubKeyCredParams': [{'type': 'public-key', 'alg': alg} for alg in (COSE_ES256, COSE_RS256)],
        'timeout': 60000,
        'attestation': 'none',
        'authenticatorSelection': {'residentKey': 'preferred', 'userVerification': 'preferred'},
        'excludeCredentials': exclude,
    }})


@bp.route('/api/webauthn/register/complete', methods=['POST'])
@limiter.limit("20 per minute")
@login_required
def webauthn_register_complete():
    admin = current_admin()
    data = request.get_json(silent=True) or {}
    challenge = _webauthn_challenge_read('register')
    if not challenge:
        return jsonify({'success': False, 'error': 'Registration challenge expired'}), 400
    response = data.get('response') or {}
    try:
        verified = verify_registration(
            client_data_json=b64url_decode(response.get('clientDataJSON')),
            attestation_object=b64url_decode(response.get('attestationObject')),
            expected_challenge=challenge, rp_id=_webauthn_rp_id(), origin=_webauthn_origin(),
        )
    except WebAuthnError as exc:
        _audit('auth.webauthn.register_failed', admin, meta={'error': str(exc)[:120]})
        db.session.commit()
        return jsonify({'success': False, 'error': f'Registration rejected: {exc}'}), 400
    credential_id = b64url_encode(verified['credential_id'])
    if AdminWebAuthnCredential.query.filter_by(credential_id=credential_id).first():
        return jsonify({'success': False, 'error': 'Credential is already registered'}), 409
    transports = data.get('transports') or response.get('transports') or []
    row = AdminWebAuthnCredential(
        admin_id=admin.id,
        credential_id=credential_id,
        public_key_pem=verified['public_key_pem'],
        alg=verified['alg'],
        sign_count=verified['sign_count'],
        aaguid=verified['aaguid'],
        name=str(data.get('name') or 'Passkey')[:120],
        transports=','.join(str(item)[:16] for item in list(transports)[:5]) or None,
    )
    db.session.add(row)
    _audit('auth.webauthn.registered', admin, meta={'alg': verified['alg']})
    db.session.commit()
    return jsonify({'success': True, 'credential': row.to_safe_dict()})


@bp.route('/api/webauthn/login/begin', methods=['POST'])
def webauthn_login_begin():
    admin = _pending_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'No pending authentication'}), 401
    credentials = _admin_credentials(admin.id)
    if not credentials:
        return jsonify({'success': False, 'error': 'No passkey is registered for this account'}), 404
    challenge = _webauthn_challenge_start('login')
    return jsonify({'success': True, 'publicKey': {
        'challenge': challenge,
        'rpId': _webauthn_rp_id(),
        'timeout': 60000,
        'userVerification': 'preferred',
        'allowCredentials': [{'type': 'public-key', 'id': row.credential_id} for row in credentials],
    }})


@bp.route('/api/webauthn/login/complete', methods=['POST'])
@limiter.limit("20 per minute")
def webauthn_login_complete():
    admin = _pending_admin()
    if not admin:
        return jsonify({'success': False, 'error': 'No pending authentication'}), 401
    data = request.get_json(silent=True) or {}
    challenge = _webauthn_challenge_read('login')
    if not challenge:
        return jsonify({'success': False, 'error': 'Authentication challenge expired'}), 400
    credential_id = str(data.get('id') or data.get('rawId') or '').strip()
    row = AdminWebAuthnCredential.query.filter_by(
        admin_id=admin.id, credential_id=credential_id,
    ).first()
    if row is None:
        return jsonify({'success': False, 'error': 'Unknown credential'}), 404
    response = data.get('response') or {}
    try:
        new_count = verify_assertion(
            client_data_json=b64url_decode(response.get('clientDataJSON')),
            authenticator_data=b64url_decode(response.get('authenticatorData')),
            signature=b64url_decode(response.get('signature')),
            public_key_pem=row.public_key_pem, alg=row.alg,
            expected_challenge=challenge, rp_id=_webauthn_rp_id(), origin=_webauthn_origin(),
            stored_sign_count=row.sign_count or 0,
        )
    except WebAuthnError as exc:
        _audit('auth.webauthn.login_failed', admin, meta={'error': str(exc)[:120]})
        db.session.commit()
        return jsonify({'success': False, 'error': f'Authentication rejected: {exc}'}), 401
    row.sign_count = new_count
    row.last_used_at = datetime.utcnow()
    _establish_session(admin, mfa_verified=True, commit=False)
    _audit('auth.webauthn.success', admin)
    db.session.commit()
    return jsonify({'success': True, 'redirect': url_for('pages.dashboard')})


@bp.route('/api/webauthn/credentials', methods=['GET'])
@login_required
def webauthn_list_credentials():
    admin = current_admin()
    return jsonify({'success': True,
                    'credentials': [row.to_safe_dict() for row in _admin_credentials(admin.id)]})


@bp.route('/api/webauthn/credentials/<int:credential_row_id>', methods=['DELETE'])
@login_required
def webauthn_delete_credential(credential_row_id):
    admin = current_admin()
    row = db.session.get(AdminWebAuthnCredential, credential_row_id)
    if row is None or row.admin_id != admin.id:
        return jsonify({'success': False, 'error': 'Credential not found'}), 404
    db.session.delete(row)
    _audit('auth.webauthn.removed', admin, meta={'credential_row_id': credential_row_id})
    db.session.commit()
    return jsonify({'success': True})


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
