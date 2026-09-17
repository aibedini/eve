# Changelog - Eve

All notable changes to Eve - Xui Manager are documented in this file.

## [2.7.11] - 2026-09-17

### Fixed
- Made the Telegram settings migration regression test own a disposable SQLAlchemy engine, eliminating import-order-dependent mutation and teardown of the process-global test database.
- Supplied the repository token required by the pinned Gitleaks action so the blocking full-history secret scan actually executes before dependency and filesystem security scans.

## [2.7.10] - 2026-09-17

### Changed
- Recorded the product decision that real-panel 3.7.x acceptance is not required for this release. Core 3.7 remains gated by the automated/contract compatibility matrix, while core 3.8 retains controlled real-panel acceptance.
- Marked Spec Kit tasks T048/T053 as waived rather than unresolved blockers, updated the Definition of Done and verdict rules, and added a regression guard for the release-scope metadata.
- Stabilized performance and coalescing test isolation on contended shared hosts: benchmark retries keep every original SLO/scale threshold intact, and the coalescing test now waits for actual follower overlap instead of assuming a fixed thread-start delay.

## [2.7.9] - 2026-09-17

### Fixed
- Restored the missing `tests/fixtures/xui/README.md` contract fixture and now test that the exact audited 3x-ui 3.7.0 and 3.8.0 commits remain recorded.
- Stabilized the mutation-scale O(1) benchmark under shared-host scheduler and antivirus contention. Mutation latency is measured in three independent rounds with garbage collection outside each timed window; the lowest-noise wall-clock round and its matching CPU samples are kept together, without relaxing the existing 2x scale bound.

### Validation
- Product waived real-panel 3.7.x acceptance for this release; it was not run and no real 3.7 panel was obtained, built, or started. The required 3.7 automated/contract gates remain in scope, while 3.8 includes controlled real-panel acceptance.

## [2.7.8] - 2026-09-16

### Added
- Wired 3.7/3.8 panel-side lifecycle automation detection into normal client reads and cache recomputation. Affected clients are reported as `partially_managed`, and Doctor receives the credential-free `panel_lifecycle_automation_detected` warning without synthesising an EVE lifecycle event.
- Made certified 3.8 panel settings authoritative for `subPath`, `subJsonPath`, and `subClashPath`. Link generation consumes the bounded metadata cache; missing settings retain the configured path and expose `subscription_path_fallback` in Doctor. Older, future, and unknown panel families keep their prior configured-path behaviour.
- Added authenticated Doctor-route coverage for version/profile/source/certification/auth state and verified that panel credentials, paths, and private-key material are absent from the response.

### Tests
- Added integration coverage for lifecycle read-path behaviour, randomized/operator-changed 3.8 subscription paths, observable path fallback, pre-3.8 compatibility, and authenticated Doctor output.
- Superseded by the 2.7.9 product decision: real-panel 3.7.x acceptance is waived and not required for release; automated/contract verification is the required core 3.7 gate.

## [2.7.7] - 2026-09-15

Version-gated 3x-ui 3.7.x / 3.8.x compatibility. Every behaviour introduced for a
3.7 or 3.8 panel is confined to that panel's family; older, newer and unknown
versions keep the behaviour they had before.

### Fixed
- **An EVE client mutation silently removed the operator's per-device (HWID) limit on 3x-ui 3.7.x and 3.8.x.** Upstream binds `limitHwid` as a *sibling* of the client object on `POST /panel/api/clients/update/{email}` (`model.Client` has no such field), so an absent key binds to Go's zero value, and `setClientLimitHwidByEmail` then writes `limit_hwid = 0` **unconditionally** (`client_crud.go:806` -> `client_hwid.go:296`, identical in both tags). Reproduced on a live 3.8.0 panel: `limitHwid` 2 before an EVE-shaped update, 0 after, with `expiryTime` changing in the same request. EVE could not echo the value because its mutation read path (`/inbounds/list` -> `settings.clients[]`) does not carry the field at all. Client updates now read the authoritative record from `/clients/get/{email}` and echo the device limit, so an unrelated renewal, edit, enable, reset or rotate leaves it untouched. A stored `0` ("no limit") is a real operator value and round-trips as `0`; if the authoritative read fails the mutation is refused rather than sent without the field, because sending it absent is what destroys the value.
- **A scoped or expired 3x-ui API token could make EVE treat a modern panel as a legacy one.** 3.7.0 added scoped and optionally expiring API tokens and answers a scope miss with `403`; a rejected Bearer is `401` on 3.8.0 but `404` on 3.7.0 unless the request carries `X-Requested-With: XMLHttpRequest`. The capability probe was a bare boolean that read any non-200 as "no v3 client API", cached that verdict, and sent the removed legacy `updateClient` request -- the same failure class that previously left renewed users inactive on token-less panels. The probe now returns a typed outcome (`SUPPORTED`, `ROUTE_MISSING`, `AUTH_INVALID`, `SCOPE_INSUFFICIENT`, `TRANSPORT_ERROR`, `INVALID_RESPONSE`), only a definitive route answer may update the capability cache, and the 3.7 profile sends the hint header because that is the only way to tell a bad credential from a missing route there.

### Added
- **`panel/services/xui_compat.py` is the single authority for version-gated behaviour.** Panel versions are normalised to numbers and reduced to a `(major, minor)` family; only an explicit whitelist (`3.7`, `3.8`) selects a non-baseline profile. `3.9.x`, `4.x` and anything newer resolve to the baseline profile with a `future_version_uncertified` warning and never inherit 3.8 semantics; an unparseable version resolves to the baseline with `panel_version_unknown` and `unverified`. No other module compares versions.
- **Version detection is local-first.** The panel's own build identity (`panelVersion` on `/panel/api/server/status`, present in both tags and continuously since 3.3.1) is authoritative and needs no outbound internet from the panel. `getPanelUpdateInfo` is corroboration only: it calls GitHub first and answers `success:false` with no `obj` when the panel is air-gapped, which is the normal case for the panels this matters for. Detection reuses the status response the fetcher already reads, so it adds no panel request.
- **Panel-side lifecycle automation is surfaced, never adopted.** 3.7.0 added per-client `resetDay`, `resetMax`, `trafficReset` and `trafficResetDay`, which let a panel renew or reset a client outside EVE's lifecycle journal. EVE detects and reports the condition (`panel_lifecycle_automation_detected`, service classified `partially_managed`) and neither writes nor zeroes the fields. Translating panel-side transitions into EVE lifecycle events is deliberately not part of this change.

