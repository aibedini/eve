"""Phase 3b tests: WebAuthn/passkey registration and authentication."""
import hashlib
import json
import os
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}")
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from app import (  # noqa: E402
    Admin, AdminMFABackupCode, AdminMFASetting, AdminSession,
    AdminWebAuthnCredential, app, db,
)
from panel.services import webauthn as wa  # noqa: E402

RP_ID = 'example.com'
ORIGIN = 'https://example.com'


# ── tiny canonical CBOR encoder (mirror of the decoder subset) ───────────────

def _head(major, value):
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 256:
        return bytes([(major << 5) | 24, value])
    if value < 65536:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, 'big')
    return bytes([(major << 5) | 26]) + value.to_bytes(4, 'big')


def cbor_encode(obj):
    if isinstance(obj, bool):
        return bytes([0xF5 if obj else 0xF4])
    if isinstance(obj, int):
        return _head(0, obj) if obj >= 0 else _head(1, -1 - obj)
    if isinstance(obj, bytes):
        return _head(2, len(obj)) + obj
    if isinstance(obj, str):
        raw = obj.encode('utf-8')
        return _head(3, len(raw)) + raw
    if isinstance(obj, dict):
        out = _head(5, len(obj))
        for key, value in obj.items():
            out += cbor_encode(key) + cbor_encode(value)
        return out
    raise TypeError(f'unsupported CBOR value: {type(obj)!r}')


class SimulatedAuthenticator:
    def __init__(self, rp_id=RP_ID, origin=ORIGIN):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.rp_id = rp_id
        self.origin = origin
        self.credential_id = os.urandom(16)
        self.sign_count = 0

    def registration_payload(self, challenge):
        numbers = self.private_key.public_key().public_numbers()
        cose = {
            1: 2, 3: -7, -1: 1,
            -2: numbers.x.to_bytes(32, 'big'),
            -3: numbers.y.to_bytes(32, 'big'),
        }
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([0x41])  # UP + AT
            + self.sign_count.to_bytes(4, 'big')
            + bytes(16)
            + len(self.credential_id).to_bytes(2, 'big')
            + self.credential_id
            + cbor_encode(cose)
        )
        attestation = cbor_encode({'fmt': 'none', 'authData': auth_data, 'attStmt': {}})
        client_data = json.dumps({
            'type': 'webauthn.create', 'challenge': challenge, 'origin': self.origin,
        }).encode('utf-8')
        return {
            'id': wa.b64url_encode(self.credential_id),
            'rawId': wa.b64url_encode(self.credential_id),
            'type': 'public-key',
            'response': {
                'clientDataJSON': wa.b64url_encode(client_data),
                'attestationObject': wa.b64url_encode(attestation),
            },
            'transports': ['internal'],
        }

    def assertion_payload(self, challenge, origin=None, sign_count=None):
        if sign_count is None:
            self.sign_count += 1
            sign_count = self.sign_count
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([0x01])  # UP
            + int(sign_count).to_bytes(4, 'big')
        )
        client_data = json.dumps({
            'type': 'webauthn.get', 'challenge': challenge,
            'origin': origin if origin is not None else self.origin,
        }).encode('utf-8')
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()),
        )
        return {
            'id': wa.b64url_encode(self.credential_id),
            'response': {
                'clientDataJSON': wa.b64url_encode(client_data),
                'authenticatorData': wa.b64url_encode(auth_data),
                'signature': wa.b64url_encode(signature),
            },
        }


