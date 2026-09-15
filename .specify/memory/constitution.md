<!--
Sync Impact Report
==================
Version change: unfilled Spec Kit 1.0.6 template scaffold -> 1.0.0 (initial ratification)
Modified principles: none (first ratification; the template carried no project principles)
Added sections:
  - Core Principles I-XIII
  - Cross-Repository Contract: EVE -> GMweb -> Messages
  - Development Workflow & Quality Gates
  - Governance
Removed sections: none
Follow-up TODOs: none. Every placeholder is filled; no deferred tokens remain.
Note: this report is scratch material for review of the amendment. Remove this
comment block before the constitution is committed.
-->

# EVE Constitution

This constitution encodes the invariants that have previously caused production
incidents in EVE, not generic advice. Where an existing document already owns an
invariant, this constitution **links** to it rather than restating it.

Canonical references (inputs to the specification process, never disposable):

- `docs/SMS_LIFECYCLE_INVALIDATION.md` — the generation barrier and the four-barrier scanner guard.
- `docs/SMS_GATEWAY_DELIVERY_SPEC.md` — "gateway 200/202 = accepted, NOT delivered".
- `docs/TELEMETRY_STATE_TRANSITIONS.md` — fresh telemetry detects; the timer repairs; invariant -> mechanism -> test table.
- `docs/GMWEB_CONTRACT.md` + `shared/eve-gmweb-contract-v1.json` — the wire contract.
- `docs/architecture/adr/0011-renewal-events-are-business-facts.md` — only a verified mutation is a business fact.
- `docs/security/` — ARCHITECTURE, THREAT_MODEL, NETWORK, KEY_MANAGEMENT, SECRETS, AUDIT_LOG, RBAC, BACKUP_POLICY, INCIDENT_RESPONSE, INCIDENT_HISTORY_EXPOSURE.
- `docs/operations/` — WORKERS, OBSERVABILITY, BUILD_IDENTITY, RETENTION.
- `docs/performance/` — REFRESH_LOCK, QUERY_OPTIMIZATION, LATENCY_SLO, SUBSCRIPTION_CACHE, DELTA_SYNC.
- `docs/CURRENT_STATE_TELEGRAM.md` — the maintained gap matrix (known limits).
- `docs/README.md` is an **enforced** index; `tests/test_docs_index.py` fails when a document is not linked from it.

## Core Principles

### I. Canonical Service Identity

A service identity is based on **server identity + X-UI client UUID**, and its
canonical shape is `eve:<server_id>:<client_uuid>`.

- There MUST be exactly one constructor:
  `panel/services/lifecycle.py:102 make_service_key()` (the f-string is at
  `:115`). Constructing the string inline anywhere else is prohibited — it is
  how the renewal side and the scan side silently stop agreeing on identity.
- Producers MUST resolve the UUID through
  `resolve_canonical_service_key()` (`lifecycle.py:251`) rather than guessing,
  because a mixed key writes a **different** generation.
- Phone numbers are recipients, never service identities. A phone MAY be a
  unique key for a *customer account* (`panel/models/finance.py:240`), but it
  MUST NOT key a service lifecycle, notification or ownership row.
- Email MAY be a compatibility/bootstrap locator only where a UUID genuinely
  cannot yet be resolved (`client_uuid_from_email`, `lifecycle.py:91`). It is a
  documented weak fallback and MUST NOT silently replace an existing UUID
  identity.
- Durable service identity is persisted as `UNIQUE(service_key)`
  (`panel/models/ops.py:568`) and ownership as `UNIQUE(server_id, client_uuid)`
  (`panel/models/finance.py:264`).

Regression guard: `tests/test_sms_lifecycle_invalidation.py:152` asserts the
service key never uses the phone or the email as identity.

### II. Lifecycle Generation

Every lifecycle-changing operation MUST preserve a monotonic durable generation.

- Storage: `ServiceLifecycleState.generation` (`panel/models/ops.py:576`).
  It lives in the **database**, never in Redis or a process-local dict, because
  the worker that renews and the worker that scans are different OS processes.
