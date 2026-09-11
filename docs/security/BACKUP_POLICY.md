# Backup Policy

Two backup pipelines exist in Eve and they MUST stay separate.

## 1. X-UI panel backups — intentionally NOT encrypted

**X-UI panel backups are intentionally NOT encrypted by Eve.**

They are transient artifacts:

```
X-UI panel
  -> HTTPS with certificate verification
  -> Eve downloads the backup
  -> Eve sends it to Telegram immediately
  -> Telegram returns a confirmed message_id + document metadata
  -> Eve deletes the local copy immediately
```

Rules (enforced by `panel/services/backup.py` and covered by
`tests/test_backup_policy.py`):

- No AES-GCM / `.enc` / `.eveenc` envelope is applied to an X-UI backup.
- No X-UI backup may ever reach persistent storage. It lives only inside the
  RAM-backed spool `/run/eve/xui-backup/` (tmpfs), never
  `instance/backups/` or any other persistent path.
- Spool directory mode is `0700`; every spool file is created with mode
  `0600` and a random, PID-tagged name.
- The spool file is removed in a `finally` block on success **and** on
  failure. A failed Telegram upload deletes the local copy and records only
  metadata; the next retry downloads a brand-new backup from the panel.
- Telegram success is only accepted when the API returns `ok: true` together
  with a valid `result.message_id` and `result.document.file_id`.
- Startup and each scheduled run execute a janitor
  (`prune_xui_backup_spool`) that removes spool files whose owning PID is gone
  (kill -9 / crash recovery) and any file older than the stale threshold
  (default 300 s, `EVE_XUI_BACKUP_STALE_SECONDS`).
- TLS certificate verification for the X-UI download is never disabled. A
  private CA is supported through `EVE_XUI_CA_BUNDLE`.
- No backup binary, token, or proxy credential is written to logs or job
  metadata; connection errors are passed through `redact_connection_error`
  before they are stored.

The durable backup queue/job records hold **metadata only** (`server_id`,
attempt/stage, timestamps, `failure_reason`). They never hold a path or a
binary payload.

## 2. Eve database backups — encrypted

Eve's own database backups follow a separate policy:

- The plaintext Eve database is **never** sent to Telegram.
- The Telegram copy is encrypted with AES-256-GCM
  (`panel/security/backup_crypto.py`, `EVE_BACKUP_KEY` with
  `SERVER_PASSWORD_KEY` fallback).
- The transient ciphertext is written to the run temp directory and unlinked in
  a `finally` block.
- Authenticity is verified before plaintext is published on restore (see
  `decrypt_backup_file`).

## On-demand operator download

The operator endpoint `GET /api/servers/<id>/xui-backup` (superadmin only)
returns the X-UI payload from RAM only. It never writes to disk and is likewise
unencrypted, matching the transient policy above.