class WebAuthnServiceTests(unittest.TestCase):
    def test_registration_then_assertion_round_trip(self):
        authenticator = SimulatedAuthenticator()
        challenge = wa.new_challenge()
        payload = authenticator.registration_payload(challenge)
        verified = wa.verify_registration(
            client_data_json=wa.b64url_decode(payload['response']['clientDataJSON']),
            attestation_object=wa.b64url_decode(payload['response']['attestationObject']),
            expected_challenge=challenge, rp_id=RP_ID, origin=ORIGIN,
        )
        self.assertEqual(verified['alg'], wa.COSE_ES256)
        self.assertEqual(wa.b64url_encode(verified['credential_id']), payload['id'])
        self.assertIn('BEGIN PUBLIC KEY', verified['public_key_pem'])

        assertion = authenticator.assertion_payload(challenge)
        count = wa.verify_assertion(
            client_data_json=wa.b64url_decode(assertion['response']['clientDataJSON']),
            authenticator_data=wa.b64url_decode(assertion['response']['authenticatorData']),
            signature=wa.b64url_decode(assertion['response']['signature']),
            public_key_pem=verified['public_key_pem'], alg=verified['alg'],
            expected_challenge=challenge, rp_id=RP_ID, origin=ORIGIN, stored_sign_count=0,
        )
        self.assertEqual(count, 1)

    def test_assertion_rejects_wrong_challenge_origin_and_counter(self):
        authenticator = SimulatedAuthenticator()
        challenge = wa.new_challenge()
        registration = authenticator.registration_payload(challenge)
        verified = wa.verify_registration(
            client_data_json=wa.b64url_decode(registration['response']['clientDataJSON']),
            attestation_object=wa.b64url_decode(registration['response']['attestationObject']),
            expected_challenge=challenge, rp_id=RP_ID, origin=ORIGIN,
        )
        base = dict(public_key_pem=verified['public_key_pem'], alg=verified['alg'],
                    rp_id=RP_ID, origin=ORIGIN, stored_sign_count=0)
        wrong_challenge = authenticator.assertion_payload(wa.new_challenge())
        with self.assertRaises(wa.WebAuthnError):
            wa.verify_assertion(
                client_data_json=wa.b64url_decode(wrong_challenge['response']['clientDataJSON']),
                authenticator_data=wa.b64url_decode(wrong_challenge['response']['authenticatorData']),
                signature=wa.b64url_decode(wrong_challenge['response']['signature']),
                expected_challenge=challenge, **base,
            )
        wrong_origin = authenticator.assertion_payload(challenge, origin='https://evil.example')
        with self.assertRaises(wa.WebAuthnError):
            wa.verify_assertion(
                client_data_json=wa.b64url_decode(wrong_origin['response']['clientDataJSON']),
                authenticator_data=wa.b64url_decode(wrong_origin['response']['authenticatorData']),
                signature=wa.b64url_decode(wrong_origin['response']['signature']),
                expected_challenge=challenge, **base,
            )
        # A counter that does not increase is treated as a cloned authenticator.
        replay = authenticator.assertion_payload(challenge, sign_count=1)
        with self.assertRaises(wa.WebAuthnError):
            wa.verify_assertion(
                client_data_json=wa.b64url_decode(replay['response']['clientDataJSON']),
                authenticator_data=wa.b64url_decode(replay['response']['authenticatorData']),
                signature=wa.b64url_decode(replay['response']['signature']),
                expected_challenge=challenge, **{**base, 'stored_sign_count': 1},
            )

    def test_registration_rejects_wrong_origin(self):
        authenticator = SimulatedAuthenticator(origin='https://evil.example')
        challenge = wa.new_challenge()
        payload = authenticator.registration_payload(challenge)
        with self.assertRaises(wa.WebAuthnError):
            wa.verify_registration(
                client_data_json=wa.b64url_decode(payload['response']['clientDataJSON']),
                attestation_object=wa.b64url_decode(payload['response']['attestationObject']),
                expected_challenge=challenge, rp_id=RP_ID, origin=ORIGIN,
            )

    def test_cbor_decoder_handles_indefinite_and_negative(self):
        encoded = bytes([0x9F]) + cbor_encode(-7) + cbor_encode(b'ab') + bytes([0xFF])
        decoded, offset = wa.cbor_decode(encoded)
        self.assertEqual(decoded, [-7, b'ab'])
        self.assertEqual(offset, len(encoded))


class WebAuthnRouteTests(unittest.TestCase):
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
        AdminWebAuthnCredential.query.delete()
        AdminMFABackupCode.query.delete()
        AdminMFASetting.query.delete()
        AdminSession.query.delete()
        Admin.query.delete()
        db.session.commit()
        self.admin = Admin(
            username='passkey-root', role='superadmin', is_superadmin=True, enabled=True,
        )
        self.admin.set_password('CorrectHorseBattery1!')
        db.session.add(self.admin)
        db.session.commit()
        self.client = app.test_client()

    def _register_passkey(self):
        with self.client.session_transaction() as sess:
            sess['admin_id'] = self.admin.id
        begun = self.client.post('/api/webauthn/register/begin').get_json()
        challenge = begun['publicKey']['challenge']
        authenticator = SimulatedAuthenticator(rp_id='localhost', origin='http://localhost')
        payload = authenticator.registration_payload(challenge)
        done = self.client.post('/api/webauthn/register/complete', json=payload)
        self.assertEqual(done.status_code, 200, done.data)
        return authenticator

    def test_register_and_login_with_passkey(self):
        authenticator = self._register_passkey()
        self.assertEqual(AdminWebAuthnCredential.query.filter_by(admin_id=self.admin.id).count(), 1)
        self.client.get('/logout')
        first = self.client.post('/login', json={
            'username': 'passkey-root', 'password': 'CorrectHorseBattery1!',
        }).get_json()
        self.assertTrue(first.get('mfa_required'), first)
        begun = self.client.post('/api/webauthn/login/begin')
        self.assertEqual(begun.status_code, 200, begun.data)
        challenge = begun.get_json()['publicKey']['challenge']
        assertion = authenticator.assertion_payload(challenge)
        done = self.client.post('/api/webauthn/login/complete', json=assertion)
        self.assertEqual(done.status_code, 200, done.data)
        with self.client.session_transaction() as sess:
            self.assertIn('admin_id', sess)
        self.assertEqual(self.client.get('/api/webauthn/credentials').status_code, 200)

    def test_login_rejects_assertion_for_unknown_credential(self):
        self._register_passkey()
        self.client.get('/logout')
        self.client.post('/login', json={
            'username': 'passkey-root', 'password': 'CorrectHorseBattery1!',
        })
        challenge = self.client.post('/api/webauthn/login/begin').get_json()['publicKey']['challenge']
        other = SimulatedAuthenticator(rp_id='localhost', origin='http://localhost')
        assertion = other.assertion_payload(challenge)
        response = self.client.post('/api/webauthn/login/complete', json=assertion)
        self.assertEqual(response.status_code, 404)
        with self.client.session_transaction() as sess:
            self.assertNotIn('admin_id', sess)


if __name__ == '__main__':
    unittest.main()