- Advancement: `_advance_generation()` (`panel/services/lifecycle.py:445`,
  increment at `:469`) with a bounded retry on `IntegrityError` and a **loud**
  `service_generation_contention` failure when retries are exhausted
  (`:482`). Failure to read a generation MUST fail closed — a database error
  MUST NOT be reported as generation 0 (`read_or_create_generation`, `:377`).
- A reminder computed under generation N MUST NEVER remain deliverable after a
  successful lifecycle advance to N+1.
- Generation checks MUST occur both when notification work is created
  (`panel/services/telemetry_state.py:370`) **and** as late as practical before
  delivery (`panel/jobs/messaging.py:3814-3825`, "FENCE 1"), with the scan path
  re-reading at `messaging.py:3687`.
- Do not confuse the durable barrier with the non-durable counters:
  `ServiceObservedState.state_version`, `eve:server_revision:<sid>`,
  the snapshot-delta revision and `fetch_sequence` tickets. Only **generation**
  is the durable barrier.

### III. Telemetry State

Fresh accepted panel telemetry is the primary detector of service-state
transitions. Reconciliation is a repair mechanism, not an independent competing
source of notification truth.

- The timer-driven scan is a **repair net**. Detection MUST happen from fresh
  telemetry at the moment the dashboard copy is produced
  (`docs/TELEMETRY_STATE_TRANSITIONS.md:15-26`).
- State classification MUST have **one canonical implementation**. The canonical
  calculator is `app.py:1744 _compute_client_service_state()`, normalized by
  `panel/services/client_state.py:40 normalize_client_state()`, and consumed
  through `panel/jobs/refresh.py` and the read routes.
- **Known violation to close**: three independent implementations of the same
  precedence exist today and provably disagree.
  `panel/jobs/messaging.py:3381 _classify_monitor_status()` (its own docstring
  says "keep in sync with" the others) and `panel/routes/monitor.py:73
  get_monitor_alerts()` each re-implement the classification. They disagree on
  "0 bytes remaining AND expired date" (canonical: `volume_ended`, early return
  `app.py:1765-1766`; SMS scan: `expired`) and on the low-volume threshold
  (canonical `low_volume_gb` default 1.0 at `app.py:1756` vs SMS
  `depletion_volume_gb` default 2.0 at `messaging.py:3652`). New code MUST use
  the canonical calculator and the bridges
  (`SERVICE_STATE_TO_SMS_STATE` `telemetry_state.py:75`,
  `sms_state_for` `:83`, `SMS_STATE_TO_NOTIFICATION_KIND` `lifecycle.py:60`).
  It MUST NOT add a fourth copy.
- The **first observation of an entity is a silent baseline**, not a transition
  (`telemetry_state.py:240-275`). Only an explicit reconciliation pass may raise
  an event for an already-actionable service, and it stays under the existing
  caps, cooldowns and quiet hours. Violating this texts every expired account in
  the install at deploy time.

### IV. Fetch Ordering

Concurrent panel reads require explicit ordering. A slower stale response MUST
NOT overwrite a newer accepted response.

- Ordering is by **issue**, not arrival: take a monotonic per-server ticket
  before the read (`panel/core/fetch_sequence.py:72 begin()`, taken at
  `panel/jobs/schedulers.py:671-673`) and compare-and-set it before applying
  (`fetch_sequence.py:92 accept()`, enforced first at `schedulers.py:541-552`
  before the server-revision CAS at `:553-554`). The CAS is a single Lua script
  so Redis executes it without interleaving (`fetch_sequence.py:51-58`).
- "Time the response was applied" MUST NOT be used as proof of observation
  ordering. The apply-time stamps (`refresh.py:1977-1981, 1990-2004`) ARE
  legitimate as a *staleness lower bound* (`messaging.py:3403, 3433,
  3499-3500`) and MUST NOT be repurposed as a sequence.
- Without Redis the mechanism degrades to per-process semantics and MUST say so
  (`fetch_sequence.py:150 status()` -> `panel/routes/doctor.py:174`). A
  degraded ordering backend MUST be visible to the operator.

### V. Cross-Process Correctness

EVE runs multiple processes. Any fact relied on across processes MUST use an
appropriate shared/durable mechanism: PostgreSQL, Redis, database constraints,
leases, compare-and-set, or advisory locks.