### Tests
- `tests/test_3xui_compat.py` grew 22 tests covering version normalisation (including `v`-prefixed, 2-component, build-suffixed and unusable inputs), whitelist-only profile selection, the future-version guarantee (3.9/4.x never select the 3.8 profile), pre-existing families keeping their status, status-code classification (401/403 are never "route missing"), the profile-gated hint header, device-limit preservation (present, absent, stored zero, read failure, explicit change, allowlist scoping and unrelated-field carry-through) and lifecycle-automation detection.
- Acceptance against a real 3.8.0 panel: version detection (`panelVersion` 3.8.0 -> profile `xui_3_8`, `authoritative`), device limit preserved at 2 across EVE's production `v3_client_update` path while the expiry changed, read failure refused with an actionable error, and an explicit operator change to 5 honoured.

## [2.7.6] - 2026-09-14

### Fixed
- **A renewal could not revoke a reminder that had already reached the gateway.** Live acceptance proved it: the production project key lacks the `sms.invalidate` scope, so `/send/invalidate` answered `HTTP 403 project_scope_denied`, the queued depletion reminder stayed deliverable, and the phone pulled it and submitted it after the customer had renewed (`status: sent`). A scope refusal is permanent, so a denied service invalidation now degrades into cancelling the individual sends EVE dispatched for that service (`/send/cancel/{reference}`, scope `sms.cancel`), which the key does have, and stamps them `invalidated_at = renewal_cancel_fallback` in the audit log. Regression test: `test_a_scope_denied_invalidation_falls_back_to_cancelling_known_sends`.
- **Operator action still required**: grant `sms.invalidate` to the EVE project key at the GMweb deployment (its default scope set already includes it; the key was created with an explicit list that omitted it). Until then the fallback cancels per send, which cannot cover a reminder EVE has no request id for.

## [2.7.5] - 2026-09-14

### Fixed
- **Panel coverage described the wrong process's memory.** The new coverage table read `GLOBAL_SERVER_DATA` without hydrating it from Redis first, so the web process serving `/doctor/summary` reported every panel as stale with unknown reachability while the fetcher was happily recording 11,757 services. Found by running the doctor on production minutes after 2.7.4 shipped; coverage now hydrates first, and freshness comes from the panel's own read stamp (`reachable_checked_at`) with the newest client telemetry stamp as the fallback -- the per-client stamp is only refreshed when a row is rebuilt, so on its own it made a perfectly healthy install look stale. Regression test: `test_panel_coverage_hydrates_the_shared_snapshot_first`.

## [2.7.4] - 2026-09-14

Final acceptance + production hardening for the depletion-notification pipeline.

### Added
- **Doctor can no longer show a green pipeline over a broken one.** `/doctor/summary` -> `telemetry_pipeline` now carries a computed `state` (`ok`/`warning`/`degraded`/`error`), per-panel coverage (`enabled`, `covered`, `uncovered`, `refused_transport`, `unreachable`, `stale`, plus a per-panel row with the telemetry age), stage liveness (worker heartbeat, last detection, last delivery, last reconciliation, each with an age) and named `warnings` (`worker_heartbeat_missing`, `outbox_backlog_age`, `outbox_overdue`, `notification_retry_exhausted`, `redis_unavailable`, `panel_transport_refused`, `panel_unreachable`, `panel_telemetry_stale`, `no_recent_detection`, `pipeline_off`). One refused or stale panel makes the block degraded instead of hiding behind a healthy average. Counters, ages and status names only - never a destination, an address or a message.
- Outbox metrics gained per-status counters (`pending_only`, `retry`, `leased`, `sent`, `skipped`, `superseded`, `shadowed`, `failed_terminal`, `retry_exhausted`) and the last detection/delivery/supersede/reconciliation timestamps.

### Tests
- `tests/test_telemetry_pipeline_integration.py`: the CI gap that let three wiring defects reach production is closed by an end-to-end test through the real boundaries (Server row -> `fetch_worker` -> `process_inbounds` -> snapshot -> ledger -> outbox -> delivery worker -> GMweb POST), covering the happy path with exactly one send, the renewal-before-delivery race (zero sends, event superseded), one-phone/two-services isolation, the transport policy reaching the real guard, the exactly-one-sender invariant for `off`/`shadow`/`on`, reconciliation idempotence and stale-snapshot protection after a renewal, and the 5xx/429 retry semantics.

## [2.7.3] - 2026-09-14

### Fixed
- **The transition hook was called with the wrong shape, so the ledger stayed empty on a live install.** `_record_fetch_transitions` wrapped the processed INBOUND list as if it were the client list (`{'clients': processed}`), so it looked for an email on an inbound, found none and recorded nothing -- while every unit test passed, because they exercise the pipeline directly rather than the wiring. Caught by the production verification (a successful panel fetch that produced zero ledger rows), not by CI. The helper is now a module-level, testable function that receives the block list `process_inbounds()` returned, and three tests in `tests/test_telemetry_state_transitions.py::FetchPipelineWiringTests` drive that exact shape: a processed block must reach the ledger, a state flip through the hook must create exactly one event, and a ledger failure must not escape the fetch path.

## [2.7.2] - 2026-09-14

### Fixed
- **The periodic fetcher dropped the per-server transport policy.** Both server dicts the fetch pipeline builds (`_run_snapshot_with_progress` and the periodic fan-out in `_fetch_and_update_global_data_inner`) were handed to `fetch_worker` without `allow_insecure`, and the transport guard reads that attribute off the dict-derived object: a plaintext panel whose operator had explicitly allowed it therefore failed EVERY cycle with "Refusing to send panel credentials over plaintext HTTP", so the panel never refreshed and its data aged silently. Found while verifying the new transition detector on production, where six of eight panels were affected. Regression tests assert the flag travels into the fetcher and that `fetch_worker` reads it off the dict it is given (`tests/test_allow_insecure_server.py`).

## [2.7.1] - 2026-09-14

### Fixed
- **Delivery worker crashed its own error handler.** A leased notification row can be deleted out of band (retention pruning, an operator cleanup); the worker then raised `ObjectDeletedError` while reading `event.event_id` to LOG the failure, so a handled delivery failure surfaced as a traceback in the background log. The log label is now read defensively and the failure is reported as `failed` without touching the vanished instance. Found by the live production proof, reproduced in `tests/test_telemetry_state_transitions.py::DepletionEventDeliveryTests::test_a_row_deleted_mid_delivery_is_reported_not_raised`.

### Deployment notes
- Running migrations as `root` against an evemgr-owned lock file in `/tmp` fails with `PermissionError` on hosts with `fs.protected_regular=2`; run `python -m panel.migrate` as the service user (`sudo -u evemgr ...`), which is what the units expect.

## [2.7.0] - 2026-09-14

