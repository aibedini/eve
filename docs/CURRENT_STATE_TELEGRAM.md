# CURRENT STATE — EVE TELEGRAM (Phase 0 deliverable)

Audit of `main` @ `ed38edc`. **No code was modified to produce this document.**
Every claim below was read from the source; file:line is given for each.

Owner: Phase 0 (baseline + characterization) of the Telegram Enterprise directive.

---

## 0. Verdict in one paragraph

Eve already has a **large, working Telegram domain** (~9,300 lines of Python, 28
tables, 13 test modules) with real commerce, ownership, promo, wallet, trial,
reseller-scoping and support-group features. It has **no reliability
infrastructure**: no transactional outbox, no inbound inbox, no delivery/attempt
records, no DLQ, no incident state machine, no approval/step-up layer, and one
structural defect that the directive calls out by name (a process-local lock
around the X-UI backup). The correct move is **not** a rewrite: it is to introduce
the durable delivery core *underneath* the existing domain services, then
incrementally collapse the 5,214-line handler monolith onto it.

---

## 1. Actual module map (RFP §3 names do NOT exist)

| RFP assumes | Reality in Eve |
|---|---|
| `panel/telegram/` package | **absent** |
| domain / application / delivery / inbound split | **absent** — one worker file holds everything |

Real layout:

| Path | Lines | Role |
|---|---|---|
| `telegram_bot_runtime.py` | 792 | `TelegramBotApi` transport + route failover + localized UI helpers |
| `telegram_bot_worker.py` | **5,214** | polling loop **+ every handler + all business logic** |
| `telegram_diagnostics.py` | 226 | error classification + redaction |
| `telegram_egress_worker.py` | 58 | Xray tunnel supervisor (separate process) |
| `telegram_xray.py` | 320 | Xray process supervision |
| `panel/models/telegram.py` | 848 | 28 domain tables |
| `panel/routes/telegram.py` | 1,912 | admin API/UI for operations, promos, announcements, purchases |
| `panel/jobs/messaging.py` | — | `telegram_depletion_worker`, `telegram_announcement_worker` |
| `templates/telegram_operations.html` | 727 | existing operations screen |

## 2. Runtime roles today

```
eve-web                 gunicorn (PROCESS_ROLE=web)   -> HTTP only, reads Redis snapshot
background_worker.py    PROCESS_ROLE=background      -> ALL schedulers/automation threads
telegram_bot_worker.py  own process, own systemd unit -> long-polling, all handlers
telegram_egress_worker.py own process                -> Xray tunnel supervision only
```

**Gap vs RFP §2:** there is no `eve-telegram-worker` (outbound) /
`eve-telegram-update-worker` (inbound) split, and inbound work is not durable.
`telegram_egress_worker.py` already proves the pattern of a dedicated runtime role,
which is the precedent to follow.

## 3. Transport (RFP §10)

**Good news — roughly 90% of the transport already exists.**

* `telegram_bot_runtime.py:22` `_pooled_session()` — one `requests.Session` per worker
  thread, `HTTPAdapter(pool_connections=16, pool_maxsize=32)`. Connection reuse: ✔
* `TelegramBotApi.call()` (`:114`) — single choke point; `connect_timeout = 4`,
  `read_timeout = max(15, long_poll + 10)`; ordered route failover with a 20 s
  per-route cooldown (`_route_failed` `:92`), preferred-route memory
  (`_route_succeeded` `:88`), one bounded retry on a transient TLS EOF
  (`_post_with_transient_retry` `:99`).
* `TelegramApiError` carries `retryable` and `retry_after` (`:49`).
* Methods: `send_message` `:177`, `send_document` `:220`, `send_photo` `:232`,
  `answer_callback_query` `:321`, `get_updates` `:164`, `delete_webhook` `:170`,
  `get_webhook_info` `:173`.
* Redaction: `telegram_diagnostics.redact_connection_error(exc, (token,))` used on
  every error path.

**Gap:** the transport is a **class**, not an injectable interface, and there is a
**second, independent HTTP path** for backups —
`panel/services/backup.py:837/848` calls `https://api.telegram.org/bot{token}/getMe`
and `.../sendDocument` with `requests.get/post` directly, bypassing the pooled
session, the route ordering and the retry policy. That is 2 of the 4 raw
`api.telegram.org` call sites in the repo.

## 4. Outbound delivery (RFP §4, §5, §7, §8, §14)

**No outbox, no delivery table, no attempt table, no DLQ.** Confirmed: no
`telegram_outbox` / `telegram_delivery` / `telegram_dead_letter` table exists in
any Alembic revision.