**A Python process-local variable or thread lock is never sufficient for
distributed correctness.** A process-local lock MAY only guard process-local
memory, never a durable fact.

Available and expected mechanisms:

- PostgreSQL advisory locks: `panel/core/advisory_lock.py:202 resource_lock()`
  (`pg_try_advisory_lock` on a dedicated connection; `_discard()` `:87-102`
  invalidates the connection so a session lock can never travel through the
  pool). `resource_lock_attempt()` `:183` returns a three-valued
  `ACQUIRED | CONTENDED | LOCK_UNAVAILABLE` — treating a database outage as
  "already running" is prohibited.
- Redis: per-server snapshot writes serialized with a `SET NX EX` lease and a
  token-checked delete (`redis_client.py:178`, `:196-246`); per-server
  revision CAS (`:264, :276`).
- Database constraints as the race barrier: `UNIQUE(event_id)`
  (`panel/models/ops.py:758`), `UNIQUE(service_key)` (`:568, :713`),
  `UNIQUE(operation_id, event_type)` (ADR-0011), `UNIQUE(server_id, client_uuid)`
  (`finance.py:264`).
- Leases with reclaim: `LEASE_STATUS='sending'`, `LEASE_TIMEOUT_SECONDS=900`
  (`telemetry_state.py:416, 420`), `reclaim_expired_leases()` `:503`.

**Known violations to close** (each is a process-local lock guarding a
cross-process invariant or a guard that fails open):

1. `panel/services/audit.py:18 _CHAIN_LOCK = threading.Lock()` serializes a
   **global hash chain**. `record()` `:115` reads `tip_hash()` `:102` from the
   database and inserts; two workers can read the same tip and **fork the
   chain**, and there is no database constraint on `prev_hash`. A
   tamper-evident trail MUST be serialized durably.
2. `panel/core/subscription_cache.py:39` single-flight is per process, so the
   cache **value** is shared but the stampede guarantee is not.
3. `panel/jobs/schedulers.py:1861-1880` — the worker singleton guard **fails
   open** when `fcntl` is unavailable or the lock file cannot be opened.

Deliberately process-local (and MUST stay that way, documented in
`docs/performance/REFRESH_LOCK.md`): `GLOBAL_REFRESH_LOCK` / `GLOBAL_FETCH_LOCK`
guarding in-memory `GLOBAL_SERVER_DATA`, the in-process job registries, and the
per-process worker inventory reported by `/api/doctor`.

### VI. Notification Delivery

Notification intent MUST be durable. Deduplication MUST use deterministic
identity and a database uniqueness constraint, never check-before-insert. Worker
claims MUST be exclusive and recoverable after process death.

- The depletion outbox is `ServiceNotificationEvent`
  (`panel/models/ops.py:748-829`). Its identity is deterministic:
  `transition_event_id()` (`telemetry_state.py:107`) =
  `st:sha256(service_key|state|state_version)[:40]`, protected by
  `UNIQUE(event_id)` (`ops.py:758`). The loser of a race MUST catch
  `IntegrityError` and treat the winner's row as truth (`telemetry_state.py:325-329`).
- Claiming: `claim_events()` (`telemetry_state.py:441`) uses
  `with_for_update(skip_locked=True)` on PostgreSQL (`:465-467`) followed by
  one guarded `UPDATE ... WHERE status IN ('pending','retry')` (`:472-482`),
  re-read with `populate_existing()` (`:495`).
- Retry MUST be bounded and classified:
  `NOTIFICATION_BACKOFF_SECONDS = (30,120,600,1800,3600,10800)`
  (`ops.py:701`), then `failed_terminal` (`:574`). Deferral is not dropping:
  `mark_skipped(..., retry_in=)` (`:546`) MUST move `next_attempt_at`.
- Gateway idempotency key: `depletion-<event_id>` (`telemetry_state.py:395`),
  so a retry can never become a new physical SMS task.

