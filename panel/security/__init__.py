"""Security primitives shared by routes, jobs, services, and adapters."""

from .tls import outbound_tls_verify, panel_tls_verify
from .network import (
    InsecurePanelTransportError, TrustedProxyMiddleware, allow_insecure_panel,
    client_ip, enforce_panel_transport, inspect_panel_url, is_loopback_host,
    limiter_client_key, peer_is_trusted, resolve_client_ip, trusted_proxy_policy,
)
from .backup_crypto import decrypt_backup_file, encrypt_backup_file
from . import keyring
from .secrets import (
    EncryptedText, decrypt_secret, encrypt_secret, hash_bearer_token, is_encrypted,
    is_hashed_bearer_token, protect_system_config, protect_system_setting,
    redact_secret, reveal_system_config, reveal_system_setting, rotate_secret,
    secret_domain,
)

__all__ = [
    'EncryptedText', 'decrypt_backup_file', 'decrypt_secret',
    'encrypt_backup_file', 'encrypt_secret', 'is_encrypted',
    'hash_bearer_token', 'is_hashed_bearer_token',
    'keyring',
    'outbound_tls_verify', 'panel_tls_verify',
    'InsecurePanelTransportError', 'TrustedProxyMiddleware',
    'allow_insecure_panel', 'client_ip', 'enforce_panel_transport',
    'inspect_panel_url', 'is_loopback_host', 'limiter_client_key',
    'peer_is_trusted', 'resolve_client_ip', 'trusted_proxy_policy',
    'protect_system_config', 'protect_system_setting',
    'redact_secret', 'reveal_system_config', 'reveal_system_setting',
    'rotate_secret', 'secret_domain',
]