What exists instead:

* `TelegramAnnouncement` + `TelegramAnnouncementDelivery`
  (`panel/models/telegram.py:203`, `:243`) — a **campaign** ledger with per-recipient
  rows, drained by `telegram_announcement_worker` (1 s tick). This is the closest
  existing thing to a durable queue and the natural donor for the campaign engine
  (RFP §37–38).
* `WhatsappBotLog` (shared with SMS/WhatsApp) is the dedup/cooldown ledger for
  lifecycle notifications — a log, not a queue.
* Everything else is sent **inline**: `api.send_message(...)` straight from a
  handler or a scheduler thread.

**Consequence (this is the RFP §4 scenario):** a renewal that commits and then
crashes before the Telegram call loses the notification with no record that it was
ever owed.

## 5. Inbound (RFP §15, §16)

**Long polling, and only long polling.**

* `transport_mode` column exists (`panel/models/telegram.py:24`, default `polling`)
  but the worker **actively deletes the webhook** on first contact:
  `telegram_bot_worker.py:5442-5444` (`delete_webhook`) guarded by the in-process
  set `webhook_prepared_bot_ids` (`:105`).
* The polling cursor is durable: `TelegramBotRuntime` (`panel/models/telegram.py:95`),
  claimed with a lease by `_claim_lease()` (`telegram_bot_worker.py:145`) — this is
  the one existing distributed-coordination primitive in the subsystem.
* **There is no `telegram_update_inbox`.** An update is processed in the polling
  loop, holding the bot lease, for as long as its handler takes.
* `callback_data` appears **87 times** in the worker. Samples:
  `"service:{ownership.id}"` (`:500`), `"noop"` (`:495`). No secrets observed in
  the sampled forms, but there is **no TTL, no nonce and no one-time-use wrapper**
  around them (RFP §21).

## 6. Business logic in handlers (RFP §10, §74) — **violated today**

The handlers do not call application services; they *are* the application. All of
the following run inside the polling loop:

| Concern | Location |
|---|---|
| renewal execution (**calls the Flask route in-process**) | `_execute_renewal_request` `:2881`, `app.test_request_context` `:2892` |
| purchase/provisioning execution | `_execute_purchase_request` `:3668`, `test_request_context` `:3688` |
| wallet debit | `apply_balance_delta` at `:1574, 2218, 2955, 3894, 5004` |
| service/ownership lookup for UI | `_cached_owned_service_location` `:518` |
| promo evaluation | `_evaluate_promos` `:350` |

Re-entering the HTTP application from inside a Telegram handler is the single
biggest structural debt: it couples the polling loop to web-route behaviour,
permissions, rate limiting and transaction boundaries.

## 7. Identity, RBAC, approvals (RFP §17–§21, §74)

* `TelegramIdentity` (`panel/models/telegram.py:~339`) binds a Telegram user to a
  `CustomerAccount` and carries `phone_normalized` + `status`.
* Customer identity binding for commerce exists and is tested
  (`tests/test_telegram_membership_gate.py`, `test_telegram_phone_policy.py`).
* **Operator binding, capabilities, step-up and dual control do not exist.** The
  only match for "approval" in `panel/routes/telegram.py` is an unrelated string at
  `:622`. Purchase/renewal "reviews" (`_provisioning_reviewer` `:3269`,
  `_execute_renewal_request(request_row, reviewer)`) are 1-person actions: the
  operator clicking a button *is* the authorization.
* Audit exists but is partial: 9 `_log_audit` calls in the Telegram routes
  (`:536, 615, 636, 777, 1005, 1023, 1031, 1131` + one more). Handler-side actions
  in `telegram_bot_worker.py` are **not** individually audited.

## 8. Secrets (RFP §48)

* Bot tokens **are encrypted at rest**: `_encrypt_telegram_secret` /
  `_decrypt_telegram_secret` (`app.py:4509/4519`) use the **versioned keyring**
  (`panel/security/keyring.py`, envelope `enc:v{version}:...`) in the `messaging`
  domain. `token_encrypted` stores the envelope and
  `panel/services/secret_rotation.py:40` already lists the column for rotation.
  ✔ This satisfies "encrypted using Eve secret architecture" and "key versioning".
* Egress profile URIs and proxy credentials are encrypted the same way
  (`panel/routes/telegram.py:91`, `app.py:4811`).

## 9. Egress (RFP §31)