**Known structural difference to keep explicit**: the lifecycle invalidation
outbox `ServiceNotificationOutbox` (`ops.py:602-665`) has **no lease columns
and no SKIP LOCKED**; `flush_invalidation_outbox()` (`lifecycle.py:894`)
selects pending rows and its safety net is idempotency plus the early return in
`attempt_outbox_event()` (`:717-718`), not an exclusive claim. A change that
relies on that outbox MUST state which of the two disciplines it depends on.

### VII. Renewal Safety

A renewal MUST immediately supersede obsolete depletion/expiry notification work,
and the delivery worker MUST re-read the lifecycle generation.

Gateway invalidation is the **semantic barrier**. Per-send cancellation MAY exist
as defense in depth but MUST NOT silently replace semantic invalidation where the
latter is required for the guarantee.

Ordering rule: the lifecycle advance is only ever called **after the panel write
succeeded and was read back** (`lifecycle.py:499-501`,
`panel/jobs/messaging.py:3014-3016`). A failed renewal MUST NOT suppress a
legitimate depletion reminder.

The single chokepoint is `_fire_cancel_stale_account_sms()`
(`messaging.py:2995`) calling `handle_successful_service_lifecycle_change()`
(`lifecycle.py:492`). Its ordered obligations:

1. generation bump **and** the invalidation-outbox row commit in the **same
   transaction** (`lifecycle.py:576-579`); a replayed operation is deduped by
   `last_operation_id` within `LIFECYCLE_OPERATION_DEDUPE_SECONDS = 1800`.
2. `supersede_pending(service_key, 'lifecycle_generation_advanced',
   max_generation=generation-1)` MUST retire queued **and already-leased** rows
   (`lifecycle.py:599` -> `telemetry_state.py:603`, lease status included
   `:615-616`).
3. immediate asynchronous gateway attempt (`lifecycle.py:622-623`).
4. best-effort local cancel of rows EVE still owns (`messaging.py:3053`).
5. `POST /send/invalidate` (`lifecycle.py:700` ->
   `messaging.py:2739`), using the path declared in the shared contract
   (`shared/eve-gmweb-contract-v1.json` -> `panel/services/gmweb_contract.py`).
6. the response MUST be validated before it is trusted
   (`validate_invalidation_response`, `lifecycle.py:168`).
7. a **permanent** authorization refusal MUST degrade to a narrower capability
   that still achieves the security goal — per-send `/send/cancel/{reference}`
   (`lifecycle.py:756-766`, `_cancel_known_sends()` `:777`, lookback
   `CANCEL_FALLBACK_LOOKBACK_MINUTES = 180`). The degradation MUST be
   observable (`invalidated_at = renewal_cancel_fallback`) and the missing
   scope recorded as an operator action item, never silently retried.
8. revocation stamped on the audit trail (`_log_invalidated_sends()`, `lifecycle.py:854`).

Deliberate exclusion: a customer's own renewal confirmation is never invalidated
(`TRANSACTIONAL_NOTIFICATION_KINDS = ('created','renew')`, `lifecycle.py:55`).

### VIII. Security

Panel credentials MUST NEVER be transmitted over plaintext HTTP silently.
`allow_insecure` is an explicit operator decision, per server. Transport or
authentication guarantees MUST NOT be weakened simply to restore functionality.

- Guard: `panel/security/network.py:237 enforce_panel_transport()`
  (exception `:51 InsecurePanelTransportError`); plaintext to a non-loopback
  host without opt-in raises (`:258-262`). A per-server decision always wins, so
  enabling one insecure server never relaxes the policy for the others
  (`:13-20`).
- `Server.allow_insecure` is a durable, audited column
  (`alembic/versions/c9d8e7f6a5b4_server_allow_insecure.py`); changes are audited
  (`panel/routes/admin.py:620, 696-698`). Turning the flag off MUST re-run the
  guard as `allow_insecure=False` (`:651-655`) — it MUST NOT simply be dropped.
- A security refusal MUST surface as **degraded coverage with a named warning**,
  never as silent staleness. The v2.7.2 incident — `allow_insecure` missing from
  both server dicts (`panel/services/depletion_pipeline.py:176-181`) — silently
  stopped **six of eight** panels from refreshing while the pipeline looked
  healthy.
- A security decision MUST travel with the data through every hop. An attribute
  reconstructed per hop is lost per hop.
