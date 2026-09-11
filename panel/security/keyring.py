"""Versioned, domain-separated key management for stored secrets.

Every secret belongs to a logical domain (X-UI credentials, messaging, finance,
subscriptions, MFA, backups, generic). Each domain has its own key per version,
so ciphertext from one domain is useless in another. Values carry an explicit
version marker (enc:v1:, enc:v2: ...):

- v1 is the historical key derived from SERVER_PASSWORD_KEY so every value
  written before this change still decrypts.
- v2 (the default once a master key exists) is derived per domain with HKDF, or
  taken from an explicit EVE_KEY_<DOMAIN>_V2 environment variable, which is the
  seam a KMS/Vault integration would use.

Rotation is therefore: add the next version's key, let new writes use it, then
re-encrypt the old rows (panel/services/secret_rotation.py). Reads always fall
back through older versions, so rotation needs no downtime.
"""
import base64
import os
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DOMAINS = (
    'generic', 'xui_credentials', 'messaging', 'finance', 'subscriptions',
    'mfa', 'backup',
)
MAX_VERSION = 9
LEGACY_PREFIX = 'enc:'
_VERSION_RE = re.compile(r'^enc:v(\d+):')
DEV_FALLBACK = b'eve-development-only-master-key-00000000000000000000000000'


def _is_dev_mode() -> bool:
    env = (os.environ.get('FLASK_ENV') or os.environ.get('ENV') or '').strip().lower()
    debug = (os.environ.get('DEBUG') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    return debug or env in {'development', 'dev', 'test', 'testing'}


def _master_key() -> str:
    return (os.environ.get('SERVER_PASSWORD_KEY') or os.environ.get('EVE_MASTER_KEY') or '').strip()


def _decode(raw: str):
    try:
        return base64.urlsafe_b64decode(str(raw).encode('ascii'))
    except Exception:
        return None


@lru_cache(maxsize=256)
def _key_b64(domain: str, version: int):
    """Return the URL-safe base64 Fernet key for a domain/version, or None."""
    domain = domain if domain in DOMAINS else 'generic'
    if version < 1:
        return None
    explicit = (os.environ.get(f'EVE_KEY_{domain.upper()}_V{version}') or '').strip()
    if explicit:
        return explicit if _decode(explicit) else None
    master = _master_key()
    if not master:
        if version == 1 and _is_dev_mode():
            return base64.urlsafe_b64encode(DEV_FALLBACK).decode('ascii')
        return None
    if version == 1:
        # Historical key: the raw SERVER_PASSWORD_KEY bytes, unchanged.
        return master if _decode(master) else None
    key_material = _decode(master)
    if not key_material:
        return None
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=f'eve:{domain}:v{version}'.encode('utf-8'),
    ).derive(key_material)
    return base64.urlsafe_b64encode(derived).decode('ascii')


def current_version(domain: str = 'generic') -> int:
    """Highest version a domain can write with."""
    explicit = []
    for version in range(2, MAX_VERSION + 1):
        if (os.environ.get(f'EVE_KEY_{domain.upper()}_V{version}') or '').strip():
            explicit.append(version)
    if explicit:
        return max(explicit)
    if _master_key():
        return 2
    return 1


def fernet(domain: str = 'generic', version=None):
    domain = domain if domain in DOMAINS else 'generic'
    version = current_version(domain) if version is None else int(version)
    key = _key_b64(domain, version)
    if not key:
        return None
    try:
        return Fernet(key)
    except Exception:
        return None


def is_envelope(value) -> bool:
    return str(value or '').startswith(LEGACY_PREFIX)


def split_envelope(value):
    """Return (version, token) for an envelope, or (None, None)."""
    raw = str(value or '')
    if not raw.startswith(LEGACY_PREFIX):
        return None, None
    match = _VERSION_RE.match(raw)
    if match:
        return int(match.group(1)), raw[match.end():]
    return 1, raw[len(LEGACY_PREFIX):]


def encrypt(value, domain: str = 'generic') -> str:
    """Encrypt a secret at the domain's current version.

    In development without a master key this falls back to plaintext so local
    fixtures keep working; production startup already requires the key.
    """
    raw = str(value or '')
    if not raw or is_envelope(raw):
        return raw
    version = current_version(domain)
    cipher = fernet(domain, version)
    if cipher is None:
        if _is_dev_mode():
            return raw
        raise RuntimeError('SERVER_PASSWORD_KEY is required to protect stored secrets')
    token = cipher.encrypt(raw.encode('utf-8')).decode('ascii')
    return f'{LEGACY_PREFIX}v{version}:{token}'


def decrypt(value, domain: str = 'generic') -> str:
    raw = str(value or '')
    if not raw:
        return ''
    version, token = split_envelope(raw)
    if token is None:
        return raw
    candidates = []
    if version:
        candidates.append(version)
    for candidate in range(current_version(domain), 0, -1):
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        cipher = fernet(domain, candidate)
        if cipher is None:
            continue
        try:
            return cipher.decrypt(token.encode('ascii')).decode('utf-8')
        except InvalidToken:
            continue
        except Exception:
            continue
    if _is_dev_mode() and not _master_key():
        return raw
    raise RuntimeError('Stored secret cannot be decrypted with the configured key(s)')


def rotate(value, domain: str = 'generic'):
    """Re-encrypt a value at the current version. Returns (value, changed)."""
    if not is_envelope(value):
        return str(value or ''), False
    plaintext = decrypt(value, domain)
    updated = encrypt(plaintext, domain)
    return updated, updated != str(value or '')


def redact(value) -> str:
    """Return a fixed placeholder; never leak the value into logs or audit."""
    return '***' if str(value or '') else ''
