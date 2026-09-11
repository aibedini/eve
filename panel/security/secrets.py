"""Versioned encryption helpers for application-managed secrets.

Envelopes now carry an explicit key version (enc:v1:, enc:v2: ...) and are
scoped to a logical domain, so the X-UI credentials, messaging, finance,
subscription, MFA and backup secrets each use a different key. The keyring in
panel/security/keyring.py owns key resolution; this module keeps the historical
public API (encrypt_secret, decrypt_secret, hash_bearer_token, EncryptedText)
working unchanged.
"""
import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.fernet import InvalidToken  # noqa: F401 (public re-export)
from sqlalchemy.types import Text, TypeDecorator

from panel.security import keyring


SECRET_PREFIX = 'enc:v1:'   # historical marker, kept for compatibility
LEGACY_PREFIX = 'enc:'
TOKEN_HASH_PREFIX = 'h1:'

SENSITIVE_SYSTEM_CONFIG_KEYS = frozenset({
    'whatsapp_gateway_api_key',
    'sms_gmweb_api_key',
    'sms_custom_api_key',
})

SENSITIVE_SYSTEM_SETTING_KEYS = frozenset({
    'telegram_backup_bot_token',
    'telegram_backup_proxy_url',
    'telegram_backup_proxy_username',
    'telegram_backup_proxy_password',
})


def _is_dev_mode() -> bool:
    return keyring._is_dev_mode()


class _FernetProxy:
    """Backward-compatible accessor for the generic-domain Fernet key.

    Callers (and tests) historically used secrets._fernet() and
    secrets._fernet.cache_clear(); both keep working and clearing also drops the
    keyring's derived-key cache so an environment change takes effect.
    """

    def __call__(self):
        return keyring.fernet('generic')

    def cache_clear(self):
        keyring._key_b64.cache_clear()


_fernet = _FernetProxy()


def secret_domain(key: str) -> str:
    """Map a sensitive config/setting key to its cryptographic domain."""
    if key in SENSITIVE_SYSTEM_CONFIG_KEYS or key in SENSITIVE_SYSTEM_SETTING_KEYS:
        return 'messaging'
    return 'generic'


def is_encrypted(value) -> bool:
    return keyring.is_envelope(value)


def encrypt_secret(value, domain: str = 'generic') -> str:
    return keyring.encrypt(value, domain)


def decrypt_secret(value, domain: str = 'generic') -> str:
    return keyring.decrypt(value, domain)


def rotate_secret(value, domain: str = 'generic'):
    """Re-encrypt a stored value at the current key version."""
    return keyring.rotate(value, domain)


def redact_secret(value) -> str:
    return keyring.redact(value)


def _token_hash_key() -> bytes:
    """Return the stable application key used for one-way bearer-token digests."""
    key = (os.environ.get('SERVER_PASSWORD_KEY') or '').strip()
    if key:
        try:
            return base64.urlsafe_b64decode(key.encode('ascii'))
        except Exception as exc:
            raise RuntimeError('SERVER_PASSWORD_KEY is not a valid Fernet key') from exc
    if _is_dev_mode():
        return b'eve-development-only-token-hash-key'
    raise RuntimeError('SERVER_PASSWORD_KEY is required to protect stored tokens')


def hash_bearer_token(token, purpose: str) -> str:
    """Create a domain-separated, deterministic digest suitable for DB lookup."""
    raw = str(token or '')
    if not raw or raw.startswith(TOKEN_HASH_PREFIX):
        return raw
    message = f'{purpose}\0{raw}'.encode('utf-8')
    digest = hmac.new(_token_hash_key(), message, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return f'{TOKEN_HASH_PREFIX}{encoded}'


def is_hashed_bearer_token(value) -> bool:
    return str(value or '').startswith(TOKEN_HASH_PREFIX)


def protect_system_config(key: str, value) -> str:
    raw = str(value or '')
    return encrypt_secret(raw, secret_domain(key)) if key in SENSITIVE_SYSTEM_CONFIG_KEYS else raw


def reveal_system_config(key: str, value) -> str:
    raw = str(value or '')
    return decrypt_secret(raw, secret_domain(key)) if key in SENSITIVE_SYSTEM_CONFIG_KEYS else raw


def protect_system_setting(key: str, value) -> str:
    raw = str(value or '')
    return encrypt_secret(raw, secret_domain(key)) if key in SENSITIVE_SYSTEM_SETTING_KEYS else raw


def reveal_system_setting(key: str, value) -> str:
    raw = str(value or '')
    return decrypt_secret(raw, secret_domain(key)) if key in SENSITIVE_SYSTEM_SETTING_KEYS else raw


class EncryptedText(TypeDecorator):
    """Transparent encrypted-at-rest text for values not queried by content."""

    impl = Text
    cache_ok = True

    def __init__(self, domain: str = 'generic'):
        super().__init__()
        self.domain = domain if domain in keyring.DOMAINS else 'generic'

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_secret(value, self.domain)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt_secret(value, self.domain)
