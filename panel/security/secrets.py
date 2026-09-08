"""Versioned encryption helpers for application-managed secrets.

The current envelope uses the existing ``SERVER_PASSWORD_KEY`` Fernet key so
upgrades can decrypt legacy ``enc:`` values. New values carry an explicit key
version marker, allowing a later online rotation to introduce ``v2`` safely.
"""

import base64
import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator


SECRET_PREFIX = 'enc:v1:'
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
    env = (os.environ.get('FLASK_ENV') or os.environ.get('ENV') or '').strip().lower()
    debug = (os.environ.get('DEBUG') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
    return debug or env in {'development', 'dev', 'test', 'testing'}


@lru_cache(maxsize=1)
def _fernet() -> Fernet | None:
    key = (os.environ.get('SERVER_PASSWORD_KEY') or '').strip()
    if not key:
        if _is_dev_mode():
            return None
        raise RuntimeError('SERVER_PASSWORD_KEY is required to protect stored secrets')
    try:
        return Fernet(key)
    except Exception as exc:
        raise RuntimeError('SERVER_PASSWORD_KEY is not a valid Fernet key') from exc


def is_encrypted(value: object) -> bool:
    raw = str(value or '')
    return raw.startswith(SECRET_PREFIX) or raw.startswith(LEGACY_PREFIX)


def encrypt_secret(value: object) -> str:
    raw = str(value or '')
    if not raw or is_encrypted(raw):
        return raw
    cipher = _fernet()
    if cipher is None:  # development compatibility only
        return raw
    token = cipher.encrypt(raw.encode('utf-8')).decode('ascii')
    return f'{SECRET_PREFIX}{token}'


def decrypt_secret(value: object) -> str:
    raw = str(value or '')
    if not raw:
        return ''
    if raw.startswith(SECRET_PREFIX):
        token = raw[len(SECRET_PREFIX):]
    elif raw.startswith(LEGACY_PREFIX):
        token = raw[len(LEGACY_PREFIX):]
    else:
        return raw
    cipher = _fernet()
    if cipher is None:
        return raw
    try:
        return cipher.decrypt(token.encode('ascii')).decode('utf-8')
    except InvalidToken as exc:
        raise RuntimeError('Stored secret cannot be decrypted with the configured key') from exc


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


def hash_bearer_token(token: object, purpose: str) -> str:
    """Create a domain-separated, deterministic digest suitable for DB lookup."""
    raw = str(token or '')
    if not raw or raw.startswith(TOKEN_HASH_PREFIX):
        return raw
    message = f'{purpose}\0{raw}'.encode('utf-8')
    digest = hmac.new(_token_hash_key(), message, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return f'{TOKEN_HASH_PREFIX}{encoded}'


def is_hashed_bearer_token(value: object) -> bool:
    return str(value or '').startswith(TOKEN_HASH_PREFIX)


def protect_system_config(key: str, value: object) -> str:
    raw = str(value or '')
    return encrypt_secret(raw) if key in SENSITIVE_SYSTEM_CONFIG_KEYS else raw


def reveal_system_config(key: str, value: object) -> str:
    raw = str(value or '')
    return decrypt_secret(raw) if key in SENSITIVE_SYSTEM_CONFIG_KEYS else raw


def protect_system_setting(key: str, value: object) -> str:
    raw = str(value or '')
    return encrypt_secret(raw) if key in SENSITIVE_SYSTEM_SETTING_KEYS else raw


def reveal_system_setting(key: str, value: object) -> str:
    raw = str(value or '')
    return decrypt_secret(raw) if key in SENSITIVE_SYSTEM_SETTING_KEYS else raw


class EncryptedText(TypeDecorator):
    """Transparent encrypted-at-rest text for values not queried by content."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_secret(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt_secret(value)