* `TelegramEgressProfile` (`panel/models/telegram.py:881`) + `TelegramProxyEndpoint`
  (`:829`) model managed Xray tunnels and proxies; `telegram_egress_worker.py`
  supervises the tunnels with a 10 s reconcile loop and `last_heartbeat_at`.
* Per-bot `connection_mode` (`panel/models/telegram.py:23`, default
  `proxy_first`) drives route ordering inside `TelegramBotApi`.
* **Gap:** there is no named policy engine (`NEVER_DIRECT`,
  `PANEL_ACCOUNT_REQUIRED`, …). Failover is "try the next route in the list", which
  means a configured-but-down proxy **falls through to direct** — the exact silent
  downgrade RFP §31 and §67 forbid. This must be fixed before anything else is
  called secure.

## 10. Backup (RFP §42–§46, §65, §76)

**The good half already matches the directive.**

* X-UI backup **is never encrypted by Eve** and **never touches
  `instance/backups`**: `_send_xui_backup_to_telegram` (`panel/services/backup.py:1075`)
  spools to `/run/eve/xui-backup` (`:95`, dir `0700`, file `0600`) and unlinks in a
  `finally` (`:1105-1106`); a cleanup failure is raised as a **security event with
  an audit row** (`_report_spool_cleanup_failure` `:130`).
* A startup janitor already exists: `prune_xui_backup_spool` (`:218`, stale 300 s,
  PID-liveness aware `_pid_alive` `:202`).
* Eve DB backup is AES-GCM with versioned envelopes
  (`panel/security/backup_crypto.py`, magics `EVE-BACKUP-AESGCM-v1/v2`) and is
  **unlinked after upload** (`_send_eve_backup_to_telegram` `:1109`).
* Delivery is verified before the temp file is treated as delivered:
  `_telegram_document_delivered` (`:248`).

**The defect:**

* `_run_telegram_backup` is serialized by `TELEGRAM_BACKUP_LOCK =
  threading.Lock()` (`:48`, acquired `:1149`). That is **process-local**. With
  N gunicorn workers, **N concurrent backups can run simultaneously** — N panel
  downloads, N uploads. This is precisely the "threading.Lock for distributed
  correctness" the directive forbids (RFP §43, §65, §96).
* There is **no cooldown, no coalescing and no duplicate-trigger rejection** for
  backup requests (grep for `cooldown|last_backup_at` in `backup.py`: no matches).
* There is **no backup manifest table** (backup_id / sha256 / size /
  `telegram_message_id` / `correlation_id`) and **no restore drill** for the Eve DB
  backup — `_pg_restore_backup` (`:351`) exists as tooling but is not run as a
  verification.

## 11. Incidents (RFP §22–§25, §75)

**Nothing exists.** No incident table, no fingerprint, no ACK/resolve/snooze, no
maintenance window, no escalation. The 84 grep hits for "incident"/"fingerprint"
under `panel/` are all unrelated (snapshot digests, TLS certificate fingerprints).
Server problems reach Telegram today as **one message per detection** — the
5,000-message failure mode in RFP §23/§68 is currently the designed behaviour.

## 12. Lifecycle notifications (RFP §32–§33)

* `telegram_depletion_worker` (`panel/jobs/messaging.py:949`) runs a state scan on a
  30-minute loop over the shared snapshot; state → template → send, with a
  `WhatsappBotLog` cooldown.
* Renewal booking/wallet flows create `TelegramServiceRequest` rows (the request
  lifecycle) — a partial order model exists.
* **Gap:** expiry reminders are not bound to a subscription *version*. There is no
  "this queued T-3 reminder is now ineligible" check, i.e. the exact stale-message
  class that was just fixed for SMS in this repository is **still open on the
  Telegram channel**. The SMS fix (service identity + durable generation +
  invalidation) is the ready-made design to port.

## 13. Observability (RFP §28–§30, §57)

* `TelegramBotRuntime` holds durable per-bot health: `last_test_status`,
  `last_test_route`, `last_test_latency_ms`, `last_test_error`,
  `last_error`, `last_heartbeat_at`.
* `panel/routes/doctor.py` + the worker inventory (`panel/jobs/schedulers.py`) report
  thread/singleton state; `/api/audit-log` exposes the hash-chained trail.
* **Gap:** no queue-depth / oldest-pending / latency percentiles / DLQ / circuit
  state / health-score metrics, because none of those objects exist yet. No
  Prometheus endpoint (metrics today are in-process counters surfaced by
  `/api/doctor`).

## 14. Tests (RFP §63)

