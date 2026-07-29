"""BNQO control-plane signing key and canonical-JSON signature helpers.

The CP holds one Ed25519 keypair, generated on first use and stored at
``instance/bnqo_cp_key`` (PEM, mode 0600) — contract §1. Configs and jobs are
signed over canonical JSON (UTF-8, keys sorted recursively, no whitespace,
``ensure_ascii=False``); agents authenticate with an Ed25519 signature over
``"<timestamp>\n" + raw body`` and a ±300 s timestamp-skew window.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import stat
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SIGNATURE_SKEW_SECONDS = 300

_KEY_LOCK = threading.Lock()
_CACHED_KEY = None


def _key_path():
    """Path of the CP key file; prefers the Flask instance dir when available."""
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            return os.path.join(current_app.instance_path, 'bnqo_cp_key')
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, 'instance', 'bnqo_cp_key')


def _load_or_create_key():
    """Lazily load (or generate + persist) the CP Ed25519 private key."""
    global _CACHED_KEY
    if _CACHED_KEY is not None:
        return _CACHED_KEY
    with _KEY_LOCK:
        if _CACHED_KEY is not None:
            return _CACHED_KEY
        path = _key_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            with open(path, 'rb') as fh:
                key = serialization.load_pem_private_key(fh.read(), password=None)
        else:
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            # O_EXCL: never overwrite a key another process just created.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, pem)
            finally:
                os.close(fd)
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass  # Windows: mode bits are advisory
        _CACHED_KEY = key
        return key


def get_cp_pubkey_b64():
    """Base64 of the raw 32-byte CP Ed25519 public key."""
    key = _load_or_create_key()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode('ascii')


def canonical_json(obj):
    """Canonical JSON bytes for signatures (contract §1)."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def sign_canonical(obj):
    """Sign the canonical JSON of ``obj`` with the CP key; returns base64."""
    key = _load_or_create_key()
    return base64.b64encode(key.sign(canonical_json(obj))).decode('ascii')


def cp_secret():
    """Stable CP-local secret (raw private key bytes) for derived values such
    as per-link session seeds. Never leaves the server."""
    key = _load_or_create_key()
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def session_seed_hex(link_id):
    """Deterministic per-link session seed (64 hex chars).

    Derived as HMAC-SHA256(key=cp_secret, msg="link:<id>") so BOTH agents of a
    link compute identical HKDF directional keys from the same seed
    (contract §2.2 key derivation requires a shared seed per link).
    """
    digest = hmac.new(cp_secret(), f'link:{int(link_id)}'.encode('utf-8'), hashlib.sha256)
    return digest.hexdigest()


def decode_pubkey(pubkey_b64):
    """Parse a base64 raw 32-byte Ed25519 public key; None when malformed."""
    try:
        raw = base64.b64decode(str(pubkey_b64 or ''), validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 32:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        return None


def verify_agent_signature(pubkey_b64, timestamp, body_bytes):
    """Verify an agent request signature over ``"<ts>\n" + body`` (contract §1).

    The base64 signature is taken from the current request's
    ``X-BNQO-Signature`` header; ``timestamp`` is the X-BNQO-Timestamp value
    (unix seconds, string or int). Returns False on any malformed input,
    timestamp skew > 300 s (replay check), or bad signature.
    """
    try:
        from flask import request
        signature_b64 = request.headers.get('X-BNQO-Signature')
    except Exception:
        signature_b64 = None
    return verify_with_signature(pubkey_b64, timestamp, signature_b64, body_bytes)


def verify_with_signature(pubkey_b64, timestamp, signature_b64, body_bytes):
    """Verify ``signature_b64`` over ``"<ts>\n" + body`` including replay check."""
    try:
        ts = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > SIGNATURE_SKEW_SECONDS:
        return False
    public_key = decode_pubkey(pubkey_b64)
    if public_key is None:
        return False
    try:
        signature = base64.b64decode(str(signature_b64 or ''), validate=True)
    except (binascii.Error, ValueError):
        return False
    signed_payload = f'{ts}\n'.encode('utf-8') + (body_bytes or b'')
    try:
        public_key.verify(signature, signed_payload)
        return True
    except Exception:
        return False
