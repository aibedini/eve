"""Minimal, dependency-free WebAuthn (passkey) verification.

Implements just the subset the panel needs: a canonical CBOR decoder, COSE key
parsing for ES256/RS256, and the registration (attestation) and authentication
(assertion) ceremonies. The server deliberately stores only the public key, the
credential id, the signature counter and the algorithm; nothing secret.
"""
import base64
import hashlib
import hmac
import json
import secrets

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04
FLAG_ATTESTED_CREDENTIAL_DATA = 0x40
FLAG_EXTENSION_DATA = 0x80

COSE_ES256 = -7
COSE_RS256 = -257
SUPPORTED_ALGS = (COSE_ES256, COSE_RS256)


class WebAuthnError(ValueError):
    """Raised when a ceremony payload fails verification."""


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def b64url_decode(value) -> bytes:
    text = str(value or '').strip()
    if not text:
        raise WebAuthnError('missing base64url value')
    padded = text + '=' * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode('ascii'))
    except Exception as exc:
        raise WebAuthnError('invalid base64url value') from exc


def new_challenge() -> str:
    return b64url_encode(secrets.token_bytes(32))


def normalize_origin(origin) -> str:
    return str(origin or '').strip().rstrip('/').lower()


# ── Canonical CBOR (RFC 8949 definite-length subset used by WebAuthn) ─────────

def cbor_decode(data: bytes, offset: int = 0):
    if offset >= len(data):
        raise WebAuthnError('truncated CBOR')
    initial = data[offset]
    offset += 1
    major = initial >> 5
    info = initial & 0x1F
    if info < 24:
        value = info
    elif info == 24:
        if offset + 1 > len(data):
            raise WebAuthnError('truncated CBOR length')
        value = data[offset]
        offset += 1
    elif info == 25:
        value = int.from_bytes(data[offset:offset + 2], 'big')
        offset += 2
    elif info == 26:
        value = int.from_bytes(data[offset:offset + 4], 'big')
        offset += 4
    elif info == 27:
        value = int.from_bytes(data[offset:offset + 8], 'big')
        offset += 8
    elif info == 31:
        value = None  # indefinite length
    else:
        raise WebAuthnError(f'unsupported CBOR additional info {info}')

    if major == 0:
        return value, offset
    if major == 1:
        return -1 - value, offset
    if major == 2:  # byte string
        if value is None:
            chunks = b''
            while offset < len(data) and data[offset] != 0xFF:
                chunk, offset = cbor_decode(data, offset)
                chunks += chunk
            return chunks, offset + 1
        if offset + value > len(data):
            raise WebAuthnError('truncated byte string')
        return data[offset:offset + value], offset + value
    if major == 3:  # text string
        if value is None:
            text = ''
            while offset < len(data) and data[offset] != 0xFF:
                chunk, offset = cbor_decode(data, offset)
                text += chunk
            return text, offset + 1
        if offset + value > len(data):
            raise WebAuthnError('truncated text string')
        return data[offset:offset + value].decode('utf-8'), offset + value
    if major == 4:  # array
        items = []
        if value is None:
            while offset < len(data) and data[offset] != 0xFF:
                item, offset = cbor_decode(data, offset)
                items.append(item)
            return items, offset + 1
        for _ in range(value):
            item, offset = cbor_decode(data, offset)
            items.append(item)
        return items, offset
    if major == 5:  # map
        result = {}
        if value is None:
            while offset < len(data) and data[offset] != 0xFF:
                key, offset = cbor_decode(data, offset)
                val, offset = cbor_decode(data, offset)
                result[key] = val
            return result, offset + 1
        for _ in range(value):
            key, offset = cbor_decode(data, offset)
            val, offset = cbor_decode(data, offset)
            result[key] = val
        return result, offset
    if major == 6:  # tag: unwrap
        return cbor_decode(data, offset)
    if major == 7:
        if info == 20:
            return False, offset
        if info == 21:
            return True, offset
        if info in (22, 23):
            return None, offset
        raise WebAuthnError('unsupported CBOR simple/float value')
    raise WebAuthnError('unsupported CBOR major type')


# ── COSE keys ────────────────────────────────────────────────────────────────

def cose_public_key(cose) -> tuple[object, int]:
    if not isinstance(cose, dict):
        raise WebAuthnError('COSE key is not a map')
    kty = cose.get(1)
    alg = cose.get(3)
    if kty == 2:  # EC2
        crv = cose.get(-1)
        if crv != 1:
            raise WebAuthnError('only P-256 EC keys are supported')
        x = cose.get(-2)
        y = cose.get(-3)
        if not isinstance(x, bytes) or not isinstance(y, bytes):
            raise WebAuthnError('EC key coordinates missing')
        numbers = ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, 'big'), int.from_bytes(y, 'big'), ec.SECP256R1(),
        )
        if alg not in (None, COSE_ES256):
            raise WebAuthnError('EC key algorithm mismatch')
        return numbers.public_key(), COSE_ES256
    if kty == 3:  # RSA
        n = cose.get(-1)
        e = cose.get(-2)
        if not isinstance(n, bytes) or not isinstance(e, bytes):
            raise WebAuthnError('RSA key parameters missing')
        if alg not in (None, COSE_RS256):
            raise WebAuthnError('RSA key algorithm mismatch')
        return rsa.RSAPublicNumbers(int.from_bytes(e, 'big'), int.from_bytes(n, 'big')).public_key(), COSE_RS256
    raise WebAuthnError('unsupported COSE key type')