Real-time telemetry, dashboard sync and the depletion-notification pipeline. The
periodic SMS scan stops being the detector: fresh panel telemetry is, and a durable
notification outbox delivers one reminder per state transition.

### Added
- **Durable observed-state ledger** (`service_observed_states`): the last state Eve actually OBSERVED for a service, keyed by the canonical `eve:<server_id>:<client_uuid>` identity. A transition is "the value the one canonical calculator returns changed", so the dashboard, the subscription page and the reminder can never disagree about what a raw panel response means.
- **Transition notification outbox** (`service_notification_events`): one row per transition, with a deterministic `event_id` as the cross-worker race barrier (two pollers observing the same transition converge through a UNIQUE constraint, not a check-then-insert) and a delivery lease so two workers cannot send one reminder.
- **Low-latency delivery worker** (`depletion_event_worker`): claims due events every few seconds, re-reads the lifecycle generation, recomputes the state from the live snapshot, then posts with the durable generation and a stable `Idempotency-Key`. Quiet hours, daily and hourly budgets and rate limits DEFER an event (retry with `next_attempt_at`) instead of dropping the transition.
- **Monotonic per-server fetch tickets** (`panel/core/fetch_sequence.py`): `begin()` before a panel read, compare-and-set `accept()` before applying it, so a slow read that returns late can no longer overwrite a newer one. `telemetry_updated_at` is stamped at apply time and therefore cannot order two reads.
- **Shared per-server watch marks**: the dashboard's `?servers=1,2,3` declaration now reaches the fetching process through Redis (`eve:refresh:watch:<server_id>`, TTL = the active window), instead of only marking the web process that served the request.
- **`GET /doctor/summary` telemetry block**: mode, outbox counters (pending/overdue/oldest age/by status), tracked fetch sequences and shared watch marks. Counters only - never a phone number, an email address or a message body.

### Changed
- **The periodic SMS scan is now a reconciliation safety net.** It records what the snapshot holds into the ledger (so a transition nobody observed becomes an event instead of a silence) and drains the same outbox, under every existing gate: enabled trigger, reseller rules, `#nosms`/`#nopm`, cooldown, quiet hours, daily and hourly limits, per-recipient interval, gateway readiness and 429 backoff.
- **A renewal retires queued reminders** at the moment the lifecycle generation advances, in addition to the delivery-time generation fence: a reminder about the previous lifecycle cannot be delivered even if a worker had already leased it.
- **Canonical-to-SMS state translation** at delivery time, so the per-state trigger, cooldown, template and priority the operator configured apply to a detected transition exactly as they did to a scanned candidate.

### Rollout
- `EVE_DEPLETION_EVENT_PIPELINE` selects the sender: `off` (scan only, pre-migration behaviour), `shadow` (the pipeline records what it *would* send while the scan keeps sending) or `on` (default: the pipeline sends, the scan reconciles). Whichever mode is set, exactly one path can reach the gateway for a logical transition.
- The first observation of a service is a silent baseline, so deploying the ledger cannot text every already-depleted account at once; the reconciliation pass still catches currently-actionable accounts, bounded by the same caps.
- The only schema change is the additive `f1d4a6b8c9e2` migration (two new tables and their indexes).
- See `docs/TELEMETRY_STATE_TRANSITIONS.md` for the design, the invariant-to-test table and the operational surface.

## [2.6.0] - 2026-09-12
## [2.6.0] - 2026-09-12

A production-hardening release: thirty-two phases of security, correctness and
performance work, each with its own tests and, where performance changed, a
measured before/after artifact under `docs/performance/`.

### Security
- **PostgreSQL transport policy**: `sslmode` and certificate material are applied to every engine (Alembic included), the startup audit and `/api/doctor` warn about plaintext remote links, and transient connection failures answer 503 + `Retry-After` instead of 500 (`docs/security/POSTGRESQL.md`).
- **Upload hardening**: editor media and app files are validated by magic bytes and declared image dimensions, and anything served from `/static/uploads/` or `/static/app-files/` is sandboxed and forced to download unless it is a raster image (`docs/security/UPLOADS.md`).
- **Tamper-evident audit trail**: every sensitive action appends a hash-chained row with request id, source address and user agent; `verify_chain()` reports edits and deletions, `/api/audit-log` exposes a paginated view, and `/api/doctor` reports the chain (`docs/security/AUDIT_LOG.md`).
- **GMweb contract**: endpoint paths, headers and retry policy come from `shared/eve-gmweb-contract-v1.json`; the gateway URL is validated (http(s), no credentials, no query) and plaintext remote transport warns (`docs/GMWEB_CONTRACT.md`).
- **Request correlation and metrics**: every response carries `X-Request-ID`, JSON error payloads include it, and in-process per-endpoint counters feed `/api/doctor` (`docs/operations/OBSERVABILITY.md`).
- **Release guard**: `scripts/release_check.py` gates image publishing on version format, hash-pinned dependencies, wired scanners, a non-root image and the disclosure policy; the published image carries a signed SBOM and provenance (`docs/RELEASE_SECURITY.md`).
- **Network, certificate, header, RBAC, MFA and financial-privacy hardening** from the earlier phases of the program remain documented under `docs/security/`.

### Performance
- **Subscription response cache**: a bounded LRU plus TTL cache with single-flight coalescing in front of `/s/<server>/<sub>`; measured 400 polls over 20 keys from 400 panel reads / 8.2 s to 20 reads / 0.41 s (`docs/performance/SUBSCRIPTION_CACHE.md`).
- **Hot-path query budget**: the finance lists stopped re-reading settings per row and now eager-load their relationships; `GET /api/transactions` went from 65 to 6 statements and `GET /api/payments` from 86 to 8 (`docs/performance/QUERY_OPTIMIZATION.md`).
- **Reseller refresh projection**: `/api/refresh` derives the reseller view with per-inbound shallow copies instead of a deep copy of the snapshot; 1.76 s to 0.29 s with an identical payload (`docs/performance/SERIALIZATION.md`).
- **Static asset delivery**: `url_for` appends a content fingerprint so versioned assets are cached immutably; 0/6 to 6/6 dashboard assets (`docs/performance/STATIC_ASSETS.md`).
- **Bounded list responses**: one pagination contract (`limit`/`offset`, `total`, `has_more`) with a server-side ceiling; the BNQO link inventory dropped from 1000 rows / 289 KB to 200 rows / 58 KB per request (`docs/performance/API_PAGINATION.md`).
- **Load test and baselines**: a repeatable fixed-rate load generator plus the baseline harness, delta sync, SSE, panel limits, database pool, adaptive refresh and per-server cache measurements, each with a JSON artifact.

