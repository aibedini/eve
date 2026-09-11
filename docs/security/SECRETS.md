# Secret management and key versioning

## Domains

Each secret belongs to a cryptographic domain with its own key, so ciphertext
from one domain is useless in another:

| Domain | Covers |
|--------|--------|
| xui_credentials | servers.password (X-UI panel password) |
| messaging | Telegram bot/proxy/egress secrets, WhatsApp/SMS API keys, Telegram backup settings |
| finance | bank card number / IBAN / account number, transaction and payment sender cards |
| subscriptions | custom subscription tokens and config URIs |
| mfa | TOTP secrets |
| backup | AES-256-GCM backup archives (raw AES key, not an envelope) |
| generic | anything not yet classified |

## Key resolution (panel/security/keyring.py)

Values carry an explicit version marker: enc:v1:, enc:v2:, ...

- v1 is the historical key: the raw SERVER_PASSWORD_KEY Fernet key. Every
  value written before this change still decrypts unchanged.
- v2 is the default once a master key exists. It is derived per domain with
  HKDF-SHA256(info="eve:<domain>:v2") from the master key, or taken from the
  explicit EVE_KEY_<DOMAIN>_V2 variable.
- Higher versions come from EVE_KEY_<DOMAIN>_V<n>; the highest configured
  version is what new writes use.

Any KMS/Vault integration only has to populate the EVE_KEY_<DOMAIN>_V<n>
variables; nothing else in the codebase needs to change.

## Reading and rotation

- decrypt tries the version named in the envelope first, then every older
  version, so a rotation needs no downtime.
- panel/services/secret_rotation.py re-encrypts existing rows to the current
  version. It is a durable, resumable migration: data and cursor advance in the
  same transaction under the system_migrations ledger (id rotate_secrets_v2),
  and values that cannot be decrypted are counted and skipped. It runs as part
  of the standard maintenance runner (python -m maintenance).

## Rotation runbook

1. Generate a new key and export it, for example
   EVE_KEY_FINANCE_V2=<urlsafe-base64-32-bytes>. Keep the old key configured.
2. Restart; new writes now use v2, reads still handle v1.
3. Run the maintenance runner to re-encrypt existing rows.
4. Once no v1 values remain, the v1 key can be removed.

For backups: export EVE_KEY_BACKUP_V2, restart (new archives use the v2 magic
header), and keep EVE_BACKUP_KEY until the last v1 archive is retired;
decrypt_backup_file picks the key from the archive header.

## Handling rules

- Secrets are never written to logs, HTTP responses, audit metadata, URLs or
  metrics. redact_secret(keyring.redact) returns a fixed placeholder.
- Decryption failures raise a generic message that never contains the token or
  the plaintext.
- hash_bearer_token is deliberately unchanged: it is already domain-separated
  by its purpose argument, and changing its derivation would invalidate every
  stored agent/session/backup-code digest.