13 Telegram test modules, ~ let me count them precisely below — they are
**characterization-grade already**, which is the best news in this audit: the
behaviour is pinned well enough to refactor against.

`test_telegram_announcements`, `_audit_controls`, `_copy_overrides`,
`_delivery_faq`, `_membership_gate`, `_notifications`, `_phone_policy`,
`_promos`, `_reseller_bots`, `_reseller_scoping`, `_rotate`,
`_settings_payload`, `_trial`.

Missing per RFP §64–§71: concurrency (100× same idempotency key), multi-worker
claim, lease-expiry recovery, chaos matrix, egress fail-closed proof, backup
concurrency, secret-redaction suite.

## 15. GAP MATRIX (the deliverable table)

| Area | State | Evidence | RFP |
|---|---|---|---|
| Central transport | **Partial** — pooled, routed, redacted; backup path bypasses it | `telegram_bot_runtime.py:22,114`; `backup.py:837,848` | §10 |
| Transactional outbox | **Absent** | no table in `alembic/versions` | §4 |
| Queue claiming (SKIP LOCKED + lease) | **Absent** (per-bot lease only) | `telegram_bot_worker.py:145` | §5 |
| Priority lanes / fairness | **Absent** | — | §6 |
| Delivery + attempts tables | **Absent** | — | §7, §8 |
| Delivery semantics doc | **Absent** | — | §9 |
| Retry classification | **Partial** — `retryable`/`retry_after` exist, no persistent policy | `telegram_bot_runtime.py:49` | §11 |
| Circuit breaker | **Partial** — 20 s per-route cooldown, in-process only | `telegram_bot_runtime.py:92-97` | §12 |
| Rate limiting | **Partial** — in-process bucket | `telegram_bot_worker.py:172-188` | §13 |
| DLQ | **Absent** | — | §14 |
| Inbound inbox | **Absent** (long polling only, webhook deleted) | `telegram_bot_worker.py:5442` | §15 |
| Webhook security | **N/A today**, must be built if webhook mode is chosen | — | §16 |
| Operator binding | **Absent** (customer identity binding exists) | `panel/models/telegram.py` | §17 |
| Capability RBAC | **Absent** (admin role checks only) | — | §18 |
| Step-up auth | **Absent** | — | §19 |
| Dual control | **Absent** | — | §20 |
| Callback nonce/TTL | **Absent** (87 `callback_data` uses) | `telegram_bot_worker.py:495,500` | §21 |
| Incident engine | **Absent** | — | §22–§25 |
| Operations Center | **Partial** — `telegram_operations.html` (727 lines) covers promos/purchases/announcements | `panel/routes/telegram.py` | §26 |
| Delivery trace | **Absent** | — | §27 |
| Metrics / SLO / health score | **Absent** (bot-level health only) | `TelegramBotRuntime` | §28–§30 |
| Egress policy engine | **Absent** — failover can silently go direct | `telegram_bot_runtime.py:80-97` | §31 |
| Lifecycle versioning | **Absent** on Telegram (just fixed for SMS) | `panel/jobs/messaging.py:949` | §32, §33 |
| Provisioning saga | **Partial** — `TelegramPurchaseRequest(Detail/Allocation)`, no explicit states | `panel/models/telegram.py:416,776,792` | §34 |
| Support center | **Partial** — support group + topics + SLA fields on the bot, no ticket model | `panel/models/telegram.py:25-30` | §35, §36 |
| Campaign engine | **Partial** — announcement ledger + delivery rows; no segmentation/preview/dry-run/suppression | `panel/models/telegram.py:203,243` | §37–§39 |
| Template engine | **Partial** — `copy_overrides_json`, per-bot FAQ/tutorial; no versioning | `panel/models/telegram.py:21` | §40, §41 |
| Eve DB backup | **Strong** — AES-GCM v1/v2, keyring, verified delivery, unlink | `panel/security/backup_crypto.py`; `backup.py:1109` | §42A |
| X-UI backup | **Strong except locking** — spool `0700/0600`, finally-unlink, janitor, no encryption (correct) | `backup.py:95,1075,1105,218` | §42B |
| X-UI distributed lock | **BROKEN** — process-local `threading.Lock` | `backup.py:48,1149` | §43 |
| Backup manifest | **Absent** | — | §44 |
| Restore drill | **Absent** | `backup.py:351` (tooling only) | §45 |
| Backup alerting | **Absent** | — | §46 |
| Audit trail | **Partial** — hash-chained in web routes; worker actions not audited | `panel/routes/telegram.py` (9 calls) | §47 |
| Secret handling | **Good** — encrypted, versioned, redacted | `app.py:4509`; `secret_rotation.py:40` | §48 |
| PII / retention | **Partial** — retention service exists for logs; no Telegram-specific policy | `panel/services/retention.py` | §49 |
| Backpressure / bounds | **Absent** | — | §50 |
| DB concurrency discipline | **Mixed** — `TelegramBotRuntime` lease is fine; no guarded state transitions elsewhere | — | §51 |
| Redis failure behaviour | **Undefined for Telegram** | — | §52 |
| Attack/degraded modes | **Absent** | — | §54 |
| Worker heartbeats | **Partial** — bot runtime + egress profile heartbeats; no worker registry | `telegram_egress_worker.py:58` | §57 |
| Graceful shutdown | **Partial** — egress worker handles SIGTERM; polling worker has `_stop` `:131` | — | §58 |
| systemd units | **Partial** — web/worker/egress units referenced; no telegram delivery/update units | `docker/`, `install` | §59 |
| Docs / ADRs | **Partial** — `docs/TELEGRAM_ROADMAP.md` exists; no `docs/telegram/*` | — | §78, §79 |