### Operations
- **Worker inventory**: every background worker start is recorded and reported by `/api/doctor`; a lock file that cannot be created fails open with a recorded reason instead of silently disabling the worker (`docs/operations/WORKERS.md`).
- **Data retention**: bounded, resumable policies prune operational logs and expired sessions through the `system_migrations` ledger, with a dry run and per-policy windows (`docs/operations/RETENTION.md`).
- **Documentation index and runbook**: `docs/README.md` lists every document and `docs/OPERATIONS_RUNBOOK.md` walks the day-two tasks; a test keeps both complete and link-clean.

### Operator notes
- Retention is enabled by default for operational logs (`health_logs` 90 d, `monitor_message_log` 90 d, `whatsapp_bot_log` 30 d, `sms_send_log` 180 d, `bnqo_jobs` 30 d, `admin_sessions` 30 d past expiry). Run `python -m panel.services.retention --dry-run` before upgrading, and set `retention_days_<policy>` to `0` to keep a table forever.
- The only schema change in this release is the additive audit-chain migration (`a1b2c3d4e5f6`); existing rows keep working and are reported as legacy by the chain verifier.
- New environment knobs are documented with each feature (`EVE_DB_SSLMODE`, `EVE_SUBSCRIPTION_CACHE_*`, `EVE_STATIC_*`, `EVE_PANEL_*`, `EVE_DB_POOL_*`).
## [2.5.85] - 2026-08-26

### Added
- Operator-defined **hourly SMS throttle** (`sms_hourly_limit`, Settings → SMS Automation → Rate limits; `0` = unlimited). It counts the same billable segments as the daily cap and is measured over the Tehran-clock hour, so a burst of gateway `429`/unpaired failures can never consume the allowance — only confirmed sends do.
- Bulk lanes (near expiry, low volume, expired, volume ended, royalty, announcements) that hit the hourly ceiling now stop the scan cleanly with the dedicated `hourly_limit_reached` reason instead of failing rows: nobody's per-state cooldown is spent, and the next scheduled scan resumes the remaining list once the hour rolls over — the same "wait for the window" behaviour quiet hours already had. Announcement deliveries park as `retry` with a 60-minute next attempt and are counted in the campaign's blocked list.
- `GET /api/sms/scan/status` reports `segments_used_this_hour` and `segment_hourly_limit`, and the SMS panel's segment line shows `this hour N/LIMIT` plus an explicit note when bulk sending is paused.

### Changed
- **Transactional create/renew SMS are now formally exempt from every rate ceiling except the per-recipient interval.** `_sms_take_send_slot` takes the sending lane, and the `critical` lane (create, renew, test, and quiet-hours-parked transactional flushes) bypasses the hourly throttle so a paying customer's confirmation is never delayed by a running bulk campaign.

### Fixed
- Recent sends could sit on `queued` forever while the gateway had long since reported them `sent`. The status poller walked pending rows **oldest-first**, and because the gateway ledger only retains a send for a bounded window, a growing tail of permanently-404 rows (1,353 of them, back to 18 Aug) filled every 100-row batch and starved the fresh sends. Polling is now newest-first, and a `404` on a row older than a 6-hour grace window closes it as `gateway_status_expired` (terminal, **not** successful, so it never counts as a billable segment) instead of being retried forever — the pending set drains rather than grows.

## [2.5.46] - 2026-07-29

### Added
- BNQO (Bidirectional Network Quality Observatory) Phase 1, integrated into eve: continuous bidirectional link-quality monitoring between Iran, outside, and relay servers. Rust `bnqo-agent` (workspace under `bnqo/`) probes each link in both directions with authenticated BNQO-UDP packets (XChaCha20-Poly1305, per-direction HKDF keys, replay window, no amplification) and also runs ICMP cycles, TCP/TLS service-target probes, host metrics, and MTR diagnostics; results spool to a local crash-safe WAL and upload idempotently with strictly-increasing sequence numbers.
- Control plane inside eve: one-time enrollment tokens → per-agent Ed25519 identity; every agent request is signature-verified with a ±300 s replay window; configs and typed jobs (RUN_MTR etc., no remote shell) are CP-signed with anti-rollback config versioning. New models (`bnqo_agents`, `bnqo_links`, `bnqo_measurements`, `bnqo_service_probes`, `bnqo_routes(_hops)`, `bnqo_incidents`, `bnqo_rollups_hourly`, `bnqo_jobs`, `bnqo_enroll_tokens`) via Alembic revision `b7e2c9a41d05`.
- Status engine (15 s background tick): 12-state per-link status with per-direction detail — no data is never reported as healthy — thresholds per RFP (warning loss ≥1% over 3 windows / RTT p95 > baseline+50%; critical loss ≥5% / complete loss / micro-outages), incident open/auto-resolve with evidence, route-change detection from MTR route hashes, auto diagnostic MTR on critical incidents (15 min cooldown), Telegram alerts, and raw→hourly rollup retention after 14 days.
- UI: new "Links" section (`/pulse/links` + per-link detail) with per-direction loss/RTT/jitter charts (vendored Chart.js), service-target states, route timeline with change highlighting, incident ack/resolve, agent management, and one-time install-command enrollment; linked from the Pulse page and the sidebar.
- CLI: `eve` → `[n] BNQO — Network Link Monitor` — install agent over SSH (key or sshpass), print the manual one-time install command, agent status, SSH uninstall, binary info; idempotent installer `static/app-files/bnqo/install.sh` deploying a hardened `bnqo-agent.service` (unprivileged user, CAP_NET_RAW only).
- Tests: 69 Rust tests (`cargo test --workspace`) and 22 Python tests (`tests/test_bnqo_web.py`); design + contract docs under `docs/bnqo/` (architecture, 10 ADRs, threat model, security controls, protocol, data model, API, SLO, test strategy, implementation plan, integration profile).

## [2.5.1] - 2026-07-18

### Fixed
- Telegram Bots settings tab failed to load with a 500 on upgraded databases: the startup migrations applied `ALTER TABLE` batches inside a single try/except, so one failed statement (e.g. two gunicorn workers racing the same migration, or a transient lock) silently skipped the remaining columns — leaving `telegram_purchase_policies`/`packages`/purchase tables partially migrated and breaking only `GET /api/settings/telegram-bots` while the bot list and promotions endpoints kept working. All telegram/package/bank-card migrations now add columns one-by-one through `_migrate_add_columns` with an independent guard per column, so migrations are fully resumable; the `archived_at` type is also postgres-safe (`TIMESTAMP`). Adds a regression test asserting the settings payload returns 200 with all sections and that partially applied migrations resume.

## [2.5.0] - 2026-07-18