- `docs/security/BACKUP_POLICY.md` governs backups: two pipelines MUST stay
  separate; an X-UI backup contains a plaintext panel credential and MUST be
  destroyed on **every** exit path including cancellation (`:32-49`); success
  MUST be proven by the recipient's own metadata (`message_id` +
  `document.file_id`), never by an accepted request; a cleanup failure is a
  security event (`xui_backup_spool_cleanup_failed`).
- Runtime artifacts (databases, backups, keys, `.env`, `*.bak`) MUST NOT enter
  version control. `docs/security/INCIDENT_HISTORY_EXPOSURE.md` records an
  **open** incident: the working tree is contained but git history is **not yet
  purged**, and credential rotation is a mandatory human action. Deleting a blob
  is not remediation.
- Secrets MUST respect their cryptographic domain (`docs/security/SECRETS.md`);
  `SESSION_SECRET`, `SERVER_PASSWORD_KEY` and `EVE_BACKUP_KEY` are independent
  (`docs/security/KEY_MANAGEMENT.md`).
- The audit trail is hash-chained and `verify_chain()` (`panel/services/audit.py:159`)
  MUST report the first break; the same action MUST be audited regardless of
  which process performs it. See Principle V for the open serialization defect.

### IX. Public Request Paths

Public subscription and read paths MUST NOT introduce expensive synchronous X-UI
calls that defeat caching / stale-while-revalidate behavior.

- A cache MUST coalesce concurrent misses into **exactly one** fill
  (leader/follower `begin`/`end`/`wait_for_fill`, `panel/core/subscription_cache.py:216-243`,
  bounded by `EVE_SUBSCRIPTION_CACHE_WAIT_SECONDS`). The measured regression it
  fixes: 2-4 live panel round trips on **every** request to
  `/s/<server_id>/<sub_id>`; the fix took 20 concurrent callers to 1 render
  (`docs/performance/SUBSCRIPTION_CACHE.md`).
- A lock MUST be held only for the work that needs it. Network I/O MUST NOT run
  inside the shared-state lock: the measured concurrent acquisition wait was
  **435.3 ms** (the whole fan-out) before scoping, **0.05 ms** after
  (`docs/performance/REFRESH_LOCK.md`; reproducible with
  `scripts/benchmark_locks.py`).
- Hot paths MUST respect the per-request SQL-statement budget
  (`docs/performance/QUERY_OPTIMIZATION.md`), and the numeric p95 budgets in
  `docs/performance/LATENCY_SLO.md` are CI-gated
  (`scripts/benchmark_latency_slo.py --quick`).

### X. Database Evolution

Schema changes MUST use Alembic and MUST maintain **one migration head** unless
an explicitly planned merge revision is required. Production migration execution
and rollback / forward recovery MUST be documented.

- Directory `alembic/versions/`; engine built through
  `panel/core/db_pool.alembic_engine_options` (`alembic/env.py`).
- Exactly one baseline adopts existing databases
  (`11b7afcfe0ee_baseline.py`, `down_revision = None`) and is **stamped, never
  replayed** (`panel/migrate.py:621 _ensure_alembic_current()`, stamp at `:634`,
  upgrade at `:636`).
- The whole migration runner (`run_migrations()`, `panel/migrate.py:639`) runs
  inside the cross-process file lock `_migration_lock()` (`:40`).
- **Every new schema change MUST be an Alembic revision**, never a runtime
  `ALTER`.
- **Known gap to close**: the single-head property is an unguarded convention.
  No `get_heads()` assertion exists in the codebase or CI; only per-revision
  upgrade tests (`tests/test_alembic_*.py`) exist. A change that adds a revision
  MUST NOT rely on the absence of a guard — add the single-head check or state
  explicitly that it remains unguarded.
- Migration execution and rollback/forward-recovery procedure MUST be documented
  in the operations runbook for any production migration.

### XI. Observability

Doctor and health surfaces MUST expose **partial degradation**. One unreachable,
stale or uncovered panel MUST prevent a falsely green telemetry status.