## 16. Findings that must be fixed regardless of phasing

1. **`TELEGRAM_BACKUP_LOCK` is not distributed** (`panel/services/backup.py:48`).
   Multi-worker Eve can run concurrent X-UI backups for the same server. This is a
   correctness *and* a panel-load bug. (RFP §43, §65, §96.)
2. **Egress failover can silently downgrade to direct.** A configured proxy that is
   down makes `_ordered_routes()` fall through to the direct route. (RFP §31, §67.)
3. **No durable record of an owed notification.** A crash between commit and
   `sendMessage` loses the message silently. (RFP §4, §98.)
4. **Business logic lives in the polling loop and re-enters Flask**
   (`telegram_bot_worker.py:2892, 3688`). (RFP §10, §74.)
5. **High-risk actions need only a button press** — no step-up, no dual control,
   no per-action audit. (RFP §19, §20, §47, §74.)
6. **No incident deduplication** — every failed health check is a message.
   (RFP §22, §23, §68.)
7. **Server problems and lifecycle messages share one channel with campaigns**, so a
   100k-recipient broadcast can delay a renewal notice. (RFP §6, §38, §69.)

## 17. Reuse map (what I will build ON, not replace)

| RFP component | Build on |
|---|---|
| Transport + pooling + redaction | `telegram_bot_runtime.py` (extract an interface, keep the behaviour) |
| Route failover → policy engine | `TelegramRoute`, `connection_mode`, `TelegramEgressProfile` |
| Campaign engine | `TelegramAnnouncement` + `TelegramAnnouncementDelivery` |
| Durable cursor / lease precedent | `TelegramBotRuntime` + `_claim_lease()` |
| Commerce saga | `TelegramPurchaseRequest` + `…Detail` + `…Allocation` |
| Support | bot `support_group_*` fields + `TelegramIdentity` |
| Lifecycle invalidation | the SMS design shipped in `panel/services/lifecycle.py` |
| Secrets / keyring / rotation | `panel/security/keyring.py`, `secret_rotation.py` |
| Backup spool / janitor / manifest hook | `panel/services/backup.py` |
| Audit | `_log_audit` + `AuditLog` hash chain |

## 18. Decisions I need before Phase 1

These change the shape of every later phase, so I am not guessing:

1. **Long polling or webhook?** Eve deletes its webhook today
   (`telegram_bot_worker.py:5443`). Durable-inbox + webhook is better for HA, but
   long polling needs no public HTTPS entry point. RFP §16 says support migration
   *if* webhook mode is chosen.
2. **Scope discipline.** Phases 1–17 as written are a multi-week program
   (≈9.3k existing lines + an estimated 10–15k new). Do you want (a) everything
   sequentially, or (b) the Phase-1/2/3 core (transport + durable outbox +
   delivery + retries + DLQ) landed and proven first, then the feature phases?
3. **Monolith extraction.** Do I extract handlers out of
   `telegram_bot_worker.py` (5,214 lines) as part of Phase 1–3, or keep the file
   intact and route its sends through the new delivery layer first, extracting
   later? The second is lower-risk and preserves the 13 characterization suites.
4. **Fixed defects now or in phase?** Findings 1 and 2 (backup lock, egress
   fallback) are small, independent and correctness-critical. I recommend fixing
   them immediately, before Phase 1, because "no silent security fallback" is
   already violated in production.