### Telegram
- Add a rule-based promo engine: `TelegramPromo` (optional code, percent/fixed with cap, bot/package scope, purchase/renewal targeting, min-amount, first-purchase, 30/90-day purchase counts, min-referrals, channel-membership requirement, time window, total/per-user usage caps, stacking + priority, reseller-pricing opt-out, per-reseller ownership) with durable `TelegramPromoUse` enforcement and stats, managed from a new Promotions card in the telegram settings tab via `GET/POST/PUT/DELETE /api/settings/telegram-promos` (audit-logged).
- Purchases evaluate promos at payment time (entered code + automatic), freeze the final amount, primary promo, per-promo discounts, and code on the purchase session, show original/strikethrough + discount in the payment message, and persist original_amount/discount_amount/promo_code plus PromoUse rows at receipt — admin notifications and the operations UI show both amounts. A "I have a promo code" button captures codes via a new awaiting_promo_code state.
- Renewals evaluate automatic promos in the package list and store original/discount on the service request; renewal amounts now use the reseller-aware price resolver instead of raw package.price.
- Add referrals: `/start ref_<id>` records a `TelegramReferral` (no self-referrals, one referrer per user), qualified when the invitee verifies a phone, feeding min_referrals promo conditions; the main menu gains a "My invite link" button. Channel promos check getChatMember live with a short cache and are honored exactly once per user per promo.
- Add durable audit trail: new `AuditLog` model with a `_log_audit` helper recording telegram bot settings saves, lifecycle actions (enable/disable/restart/archive/restore), purchase and service review decisions (both the operations API and the in-bot reviewer callbacks), and trial/emergency grants with actor attribution (admin/system/customer).
- Add rate limiting: `@limiter.limit` on `/api/telegram-operations*` and `/api/telegram-bots*` (60/min reads, 20/min actions), plus an in-process sliding-window `_rate_ok` in the bot worker guarding purchase-start, buy-package, receipt, and support message handlers with a quiet drop on excess.
- Add receipt fraud controls: reusing a `receipt_file_unique_id` across purchase requests flags `duplicate_receipt` on the new record (never auto-rejects), surfaces a prominent warning in the admin Telegram notification and the operations UI, and the purchase amount is now frozen as `quoted_amount` on the purchase session at payment time so later price changes cannot drift the receipt.
- Extend `/api/telegram-operations` with a `per_bot` breakdown (purchases, approved+completed revenue, completion rate, open support tickets per bot), rendered as cards in the operations page.
- Add controlled free trial: packages can be flagged `is_trial` (price 0 allowed, hidden from normal purchase/renewal lists), and a per-bot policy (`trial_enabled` + `trial_package_id`) shows a "free trial" main-menu button. Verified customers get direct free provisioning through the purchase path (`free: True`, ownership `verification_method='telegram_trial'`), gated by the durable `TelegramTrialGrant` ledger at one trial per phone number (and per telegram user) per bot, with admin notification.
- Add emergency access: a per-bot policy (`emergency_enabled`, days/volume/cooldown, defaults 1d/1GB/30d) puts an emergency button on expired or volume-ended service cards, granting a free custom renewal once per service per cooldown window, recorded in the same ledger.
- Trial and emergency policies are saved through the telegram-bots settings API (`trial_packages` payload added) and configurable in the settings UI; provisioning reviewers fall back from the bot owner to a superadmin when free creation is not permitted.
- Add customer Telegram notifications: a singleton `_run_telegram_depletion_scan` (30-minute `telegram_depletion_worker` thread) messages bot-linked customers whose service is near expiry or low on volume, resolved via ServiceOwnership → CustomerAccount → TelegramIdentity. Reseller-owned accounts are messaged through the reseller's own active, non-archived bot with fallback to the central bot. Templates (`tg_tpl_near_expiry` / `tg_tpl_low_volume`), the `tg_depletion_enabled` flag, and thresholds (falling back to the shared WhatsApp/SMS depletion thresholds) are settings-driven; cooldown reuses WhatsappBotLog with `tg_near_expiry` / `tg_low_volume` events and is reset by the existing renewal cooldown clear.
- Panel-side renewals now also send the rendered renewal confirmation to the customer's linked Telegram identity via `_notify_customer_telegram` (in-bot purchase order events already notified the customer).
- Add per-bot runtime controls and soft-archive lifecycle: `POST /api/telegram-bots/<id>/runtime` supports enable/disable/restart (owner or superadmin; enable requires a token), archive/restore (superadmin only, system bot forbidden). Archive blocks with 409 + pending counts while purchase/service requests are pending, frees the reseller scope_key so a replacement bot can be created, and hides the bot from the worker discovery query, the settings routes, and the default list (`include_archived=1` restores visibility for superadmins).
- Report per-bot health (`running|stale|stopped|disabled|archived|error` with heartbeat/last error/failed update count) in `GET /api/telegram-bots`, surfaced as badges and lifecycle buttons in the settings Bots card and the reseller bot page.
- Add reseller-owned Telegram bots: superadmin bot list/create card in settings plus a reseller self-service page, with one bot per reseller (`scope_key=reseller:<id>`, 409 on duplicates) and duplicate-token rejection both at save time and after a successful getMe diagnostic.
- Open the telegram-bots settings APIs to an optional `bot_id` with an ownership check (superadmin or the owning reseller); without `bot_id` behavior stays exactly as before on the central bot.
- Brand reseller-bot customer messages with the bot's display name and fall back on the central bot to the telegram user's reseller (latest active `ServiceOwnership.reseller_id`) for package visibility, bank card selection, pricing, eligible servers, and purchase notifications.
- Scope Telegram purchase/renewal flows to the bot owner: reseller-owned bots pick bank cards by priority (own → assigned → central), show only visible packages (global + assigned + personal), and charge reseller-resolved prices; renewal package lists follow the same scoping.
- Add reseller ownership to bank cards (`reseller_id` + `assigned_reseller_ids`) with superadmin-only management UI/API, scoped card listing for non-superadmins, and receipt-time revalidation of the stored card.
- Make the telegram-bots settings payload bot-aware so the settings UI mirrors what the bot's customers see.

### Installer
- Install the `eve` CLI from the freshly synced application copy instead of `$0`, ensuring the menu itself advances during runner-based online updates.
- Treat an already-running `/usr/local/bin/eve` as current instead of attempting to copy the CLI onto itself and printing a false permission warning.
- Keep operational flags out of positional domain parsing and persist the validated panel domain root-only, preventing online updates from rewriting nginx as `server_name --online-update`.
- Support private GitHub repositories through a root-only persisted install credential plus an ephemeral askpass flow, so the `eve` update menu needs no repeated credential entry and no secret is embedded in the origin URL.
- Run installer-owned Git operations as root to reuse the original installation credential instead of losing access by switching to the runtime-only `evemgr` account.
- Bootstrap and self-update the CLI through authenticated Git objects rather than unauthenticated private-repository raw URLs.
- Apply `EVE_REPO_URL` to existing installations during upgrades and report authentication-specific recovery steps when fetch fails.

