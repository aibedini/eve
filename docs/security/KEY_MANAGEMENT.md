# Key Management

- `SESSION_SECRET`: signs browser sessions. Rotating it logs everyone out.
- `SERVER_PASSWORD_KEY`: URL-safe base64 encoding of 32 random bytes. It
  encrypts application secrets and sensitive database fields.
- `EVE_BACKUP_KEY`: independent URL-safe base64 32-byte key for `.eveenc`
  Telegram backup files. New installers generate it automatically. Existing
  deployments must preserve it across upgrades and store an offline recovery
  copy.

Keep keys outside Git, logs, tickets, and backup archives encrypted by the same
key. Restrict the environment file to the service account (`0600`). Rotate a key
only through a migration that can decrypt with the old key and re-encrypt with
the new version; replacing it directly makes existing ciphertext unrecoverable.

Private CAs can be configured with `EVE_XUI_CA_BUNDLE`,
`EVE_WHATSAPP_CA_BUNDLE`, or the fallback `EVE_OUTBOUND_CA_BUNDLE`.
