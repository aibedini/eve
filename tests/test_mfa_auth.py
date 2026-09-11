"""Phase 3 tests: TOTP MFA, login gating, session registry and step-up."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from flask import jsonify  # noqa: E402

from app import (  # noqa: E402
    Admin, AdminMFABackupCode, AdminMFASetting, AdminSession, app, db,
)
from panel.routes.common import step_up_required  # noqa: E402
from panel.services import mfa, sessions  # noqa: E402
from panel.services.mfa import (  # noqa: E402
    generate_backup_codes, generate_totp_secret, hotp, provisioning_uri,
    totp_code, verify_totp,
)


# RFC 6238 test secret (ASCII "12345678901234567890") and vectors.
_RFC_SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
_RFC_VECTORS = [
    (59, 94287082),
    (1111111109, 7081804),
    (1111111111, 14050471),
    (1234567890, 89005924),
    (2000000000, 69279037),
    (20000000000, 65353130),
]


def _protected_view():
    return jsonify({'success': True})


app.add_url_rule('/_test/stepup', 'test_stepup',
                 step_up_required('test')(_protected_view), methods=['GET'])


class TotpTests(unittest.TestCase):
    def test_rfc6238_vectors(self):
        for moment, expected in _RFC_VECTORS:
            self.assertEqual(hotp(mfa._decode_secret(_RFC_SECRET), int(moment // 30), 8),
                             str(expected).zfill(8))

    def test_verify_accepts_window_and_rejects_replay(self):
        secret = generate_totp_secret()
        now = 1_700_000_000.0
        code = totp_code(secret, at_time=now)
        ok, counter = verify_totp(secret, code, at_time=now)
        self.assertTrue(ok)
        self.assertIsNotNone(counter)
        replay, _ = verify_totp(secret, code, at_time=now, last_counter=counter)
        self.assertFalse(replay)
        # A code from the previous step is still accepted once (clock drift)…
        previous = totp_code(secret, at_time=now - 30)
        ok_prev, _ = verify_totp(secret, previous, at_time=now)
        self.assertTrue(ok_prev)
        # …but the same step can never be accepted twice.
        ok_prev2, _ = verify_totp(secret, previous, at_time=now, last_counter=counter)
        self.assertFalse(ok_prev2)

    def test_verify_rejects_garbage(self):
        secret = generate_totp_secret()
        self.assertFalse(verify_totp(secret, '')[0])
        self.assertFalse(verify_totp(secret, 'abcdef')[0])
        self.assertFalse(verify_totp(secret, '12345')[0])

    def test_provisioning_uri_carries_secret_and_issuer(self):
        secret = generate_totp_secret()
        uri = provisioning_uri(secret, 'root', issuer='Eve')
        self.assertTrue(uri.startswith('otpauth://totp/'))
        self.assertIn(f'secret={secret}', uri)
        self.assertIn('issuer=Eve', uri)


class AdminAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        AdminMFABackupCode.query.delete()
        AdminMFASetting.query.delete()
        AdminSession.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(
            username='mfa-root', role='superadmin', is_superadmin=True, enabled=True,
        )
        self.admin.set_password('CorrectHorseBattery1!')
        db.session.add(self.admin)
        db.session.commit()
        self.client = app.test_client()

    def _enable_mfa(self):
        secret = generate_totp_secret()
        db.session.add(AdminMFASetting(
            admin_id=self.admin.id, totp_secret=secret, enabled=True,
            confirmed_at=datetime.utcnow(),
        ))
        db.session.commit()
        return secret

    def _login(self):
        return self.client.post('/login', json={
            'username': 'mfa-root', 'password': 'CorrectHorseBattery1!',
        })

    def test_superadmin_without_mfa_must_enrol(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get('mfa_enrollment_required'))
        # No privileged session is granted before MFA completes.
        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_id', sess)
            self.assertIn('mfa_pending', sess)

    def test_enrolment_confirmation_enables_mfa_and_grants_session(self):
        self._login()
        self.client.get('/mfa/setup')  # generates and stores the secret
        setting = AdminMFASetting.query.filter_by(admin_id=self.admin.id).one()
        code = totp_code(setting.totp_secret)
        confirm = self.client.post('/mfa/confirm', json={'code': code})
        self.assertEqual(confirm.status_code, 200, confirm.data)
        body = confirm.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(len(body['backup_codes']), 10)
        setting = AdminMFASetting.query.filter_by(admin_id=self.admin.id).one()
        self.assertTrue(setting.enabled)
        self.assertIsNotNone(setting.confirmed_at)
        with self.client.session_transaction() as sess:
            self.assertIn('admin_id', sess)
            self.assertIn(sessions.SESSION_TOKEN_KEY, sess)
        listing = self.client.get('/api/sessions')
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.get_json()['sessions']), 1)

    def test_enrolled_superadmin_gets_challenge_and_bad_code_fails(self):
        secret = self._enable_mfa()
        response = self._login()
        self.assertTrue(response.get_json().get('mfa_required'))
        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_id', sess)
        bad = self.client.post('/mfa/verify', json={'code': '000000'})
        self.assertEqual(bad.status_code, 401)
        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_id', sess)
        good = self.client.post('/mfa/verify', json={'code': totp_code(secret)})
        self.assertEqual(good.status_code, 200, good.data)
        with self.client.session_transaction() as sess:
            self.assertIn('admin_id', sess)

    def test_backup_code_is_single_use(self):
        self._enable_mfa()
        codes = ['ABCDE12345']
        from panel.security import hash_bearer_token
        db.session.add(AdminMFABackupCode(
            admin_id=self.admin.id,
            code_hash=hash_bearer_token(codes[0], f'mfa-backup:{self.admin.id}'),
        ))
        db.session.commit()
        from panel.routes.auth import _consume_mfa_code
        self.assertTrue(_consume_mfa_code(self.admin, 'abcde-12345'))
        db.session.commit()
        self.assertFalse(_consume_mfa_code(self.admin, 'ABCDE12345'))

    def test_logout_revokes_the_registry_session(self):
        secret = self._enable_mfa()
        self._login()
        self.client.post('/mfa/verify', json={'code': totp_code(secret)})
        with self.client.session_transaction() as sess:
            token = sess.get(sessions.SESSION_TOKEN_KEY)
        self.assertIsNotNone(sessions.resolve_session(token))
        self.client.get('/logout')
        self.assertIsNone(sessions.resolve_session(token))

    def test_idle_and_absolute_timeouts_and_revocation(self):
        token, row = sessions.create_session(self.admin, mfa_verified=True)
        db.session.commit()
        self.assertIsNotNone(sessions.resolve_session(token))
        row.last_seen_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        self.assertIsNone(sessions.resolve_session(token))  # 45 min idle for superadmin
        row.last_seen_at = datetime.utcnow()
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.session.commit()
        self.assertIsNone(sessions.resolve_session(token))  # absolute lifetime
        row.expires_at = datetime.utcnow() + timedelta(hours=1)
        db.session.commit()
        self.assertIsNotNone(sessions.resolve_session(token))
        self.assertTrue(sessions.revoke_session(token))
        db.session.commit()
        self.assertIsNone(sessions.resolve_session(token))

    def test_revoke_other_sessions_keeps_current(self):
        keep_token, _ = sessions.create_session(self.admin, mfa_verified=True)
        other_token, _ = sessions.create_session(self.admin, mfa_verified=True)
        db.session.commit()
        revoked = sessions.revoke_other_sessions(self.admin.id, keep_token)
        db.session.commit()
        self.assertEqual(revoked, 1)
        self.assertIsNotNone(sessions.resolve_session(keep_token))
        self.assertIsNone(sessions.resolve_session(other_token))

    def test_step_up_is_required_and_satisfied_by_a_code(self):
        secret = self._enable_mfa()
        token, row = sessions.create_session(self.admin, mfa_verified=True)
        db.session.commit()
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
            sess[sessions.SESSION_TOKEN_KEY] = token
        blocked = self.client.get('/_test/stepup')
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.get_json()['code'], 'step_up_required')
        verified = self.client.post('/api/mfa/step-up', json={'code': totp_code(secret)})
        self.assertEqual(verified.status_code, 200, verified.data)
        allowed = self.client.get('/_test/stepup')
        self.assertEqual(allowed.status_code, 200)


if __name__ == '__main__':
    unittest.main()