### SMS Automation
- Preserve GMweb HTTP 429 response details in SMS logs and scan-stop messages, including rate-limit reason, minute/hour usage, and retry-after hints.

### 3x-ui compatibility
- Refresh v3 compatibility coverage for 3x-ui v3.5.0 and add MTProto fallback subscription-link generation using per-client secret/adTag fields.
- Detect the first-class v3 client API by endpoint capability instead of treating API Token presence as the panel version; cookie + CSRF and Bearer authentication are both supported.
- Use native client attach/detach endpoints when available, with a 404-only legacy fallback, so 3.4.2+ WireGuard clients receive panel-generated keys and addresses safely.
- Add WireGuard subscription-link fallback generation and preserve legacy string-encoded as well as modern nested inbound JSON.
- Add compatibility contract tests for legacy panels, tokenless v3 panels, native membership endpoints, and WireGuard links.

## [2.4.0] - 2026-06-26

> Big release since **2.3.0** — a whole new **SMS Automation** subsystem, **3x-ui v3.4+** support, reseller **finance statements**, and major dashboard **performance** work.

### 📲 SMS Automation — new subsystem (GMweb / Google Messages gateway)
- **Automated SMS** on **create**, **renew**, and **near-depletion** (low-volume / near-expiry / expired / volume-ended) — using your own SMS templates, so an automated text reads exactly like a manual one
- **👑 Royalty SMS**: nudge owner-less *idle* accounts (active but zero traffic in the window). A **cap-fair queue** drains huge lists over several days — each user exactly once, every send & skip logged
- **🧾 Send queue & live log**: paginated, **Jalali + Asia/Tehran** timestamps, auto-refreshes every 5s, `no-store` (always fresh), and shared across gunicorn workers via Redis
- **🧪 Send Test SMS** to the superadmin / panel contact number · **Start now** / **Stop & disable** · live scan progress + cancel
- **🌙 Quiet hours** (Asia/Tehran): hold reminder SMS overnight and flush after the window — create/renew confirmations always go out immediately
- **⏱️ Fairness & safety**: per-state hourly cooldown shared with WhatsApp (no double-ping, reset on renewal), global send pace + **HTTP 429 backoff**, and an **Idempotency-Key** so retries never double-send
- **🔒 Owner gating**: only owner-less (system/superadmin) accounts are messaged — reseller-owned accounts are never texted from the system number
- **🚫 Opt-out tags** `#nosms` / `#nopm` in the client comment suppress messaging; manual **disable** adds them, **enable/renew** strips them
- Options: skip unlimited accounts, expired max-age cap, volume-ended cutoff

### 🧩 3x-ui v3.4 / v3.4.1 compatibility
- **Account creation now works on v3.4+ panels** (node-hosted inbounds): client payload defaults `security=auto`. v3.4 made that field required — its absence made the panel silently drop the new client (empty 200)
- Renew / disable / read / subscription / online detection verified working on v3.4.1

### 💰 Reseller Finance
- **Reseller statement**: accounts / cost / packages with **per-package drill-down** (gift + GB/days fallback) and a **"should-deposit"** figure

### 🔐 Reseller Permissions
- **Free creation/renew/reset** gated behind a per-user permission
- **Per-reseller WhatsApp automation** permission

### ⚡ Performance
- **Progressive, server-by-server dashboard load** — no more waiting for every panel before anything shows
- Removed the per-request **deepcopy** on the `/api/refresh` hot path
- **gzip/br compression** to shrink large `/api/refresh` payloads

### 🛠️ Fixes & polish
- **Renew** surfaces the **real panel error** on HTTP 400 (e.g. *Duplicate subId*) instead of a generic message
- **"Assigned inbounds"** now shows reliably for v3 servers in Edit Client
- Fixed a **500 on /admins** (an escaped apostrophe broke a Jinja string)
- **Settings** fully responsive on mobile + no horizontal overflow at desktop widths (e.g. 1440px)
- **setup.sh** prunes old app-dir backups on update so `/opt` stops filling
- Subscription page loads **all inbounds** for v3 multi-inbound clients
- **i18n**: reseller permission labels localized by panel language
- Online update auto-verifies and installs requirements

## [2.3.2] - 2026-06-20

### 📱 WhatsApp Automation Scope
- **Per-reseller automation permission**: New "WhatsApp Automation Enabled" toggle in each reseller's user settings (default OFF). The system no longer messages a reseller's clients from the owner's WhatsApp number unless that reseller is explicitly opted in
- **Scoped near-depletion scan**: Background depletion scanner skips accounts owned by resellers who haven't enabled automation
- **Scoped renew auto-send**: Automatic post-renewal WhatsApp message is suppressed for reseller-owned accounts without the permission
- Accounts owned by the system owner / admins / superadmins are always eligible (unchanged behavior)

## [2.3.1] - 2026-06-20

### 🔒 Reseller Permissions
- **Free creation/renew gating**: The "Free" toggle (new purchase, renewal, and traffic reset) is now hidden from resellers unless explicitly enabled per-user. A new "Allow Free Creation" switch in the reseller's user settings controls it
- **Server-side enforcement**: All three free-action endpoints reject `is_free` requests with HTTP 403 for resellers without the permission — the toggle cannot be bypassed client-side
- Admins/superadmins are unaffected (they never consume credit)

## [2.3.0] - 2026-06-20

### 🤖 WhatsApp Bot (OpenWA)
- **OpenWA self-hosted gateway**: Integrate Eve with OpenWA as an alternative to Baileys — send WhatsApp messages through your own local server without relying on the cloud
- **Warm-up mode**: Linear ramp-up of the daily send cap over N days so new WhatsApp sessions aren't flagged for sudden volume spikes
- **Near-depletion bot**: Background scanner (every 30 min) that automatically messages clients whose subscription volume or time is running low; configurable thresholds, cooldown, and dedup via database log
- **Bot templates**: Dedicated WhatsApp message templates for Created, Renew, Ended, and Info events — separate from SMS templates with their own placeholders
- **Pace gate**: Optional minimum gap + random jitter between any two WhatsApp sends to mimic human pacing (off by default)
- **Ban-risk warning banner**: Displays a prominent warning when OpenWA provider is selected with recommendations for safe usage
- **Session UUID resolver**: Automatically resolves OpenWA session names to internal UUIDs (with 5-minute cache) to work around OpenWA's runtime engine indexing by UUID not name