def public_key_to_pem(public_key) -> str:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode('ascii')


def pem_to_public_key(pem: str):
    return serialization.load_pem_public_key(str(pem or '').encode('ascii'))


def verify_signature(public_key, alg: int, signature: bytes, data: bytes) -> None:
    if alg == COSE_ES256:
        public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
    elif alg == COSE_RS256:
        public_key.verify(signature, data, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise WebAuthnError(f'unsupported signature algorithm {alg}')


# ── Ceremonies ───────────────────────────────────────────────────────────────

def _parse_client_data(client_data_json: bytes, expected_type: str, expected_challenge: str,
                       expected_origin: str) -> dict:
    try:
        client_data = json.loads((client_data_json or b'').decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebAuthnError('clientDataJSON is not valid JSON') from exc
    if client_data.get('type') != expected_type:
        raise WebAuthnError('wrong clientData type')
    if str(client_data.get('challenge') or '') != str(expected_challenge):
        raise WebAuthnError('challenge mismatch')
    if normalize_origin(client_data.get('origin')) != normalize_origin(expected_origin):
        raise WebAuthnError('origin mismatch')
    return client_data


def _parse_authenticator_data(auth_data: bytes):
    if len(auth_data) < 37:
        raise WebAuthnError('authenticatorData is too short')
    rp_id_hash = auth_data[:32]
    flags = auth_data[32]
    sign_count = int.from_bytes(auth_data[33:37], 'big')
    return rp_id_hash, flags, sign_count, auth_data[37:]


def _check_rp_id(rp_id_hash: bytes, rp_id: str) -> None:
    expected = hashlib.sha256(str(rp_id).encode('utf-8')).digest()
    if not hmac.compare_digest(rp_id_hash, expected):
        raise WebAuthnError('rpIdHash mismatch')


def verify_registration(*, client_data_json: bytes, attestation_object: bytes,
                        expected_challenge: str, rp_id: str, origin: str) -> dict:
    _parse_client_data(client_data_json, 'webauthn.create', expected_challenge, origin)
    try:
        decoded, _ = cbor_decode(attestation_object)
    except WebAuthnError:
        raise
    if not isinstance(decoded, dict):
        raise WebAuthnError('attestationObject is not a map')
    auth_data = decoded.get('authData')
    if not isinstance(auth_data, bytes):
        raise WebAuthnError('attestationObject has no authData')
    rp_id_hash, flags, sign_count, rest = _parse_authenticator_data(auth_data)
    _check_rp_id(rp_id_hash, rp_id)
    if not flags & FLAG_USER_PRESENT:
        raise WebAuthnError('user presence flag is not set')
    if not flags & FLAG_ATTESTED_CREDENTIAL_DATA:
        raise WebAuthnError('attested credential data is missing')
    if len(rest) < 18:
        raise WebAuthnError('attested credential data is truncated')
    aaguid = rest[:16]
    cred_len = int.from_bytes(rest[16:18], 'big')
    if len(rest) < 18 + cred_len:
        raise WebAuthnError('credential id is truncated')
    credential_id = rest[18:18 + cred_len]
    cose, _ = cbor_decode(rest[18 + cred_len:])
    public_key, alg = cose_public_key(cose)
    if not credential_id:
        raise WebAuthnError('empty credential id')
    return {
        'credential_id': credential_id,
        'public_key_pem': public_key_to_pem(public_key),
        'alg': alg,
        'sign_count': sign_count,
        'aaguid': aaguid.hex(),
        'user_verified': bool(flags & FLAG_USER_VERIFIED),
    }


def verify_assertion(*, client_data_json: bytes, authenticator_data: bytes, signature: bytes,
                     public_key_pem: str, alg: int, expected_challenge: str, rp_id: str,
                     origin: str, stored_sign_count: int) -> int:
    _parse_client_data(client_data_json, 'webauthn.get', expected_challenge, origin)
    rp_id_hash, flags, sign_count, _rest = _parse_authenticator_data(authenticator_data)
    _check_rp_id(rp_id_hash, rp_id)
    if not flags & FLAG_USER_PRESENT:
        raise WebAuthnError('user presence flag is not set')
    if int(stored_sign_count or 0) and sign_count and sign_count <= int(stored_sign_count):
        raise WebAuthnError('signature counter did not increase (possible cloned authenticator)')
    public_key = pem_to_public_key(public_key_pem)
    signed = authenticator_data + hashlib.sha256(client_data_json).digest()
    verify_signature(public_key, int(alg), signature, signed)
    return sign_count
