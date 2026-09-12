# Data retention

## Problem

Operational log tables grew without limit. Only the BNQO raw measurements (folded
into hourly rollups after 14 days) and backup files (opt-in) had any cleanup, so
`health_logs`, `monitor_message_log`, `whatsapp_bot_log`, `sms_send_log`,
`bnqo_jobs` and `admin_sessions` accumulated forever. Unbounded growth ends in a
full disk, and a full disk ends in an outage.

## Change

`panel/services/retention.py` (new) owns a small registry of policies:

| policy | table | window (default) | what it removes |
|--------|-------|------------------|-----------------|
| health_logs | health_logs | 90 d | watchdog and auto-heal log |
| monitor_message_log | monitor_message_log | 90 d | depletion message dedup window |
| whatsapp_bot_log | whatsapp_bot_log | 30 d | WhatsApp depletion send log |
| sms_send_log | sms_send_log | 180 d | SMS send history |
| bnqo_jobs | bnqo_jobs | 30 d | delivered jobs (pending ones are kept) |
| admin_sessions | admin_sessions | 30 d past expiry | expired or revoked sessions |

`AuditLog` is deliberately **not** a policy: the audit trail is evidence and must
be exported or archived, not silently deleted.

Properties:

* each window comes from `retention_days_<policy>` (clamped 1..3650); `0`
  disables that policy, and `retention_enabled=false` disables the whole pass;
* rows newer than the cutoff are never touched;
* deletion runs in bounded batches (`batch_size`, default 500; `max_batches`,
  default 20), and every batch deletes rows and advances the cursor in the same
  transaction, so an interrupted or repeated run is safe (idempotent);
* progress is recorded in the durable `system_migrations` ledger as
  `retention:<policy>` (status, phase, processed_rows, cursor, last_error), which
  is what makes a long cleanup resumable and auditable;
* `preview()` counts what a run would remove without deleting anything.

The backup scheduler loop in `panel/jobs/schedulers.py` runs the pass at most once
every 24 hours (tracked by the `retention_last_run` setting) and logs what it
removed. Operators can run it by hand:

    python -m panel.services.retention --dry-run
    python -m panel.services.retention --only health_logs --batch-size 1000
    python -m panel.services.retention --status

`GET /api/doctor` reports `checks.retention` (per policy: window, last status,
processed rows).

## Verification

`tests/test_retention.py` (12 tests): a preview counts without deleting; old rows
are deleted in batches and a later run resumes and finishes; recent rows are never
touched; the ledger records progress and marks the policy done; the second run
after completion deletes zero; a zero window disables a policy; `retention_enabled
= false` disables everything; expired sessions are pruned while live and recently
expired ones are kept; pending BNQO jobs are kept while delivered ones expire; the
status report covers every policy; the audit trail is not a policy; the CLI dry
run exits 0 and emits the policy report; and the doctor exposes the retention
block.

## Residual risk

* Defaults are enabled for operational logs only. Deployments that need the full
  history for a policy must set its window to `0` (or a large number) before
  upgrading; the preview command shows exactly what a run would remove.
* The scheduler pass is per process. With several workers each may run it; the
  batch delete is idempotent and the ledger is shared, so the duplicate work is
  harmless but not free. There is no cross-process lock on this pass yet.
* Retention is a hard delete. There is no soft-delete/undo: an operator who
  narrows a window cannot recover the rows.
* Large tables with no index on the timestamp column would scan; the policies were
  chosen among tables that already index their timestamp column.
