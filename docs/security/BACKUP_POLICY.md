# Backup Policy

Two backup pipelines exist in Eve and they MUST stay separate.

## 1. X-UI panel backups — intentionally NOT encrypted

**X-UI panel backups are intentionally NOT encrypted by Eve.** They are transient
artifacts that must never survive the operation that created them:

```
X-UI panel
  -> HTTPS with certificate verification
  -> Eve downloads the backup
  -> Eve writes it into a dedicated transient spool file
  -> Eve sends it to Telegram immediately
  -> Telegram returns a confirmed message_id + document metadata
  -> Eve deletes the local copy immediately (on success and on failure)
```

Policy (enforced by `panel/services/backup.py`, covered by
`tests/test_backup_policy.py`):

1. **Created only for the operation.** The spool file exists from the moment one
   upload attempt starts until that attempt's `finally` block runs. Nothing is
   pre-created, cached, queued or retained between runs.
2. **Dedicated transient location.** `/run/eve/xui-backup/` (tmpfs) or
   `EVE_XUI_BACKUP_DIR`, created `0700`; on hosts without `/run` a `0700`
   directory under the system temp dir. Never `instance/` and never
   `instance/backups/`. Nothing about an X-UI backup is persistent.
3. **Minimum permissions, atomic create.** `O_CREAT | O_EXCL | O_WRONLY` with mode
   `0600`, a random PID-tagged name, and the mode re-asserted after the write.
4. **Deleted on confirmed success.** Telegram success is accepted only when the
   API returns `ok: true` together with a valid `result.message_id` and
   `result.document.file_id`.
5. **Deleted on failure, timeout, exception and cancellation.** The unlink runs in
   a `finally` block, so a refused API response, a network error, a timeout and a
   `BaseException` cancellation (e.g. `CancelledError`/`KeyboardInterrupt`) all
   delete the file. A failed send leaves no local copy behind.
6. **Every retry re-downloads.** A new attempt fetches a fresh backup from the
   X-UI panel and creates a new spool file; a payload from a previous attempt is
   never reused.
7. **No persistent spool, cache, queue or retained backup.** The durable
   backup queue/job records hold **metadata only** (`server_id`, attempt/stage,
   timestamps, `failure_reason`). They never hold a path, a binary payload or the
   file itself.
8. **A cleanup failure is a security event.** A file that cannot be deleted is
   logged on the `eve.security` channel as `[security] transient backup file
   could not be deleted (...)` and recorded in the audit trail as
   `xui_backup_spool_cleanup_failed`. It is never swallowed silently.
9. **Crash recovery.** The janitor `prune_xui_backup_spool` runs at process
   startup (from `init_backup_tmp_dir`) and at the start of every backup run.
   Files whose owning PID is gone are removed immediately (kill -9 recovery); any
   file older than the stale threshold (default 300 s,
   `EVE_XUI_BACKUP_STALE_SECONDS`) is removed as well, covering PID reuse and
   platforms where the process probe is unavailable.
10. **No leakage.** Spool names are random and content-free. File names, logs,
    job metadata and error strings never carry backup bytes, a password, a bot
    token, a cookie or a proxy credential: connection errors pass through
    `redact_connection_error` before they are stored or logged, and the security
    report above names only the random basename and the error class.

Additional invariants:

- No AES-GCM / `.enc` / `.eveenc` envelope is applied to an X-UI backup.
- TLS certificate verification for the X-UI download is never disabled. A
  private CA is supported through `EVE_XUI_CA_BUNDLE`.

Test coverage in `tests/test_backup_policy.py`: deletion after a confirmed send,
after a refusal without document metadata, after a failed upload, after a timeout,
after an exception, and after cancellation; the spool directory/file permissions;
the startup janitor removing an orphan file and the janitor removing a dead-PID
file; a missing file not being reported as a failure; a cleanup failure being
reported as a security error (with the audit action and no path/token/content in
the log); and a retry downloading a fresh backup instead of reusing the previous
file.

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