### 📊 Monitor Overhaul
- **Zero-usage badge & royalty extend**: Idle clients now shown with a distinct chip; royalty information template can be sent directly from the monitor table
- **Message send counter**: Per-client SMS/WhatsApp send count visible in the monitor row
- **`{dashboard_link}` & `{sub_link}` fixed**: These placeholders were empty in monitor alert messages — now correctly populated from cached client data
- **Royalty template fallback chain**: Monitor now tries the royalty template first, then the standard template, with a proper reset of send counter on renewal
- **No-usage template field**: Added directly in monitor settings (no longer buried)
- **Default filters**: Monitor now defaults to Low+Soon only (reseller users hidden) on first load
- **Deduplication**: Same user on multiple inbounds shown once per server; ended/expired clients always restored regardless of enable flag
- **Time-expiry priority**: Expiry by time takes priority over volume-ended status
- **Responsive layout**: Fixed monitor table layout for Surface/tablet widths (1101–1500px)
- **Add Days modal**: Translated to English; counter resets on any renewal (dashboard or bulk)

### 📝 Templates
- **WhatsApp/SMS variants**: Dedicated Created and Renew templates for WhatsApp and SMS with send test buttons
- **Conditional gift blocks**: `{if_gift}…{/if_gift}` and `{gift_volume}` placeholders available in all template editors
- **Account-info variables**: `{telegram_channel}`, `{whatsapp_channel}`, and all account-info placeholders now resolved correctly by role in Created/Renew sends
- **Unresolved placeholder cleanup**: All `{…}` tokens that don't match any variable are stripped before sending

### ⚙️ 3x-ui v3 Compatibility
- **v3.3.1 CSRF support**: Fetch `X-CSRF-Token` before login so Eve works with panels that have CSRF middleware enabled (fully backward compatible)
- **http→https self-heal**: Server saved with `http://` automatically retried over `https://` when the panel is SSL-only
- **Spaced email handling**: Emails with spaces are renamed on the panel before any add/update/delete/renew operation; search and dedup handle spaced emails correctly
- **Session cache invalidation**: Cached panel session cleared when a server's auth mode changes
- **v3 Last User**: Shows recent clients only for the checked inbounds, refreshed on toggle, deduplicated

### 🚀 Dashboard & Performance
- **Lazy row mount**: Phase 3 incremental render — rows mount in chunks so the dashboard is interactive before all data arrives
- **Write-through cache**: Edits and renewals update the in-memory cache instantly; no stale data after panel operations across all panel types
- **Persian/Arabic digit search**: Search box converts Persian/Arabic digits to ASCII automatically
- **"Why?" error modal**: Server cards now have a button explaining fetch errors in plain language
- **Volume stats button**: Added to v3 server cards for quick traffic overview

### 💾 Backup & Migration
- **Upload progress UI**: Real-time progress bar during backup restore upload
- **512MB upload limit**: Raised from the old limit to support large database bundles
- **X-Accel-Redirect streaming**: Large backup downloads stream through Nginx to avoid Gunicorn timeout
- **Migration fixes**: Schema reset before pg restore; sync of `static/app-files` and `static/uploads` from old server

### 📢 Announcements
- **Media upload in editor**: Inline image/video upload directly in the announcement message editor
- **Popup modal type**: New announcement type that opens as a modal with a custom button label

### 🔒 SSL
- **Nginx auto-reload on renewal**: SSL renewal now reloads Nginx automatically (fixed 500 error when cert destination was root-owned)
- **Cert classification fix**: Certs classified by issuer vs. subject, not file path, so self-signed and CA certs are identified correctly

### 🎯 Royalty
- **Deduplication**: Idle list deduped by email-per-server (v3 multi-inbound)
- **Synchronous scan**: Replaced fragile background-job scan with synchronous execution on the index request
- **Index snapshots**: Constant-cost baseline scan regardless of window size

### 🛠 Other Fixes
- **Backup Database button**: Added to server management cards on the Servers page
- **Reseller owner badges**: Multi-select owner filter for admin on the Packages page
- **Subscription history**: Compact inline renewal line; paginated history table (10/page); renewal days marked in table
- **Ownership anchor**: Client owner anchored to panel UUID so server/inbound edits don't lose ownership
- **Email auto-sanitize**: Email field in Add Client modal strips illegal characters on input
- **Emoji on iOS**: Emoji in messages now preserved correctly on iOS devices
- **SSL auto-apply after upload**: SSL cert applied automatically after upload completes
- **Server list refresh**: Server list no longer stale after editing a server
- **Pricing fix**: Dynamic tier price no longer overwritten by package loader on re-open
- **Shadowsocks fix**: Non-v3 update/delete operations restored for Shadowsocks protocol

### 🐛 Bug Fixes & Improvements
- **3x-ui v3.3.1 compatibility (CSRF)**: v3.3.1 added a CSRF middleware in front of `POST /login` (and every other cookie-session state-changing route), so EVE could no longer log in to upgraded panels — cookie-login servers returned `403`, which surfaced as `502 Bad Gateway` on the EVE subscription page when the client wasn't cached. EVE now fetches a token from `GET {basePath}/csrf-token` and pins it as the `X-CSRF-Token` header on the panel session before logging in, so login and all later `/panel/api/*` POSTs (add/update/delete client, reset traffic, backup, onlines) pass the guard. Verified live against a v3.3.1 panel (login `success:true` with the token, `403` without it). Fully backward compatible: older panels (≤3.3.0, v3, pre-v3) have no `/csrf-token` route and ignore the header, and API-token (Bearer) servers are unaffected (CSRF is bypassed for token auth).
- **HTTPS-only panel self-heal**: A server saved with an `http://` host pointing at an SSL-enabled panel (HSTS + Secure cookies) failed with a bare `ConnectionError` shown as "Error testing connection". Testing a server now auto-detects this and rewrites the host to `https://` when — and only when — https answers and http does not, so plaintext panels are left untouched.

## [1.4.2] - 2025-12-12

### 🐛 Bug Fixes & Improvements
- **Reseller Visibility**: Fixed issue where clients were hidden from resellers due to missing inbound IDs (implemented loose matching).
- **Traffic Formatting**: Improved traffic display to dynamically show KB/MB/GB/TB units.
- **UI Alignment**: Fixed action button alignment on desktop (right-aligned) and mobile (left-aligned).
- **Server List**: Fixed bug where server list in modals would be empty after status updates.
- **Search Autofill**: Implemented fix to prevent browser autofill on the search input.