- The canonical case: "The pipeline spent an afternoon looking healthy while the
  detector recorded nothing (a wiring bug) and while six of eight panels could
  not be fetched at all (a policy flag that never reached the fetcher). Counters
  alone did not show it." (`panel/services/depletion_pipeline.py:175-181`).
- Critical pipelines MUST expose **liveness** (when each stage last actually ran)
  and **coverage** (is every enabled panel being observed), not only aggregate
  counters: `depletion_pipeline.health()` (`:425`) with states
  `ok | warning | degraded | error` and **named** warnings, backed by TTL'd
  heartbeats (`:183-195`).
- A health surface MUST hydrate shared state before judging it. The v2.7.5
  incident: `/api/doctor` reported every panel stale while the fetcher was
  recording 11,757 services, because coverage read `GLOBAL_SERVER_DATA` without
  hydrating it from Redis first.
- A healthy global status MUST NOT hide one broken partition, server or device.
- An error path MUST NOT depend on the state it is reporting on. The v2.7.1
  incident: a leased row deleted out of band made the worker raise
  `ObjectDeletedError` while reading `event.event_id` **to log the failure**;
  retention pruning MUST NOT be able to turn a handled failure into an
  unhandled exception.
- Operational metrics and doctor output MUST be PII-free unless PII is strictly
  necessary and explicitly protected
  (`docs/security/FINANCIAL_PRIVACY.md` — a stored financial identifier is
  never returned in full by a list/detail API).

### XII. Testing

Critical lifecycle and notification work requires integration tests through the
**real wiring**, not only isolated function tests. Concurrency invariants require
concurrency tests. Production incidents require regression tests at the boundary
where the incident escaped previous coverage.

- Wiring MUST be tested end to end through the real boundaries (Server row ->
  fetch worker -> process inbounds -> snapshot -> ledger -> outbox -> delivery ->
  gateway POST). The v2.7.3 incident passed every unit test while producing zero
  ledger rows, because the caller passed the inbound list where the client list
  was expected. A unit test of a pipeline body cannot fail on the shape its
  caller passes.
- Concurrency invariants MUST have a test that actually races (duplicate
  notification, duplicate fetch, chain fork, claim contention).
- Every production incident listed in this constitution SHOULD gain a
  regression test at the boundary where it escaped.
- The invariant -> mechanism -> test mapping for SMS lifecycle work is
  maintained in `docs/TELEMETRY_STATE_TRANSITIONS.md:61-77` and MUST be updated
  with the mechanism, not after it.

### XIII. Production Acceptance

For notification lifecycle changes, green CI alone is insufficient. Use
controlled canary or production-like evidence where safe. **Never send
deliberate test SMS messages to real customers.**

- CI jobs and which of them are blocking: `docs/security/CI_REQUIRED_CHECKS.md`;
  `.github/workflows/tests.yml` (focused unit tests, latency SLOs, mutation
  scale, full suite) and `.github/workflows/security.yml` (CodeQL, gitleaks over
  full history, pip-audit, Trivy, forbidden artifacts).
- A "done" claim for lifecycle or delivery work MUST name the production or
  production-like evidence, or state explicitly that only CI-level evidence
  exists. It MUST NOT imply production verification that did not happen.
- Acceptance evidence MUST NOT depend on harming a real recipient. Use a
  controlled number, a test service, or an operator-approved canary.

## Cross-Repository Contract: EVE -> GMweb -> Messages

EVE owns *why* a notification should exist: service identity, lifecycle
generation and business state. GMweb owns whether that logical notification is
currently deliverable, plus the durable delivery/revocation state and the gateway
contract. Messages Android performs the irreversible physical SMS submission.

The wire contract is `shared/eve-gmweb-contract-v1.json`, read only through
`panel/services/gmweb_contract.py`; `docs/GMWEB_CONTRACT.md` describes it. It
MUST stay byte-identical with the gateway's own copy, so the two sides cannot
drift silently.

The system invariant for SMS lifecycle work is:

A notification from lifecycle generation N MUST NOT be physically submitted
after the service has durably advanced to generation N+1, unless physical
submission irreversibly completed before the invalidation barrier won the race.

Every EVE-side mechanism MUST map to that same invariant, using the same
vocabulary. For a change touching more than one repository:

