# Audit trail

## Problem

Every sensitive action already wrote an AuditLog row through `_log_audit`, and
the coverage was good (logins, MFA, WebAuthn, session revocation, permission
changes, telegram controls, financial reveals, credit adjustments). Two gaps
remained:

* nothing detected a **modification or deletion** of a row: an operator with
  database access could edit the trail and nobody could tell;
* rows carried no request context, so a row could not be tied to the request that
  produced it (the request id, the client address, the user agent), which is what
  incident response needs.

## Change

`panel/services/audit.py` (new) owns the trail:

* each row stores a SHA-256 digest over its canonical content (actor, action,
  target, meta, request context, timestamp) plus the previous row's hash, so the
  rows form a hash chain;
* `record()` resolves the actor and target, enriches the row with
  `request_id` (the X-Request-ID from phase 26), `source_ip` and `user_agent`
  from the active request, and adds it to the caller's transaction. It never
  raises and never commits, exactly like the helper it replaces, so a broken
  audit write cannot break a send or a payment;
* `verify_chain()` recomputes the chain oldest-first and reports the first break
  as `content_mismatch` (a row was edited) or `chain_link` (a row was deleted).
  Rows written before the chain existed have NULL hashes and are counted as
  `legacy` instead of failing the verification;
* a module-level lock serialises the tip read and the insert inside one process.

`AuditLog` gained `request_id`, `source_ip`, `user_agent`, `prev_hash` and
`entry_hash` (migration `a1b2c3d4e5f6`, additive and nullable, so existing rows
are untouched). `app._log_audit` now delegates to `audit.record`, so every
existing call site is chained without a change.

`GET /api/audit-log` (permission `settings.read`) returns a paginated,
newest-first page using the phase 22 pagination contract, with filters for
`action`, `actor_admin_id`, `target_type` and `since`/`until` (ISO 8601). Each
entry carries both hashes so an operator can verify a row outside the panel.

`GET /api/doctor` reports `checks.audit_chain` (verifying the last 2000 rows)
and marks it `warning` when the chain does not verify.

## Verification

`tests/test_audit_chain.py` (11 tests): rows chain from genesis and the verifier
accepts a clean trail; editing a row is reported as `content_mismatch` at that
row; deleting a middle row is reported as `chain_link`; legacy rows are counted
and skipped; the request context (id, user agent, client address) is captured;
`_log_audit` still writes chained rows with the right actor/target; a failed
login from the test client produces an `auth.login.failed` row whose
`request_id` matches the response header; the read API paginates newest-first
through the phase 22 contract, filters by action and actor, rejects an invalid
timestamp, refuses a reseller, and the doctor reports the chain as ok.

The migration was applied twice on a temporary database (stamp, then upgrade) and
the previous suite still passes.

## Residual risk

* The chain detects tampering, it does not prevent it: an attacker with database
  write access can delete the whole table. Off-box shipping of the trail (or of
  the tip hash) is the next step for that threat model, and `verify_chain`
  already exposes the tip for such a comparison.
* The tip read and the insert are serialised per process. Two gunicorn workers
  that commit concurrently can still fork the chain; that is reported as
  `chain_link` by the verifier and surfaced by the doctor. Rows are appended
  inside the action's transaction, so the window is small.
* Verification is bounded (2000 rows by default in the doctor) so the endpoint
  stays cheap on a large table; `verify_chain()` accepts no limit for a full,
  offline verification.
* `meta` may contain identifiers (a username on a failed login, an amount). The
  read API is permission-gated to `settings.read`, which is the same gate as the
  doctor.