## [1.4.1] - 2025-12-11

### ✨ Protocol Link Support
- Full support for direct client links for all 3x-ui protocols (vmess, vless, trojan, shadowsocks) with proper ws/grpc/tcp, TLS/Reality, and plugin parameters.
- Improved link generation logic for all supported protocols.

### 🐛 Bug Fixes & Improvements
- Webpath fixes for custom panel paths (login, API, panel URLs).
- Expiry display and UI tweaks.
- Version and tag update logic improvements.

## [1.3.0] - 2025-12-09

### ✨ New Features
- **FAQ Platform Support**: Added ability to categorize FAQs by platform (Android, iOS, Windows).
- **FAQ Editor**: Enhanced FAQ editor with RTL/LTR support and improved toolbar.
- **Subscription Page**: Added platform filtering for Apps and FAQs.

### 🎨 UI/UX Improvements
- **Upload UI**: Redesigned file upload inputs with a modern button-and-spinner style.
- **Dropdowns**: Standardized OS and Platform selection to use consistent dropdown components on the Subscription page.
- **Icons**: Added platform-specific icons to selection menus.

## [1.2.1] - 2025-12-06

### ✨ New Features
- **Settings Page**: Introduced a dedicated settings area for managing application configurations.
- **Notification Templates**: Added a system to create and manage dynamic text templates for client creation notifications.
- **Backup & Restore**: Implemented full database backup and restore functionality with download/delete options.

### 🎨 UI/UX Improvements
- **Card Design**: Updated template management to use a modern card-based layout.
- **Number Formatting**: Applied global thousands separators for better readability of prices and volumes.
- **Visual Polish**: Improved button styles, spacing, and hover effects in the Settings and Backup sections.

## [1.2.0] - 2025-12-06

### ✨ New Features
- **Version Checking**: Added automatic version checking against GitHub Releases.
- **New Client Modal**: Enhanced success modal with QR codes and copyable subscription details.
- **Transaction Logging**: Expanded transaction logging to include Admin actions when costs are involved.

### 🎨 UI/UX Improvements
- **Renew Modal**: Redesigned to match the Purchase modal layout for consistency.
- **Receipts UI**: Improved card selection with a grid layout and copy-to-clipboard functionality.
- **Typography**: Standardized Persian text using the "Vazirmatn" font.
- **Sidebar**: Added a "New Release" badge with visual indicators.

### 🐛 Bug Fixes
- Fixed `TemplateAssertionError` in `base.html`.
- Resolved issue where Admin transactions were not being logged in history.

## [1.0.0] - 2024-12-01

### 🎉 Initial Release

This is the first stable release of Eve - Xui Manager with comprehensive features for managing multiple X-UI VPN panels.

### ✨ Features

#### Security
- Rate limiting: 5 login attempts per minute to prevent brute-force attacks
- Secure cookies with HTTPONLY and SAMESITE flags
- PBKDF2 password hashing with salt
- Failed login attempt logging with IP addresses
- Environment-based configuration for sensitive credentials
- Session timeout after 7 days
- Superadmin role for admin management

#### Dashboard
- Multi-server support (unlimited X-UI panels)
- Auto-detection of panel types (Sanaei 3X-UI vs Alireza X-UI)
- Real-time statistics: servers, inbounds, clients, traffic
- Responsive sidebar navigation
- Mobile-friendly hamburger menu
- Configurable auto-refresh intervals
- Manual refresh button

#### Client Management
- Enable/disable clients
- Reset client traffic
- Renew clients with configurable days and volume
- "Start after first use" option for subscriptions
- 3-Type QR Codes per client:
  - Subscription QR Code (Copy Sub)
  - Subscription JSON QR Code (Copy JSON)
  - Direct Connection Link QR Code (Copy Direct)

#### Server Configuration
- Add/edit/delete X-UI servers
- Customizable subscription paths per server
- Customizable JSON paths per server
- Custom subscription ports (with fallback to panel port)
- Connection testing

#### Admin Management
- Create/edit/disable admin accounts
- Superadmin can manage other admins
- Last login timestamp tracking
- Enable/disable accounts without deletion

#### UI/UX
- Professional dark theme
- Responsive grid layouts
- Color-coded expiry badges (green/yellow/red)
- Jalali calendar dates
- Traffic display (upload ↑ / download ↓)
- Volume information (used / total)
- Optimized for mobile devices
- Touch-friendly buttons and spacing

### 🔧 Technical

#### Backend
- Python 3.11 with Flask framework
- PostgreSQL database with connection pooling
- Flask-Limiter for rate limiting
- Werkzeug for security features
- QR code generation with python-qrcode
- Jdatetime for Jalali calendar support

#### Frontend
- HTML5 with semantic markup
- CSS3 with CSS variables for theming
- Vanilla JavaScript (no framework dependencies)
- Responsive grid and flexbox layouts
- SVG icons for cross-browser compatibility

#### API
- RESTful JSON API
- Secure session-based authentication
- Login rate limiting (5/min)
- Global rate limits (200/day, 50/hour)

### 📋 Database
- Admins table with superadmin role support
- Servers table with full X-UI panel configuration
- PostgreSQL with secure connection pooling
- Pre-ping health checks for database connections

### 🚀 Deployment Ready
- Environment variable configuration
- PBKDF2 password hashing
- Secure cookie settings
- Failed attempt logging
- Session management with secure flags

### 📱 Responsive Design
- Desktop: 3-column QR code grid
- Tablet (1024px): 2-column grid
- Mobile (768px): 1-column grid with icon-only buttons
- Mobile header with auto-height flex wrapping
- Touch-optimized interface

### 🔒 Security Features Implemented
1. Rate limiting (5 attempts/minute)
2. Secure cookies (HTTPONLY, SAMESITE=Lax)
3. Password hashing (PBKDF2)
4. Failed login logging
5. Environment-based configuration
6. Session timeout (7 days)
7. Admin role-based access
8. Database connection pooling with health checks

### ✅ Quality Assurance
- Tested with Sanaei 3X-UI panels
- Tested with Alireza X-UI panels
- Mobile responsive testing
- Security hardening completed
- Performance optimized with connection pooling

### 📚 Documentation
- Comprehensive README.md
- Technical documentation in replit.md
- API endpoint documentation
- Configuration guide
- Security best practices

### 🐛 Known Limitations
- None at release

### 🙏 Special Thanks

This project was built with careful attention to:
- Enterprise security practices
- User experience across all device sizes
- Performance and reliability
- Clean, maintainable code

---

## Release Schedule

- **1.0.0** - December 1, 2024 (Current)

For feature requests and bug reports, please visit the GitHub issues page.