1. assign one shared feature ID (for example `stale-sms-revocation-v4`);
2. use the same identifier in specs, plans, ADR references and acceptance reports;
3. define the system invariant before modifying any participant;
4. define or update the provider contract first;
5. define the compatibility matrix;
6. implement the provider, then the consumers;
7. converge each repository individually;
8. run cross-repository contract tests; and
9. run system-level acceptance before declaring the work complete.

EVE MUST NOT call a field or state that GMweb does not implement, and Android
MUST NOT assume a third vocabulary. Any mismatch requires a documented
compatibility adapter or version, not an implicit assumption.

## Development Workflow & Quality Gates

Spec Kit is the default engineering workflow for this repository.

- Trivial changes (spelling, comments, formatting, labels, version bumps) do not
  require the full workflow; every rule in this constitution still applies.
- Non-trivial bugs: `/speckit-bug-assess` then `/speckit-bug-fix` then
  `/speckit-bug-test`. Reproduce or establish root cause before patching; a
  green unit test is not proof that the production symptom is fixed.
- Features, refactors, schema, security and notification-lifecycle changes:
  `/speckit-specify` then `/speckit-clarify` when ambiguous, then
  `/speckit-plan`, `/speckit-tasks`, `/speckit-analyze`, `/speckit-implement`
  and `/speckit-converge`. Do not implement before analyze reports the artifacts
  are coherent.
- Uncertain ideas: `/speckit-assess-intake` through `/speckit-assess-decide`.
  Only a GO decision becomes a specification.

**Version discipline.** `APP_VERSION` in `app.py` is the single source of truth
for the `2.x.y` scheme; a code or behavior change MUST include the patch bump and
the matching CHANGELOG entry, and `scripts/release_check.py` enforces the
agreement. `pyproject.toml` still carries placeholder metadata
(`repl-nix-workspace`, `1.9.1`) and MUST NOT be treated as the version.

**Evidence commands.**

- Metadata and version consistency: `python scripts/release_check.py --json`
- Documentation tree guard: `python -m pytest tests/test_docs_index.py -q`
  (every `docs/**/*.md` is linked from `docs/README.md`; no broken relative
  links; the runbook covers a required topic list)
- UI design-system drift ratchet: `python scripts/ui_design_audit.py --check`
- UI guard suite: `python -m pytest tests/test_ui_design_system.py -q`
- Focused CI unit set: `python -m pytest -q tests/test_security_hardening.py
  tests/test_wallet_ledger.py tests/test_backup_policy.py tests/test_ci_guards.py
  tests/test_package_visibility.py tests/test_finance_filters.py
  tests/test_regression_matrix.py`
- Local interpreter: `.venv-test\Scripts\python.exe` (pytest 9.1.1). The system
  Python has no pytest. Run pytest from the repository root; `pyproject.toml`
  sets `pythonpath = ["."]`.

**Documentation discipline.** A new document MUST be added to `docs/README.md` in
the same change or `tests/test_docs_index.py` fails. Existing ADRs and
architecture documents are inputs to the specification process, not disposable
legacy text. When a document contradicts the code, the code and this constitution
win, and the stale document MUST be corrected or marked stale in the same change.

## Governance

This constitution supersedes other development practices in this repository. A
repository `AGENTS.md` rule that conflicts with a MUST in this document is a
defect in that file, not a licence to bypass the principle.

- **Amendments** require a written rationale, the affected principle, a migration
  note when behavior changes, and an update to this document's version and
  amendment date. Amendment is done through `/speckit-constitution`.
- **Versioning** is semantic: MAJOR for a backward-incompatible governance change
  or principle removal/redefinition; MINOR for a new principle or materially
  expanded guidance; PATCH for clarifications and wording.
- **Compliance review** expects every non-trivial change to state which principles
  it touches and how it satisfies them, and to record any principle it knowingly
  violates as a known gap rather than silently.
- **Known violations** recorded above are pre-existing defects. They MUST be
  closed deliberately or explicitly re-ratified; they MUST NOT be used as
  precedent for new work.

**Version**: 1.0.0 | **Ratified**: 2026-09-15 | **Last Amended**: 2026-09-15
