# BNQO — Service Level Objectives

Status: Phase-0 planning document. Normative source: `docs/bnqo/RFP_DIGEST.md` (cited as §N). Companions: `IMPLEMENTATION_PLAN.md`, `TEST_STRATEGY.md`. All SLOs become live in Phase 6; performance SLIs are verified earlier by the benchmarks in `TEST_STRATEGY.md` §8. Windows are 30-day rolling unless stated otherwise.

## 1. SLIs and SLOs

### 1.1 Measurement availability

| SLI | Definition | SLO |
|-----|-----------|-----|
| SLI-1a Probe schedule execution | `executed_probe_windows / scheduled_probe_windows` per link, per direction, per test profile (§6). A window counts as executed if the agent attempted the probes on schedule (measured from agent WAL sequence continuity + heartbeat) | ≥99.5% monthly per link |
| SLI-1b Agent liveness | fraction of 1-minute heartbeats received from enrolled, non-maintenance agents (§23 `ReportHeartbeat`) | ≥99.0% monthly per agent |
| SLI-1c Reflector availability | successful authenticated reflector sessions / attempted sessions, excluding link-failure windows confirmed by independent signals (§1: never a single signal) | ≥99.9% monthly |

Notes: a failed measurement caused by a real link failure does NOT count against SLI-1a (the probe ran; the path failed) — the distinction is made via status model §8 (`unreachable`/`critical` vs `agent-unhealthy`). `maintenance` status windows (§8) are excluded from all availability SLIs.

### 1.2 Telemetry freshness (§25)

| SLI | Definition | SLO |
|-----|-----------|-----|
| SLI-2a Ingest latency | time from agent `UploadMeasurementBatch` ACK to the batch's records being queryable via `/v1/links/{id}/measurements` (§23) | p95 <5s, measured continuously per batch |
| SLI-2b Dashboard freshness | age of the newest displayed sample for an active link on the global overview / link detail views (§16) | p95 <15s for active links; ≥99% of active links fresher than 15s at any 5-min checkpoint |
| SLI-2c Telemetry staleness detection | time from last received batch to status `telemetry-delayed` being set (§8) | ≤2 missed upload intervals + 30s |

### 1.3 Detection quality (§9)

Measured against injected incidents (netem matrix, `TEST_STRATEGY.md` §4) each release, plus production-labeled incidents retrospectively.

| SLI | Definition | SLO |
|-----|-----------|-----|
| SLI-3a Alert precision | `true_alerts / (true_alerts + false_alerts)` where false = alert fired with no corresponding injected/confirmed incident | ≥90% per release validation; ≥85% in production review |
| SLI-3b Alert recall — hard failures | injected complete loss (L-05/L-06), 100% blocking (B-0x), service-failure scenarios detected | 100% (no misses tolerated) |
| SLI-3c Alert recall — degradation | injected sustained loss ≥5%, latency regression >baseline+50%, jitter above profile limit (§9 thresholds) | ≥95% per release validation |
| SLI-3d Evidence completeness | alerts carrying the full §9 evidence bundle (direction, loss vs baseline, cross-protocol confirmation, route/host correlation, confidence) | 100% of alerts |
| SLI-3e Status honesty | windows where status shows `healthy` while data is absent (forbidden by §8) | 0 (any occurrence is a sev-2 defect) |

### 1.4 Time to detect (TTD) per incident class

| Incident class | Example scenarios | TTD SLO |
|----------------|-------------------|---------|
| Complete link loss (both directions) | L-06 | ≤60s from onset |
| Directional failure (one-way black hole) | L-05, B-02 | ≤90s |
| Sustained degradation (loss ≥5%, latency/jitter regression) | L-03/L-04, D-05, J-03 | ≤5 min (≥3 consecutive windows per §9) |
| Micro-outages / burst loss | L-08…L-10 | ≤5 min, aggregated (per-window bursts reported immediately as metrics; alert on repeat pattern §9) |
| Route change with degradation | T-01, T-02, T-06 | ≤10 min (diagnostic MTR cooldown per §6 Profile C respected) |
| MTU black hole | M-02 | ≤10 min from first failing probe series |
| Agent offline / telemetry stale | H-07, P-05 | ≤2 missed intervals +30s (SLI-2c) |
| Clock unsynchronized | P-09 | ≤5 min; OWD gated `invalid-clock-sync` immediately on detection (§3) |
| Security events (replay, auth failure, cert revocation, config-signature failure) | S-01…S-13, P-11…P-13 | security-view visibility ≤60s; alert per §17 policy |

### 1.5 Agent resource budgets (§25; enforced, not just observed)

| SLI | Definition | SLO |
|-----|-----------|-----|
| SLI-4a Idle CPU | agent CPU during Profile A default schedule | avg <1% of one core, p95 <2% (24h window per agent) |
| SLI-4b Memory | agent VmRSS incl. WAL spooling during 24h collector outage | p99 <150MB |
| SLI-4c Disk | WAL + artifacts footprint | never exceeds configured quota; OS filesystem never filled (§15) — any breach is sev-1 |
| SLI-4d Bandwidth | probe egress vs configured `max_bandwidth` per profile/job | observed ≤ configured +5% burst |

### 1.6 Control plane and pipeline availability (§26)

| SLI | Definition | SLO |
|-----|-----------|-----|
| SLI-5a Management REST availability | non-5xx responses / total valid requests at LB | ≥99.9% monthly |
| SLI-5b Agent gRPC availability (enroll/config/jobs) | successful RPCs / valid attempts | ≥99.9% monthly |
| SLI-5c Ingest gateway availability | accepted valid batches / valid batch attempts (client-side retryable failures count against) | ≥99.95% monthly |
| SLI-5d CP outage impact | during any CP outage, agent measurement continuity (probe windows executed, results spooled) | 100% — CP outage must never stop configured tests (§2) |
| SLI-5e Dashboard availability | non-5xx on dashboard + query API | ≥99.5% monthly |

### 1.7 Data durability (§15, §26)

| SLI | Definition | SLO |
|-----|-----------|-----|
| SLI-6a WAL durability | committed (locally sequenced) records lost per crash/kill event | 0 (verified by `kill -9` soak; any loss = release blocker) |
| SLI-6b Pipeline durability | batches ACKed by ingest gateway but never reaching durable storage | ≤0.01% monthly; target 0 via durable MQ (§4.4) |
| SLI-6c Offline buffer | agent retains ≥72h of measurements during CP/collector outage at default profile rates | 100% of agents (§25) |
| SLI-6d Dedup correctness | logically duplicated records after reconnect/replay injection | 0 |
| SLI-6e Backup/restore | quarterly restore drill: CP PostgreSQL + object storage + config/secrets restored and verified | 100% success; restore time within RTO below |

### 1.8 RPO / RTO (§26)

| Component | RPO | RTO | Mechanism |
|-----------|-----|-----|-----------|
| Agent-local data (un-uploaded) | 0 (WAL) | ≤5 min after connectivity returns (catch-up begins; full drain rate-limited) | WAL ≥72h, ordered resend, dedup (§15) |
| Telemetry pipeline (MQ → stores) | ≤1 min (durable MQ retention) | ≤30 min | replicated MQ, stream-processor checkpoint replay (§4.4, §26) |
| CP metadata (PostgreSQL: agents/configs/policies/audit) | ≤5 min (WAL archiving / streaming replica) | ≤60 min | DB replication + encrypted backups, restore-tested (§26) |
| Object storage (diagnostic bundles) | 0 (versioning) | ≤60 min | object-storage versioning (§26) |
| Dashboard/query tier | n/a (derived) | ≤60 min | stateless, rolling redeploy |
| Full-region DR | ≤15 min | ≤4h | DR runbook (§26), documented and drilled pre-release and annually |

Compatibility SLO (§26): agents one version behind/ahead of CP must remain fully functional (measurement + upload); verified in pre-release upgrade/downgrade matrix.

## 2. Error budgets and exhaustion policy

Error budget per SLO = `1 − SLO` over the 30-day rolling window (e.g., SLI-5a 99.9% ⇒ ~43.2 min/month of 5xx; SLI-1a 99.5% ⇒ ~3.6h/month of missed probe windows per link).

| Budget state | Policy |
|--------------|--------|
| >50% remaining | Normal: feature work proceeds per phase plan |
| 25–50% remaining | Yellow: reliability review in weekly ops meeting; risky rollouts deferred |
| <25% remaining | Orange: feature freeze for the affected component; only reliability fixes and Phase-gating work merge; canary pace halved |
| Exhausted | Red: full freeze on the affected component, mandatory postmortem within 5 business days, remediation plan with owner + dates, rollout of new versions blocked until budget recovers to ≥50% or sponsor grants a written exception |

SLO breaches from force-majeure path conditions (real link failures Iran↔outside) do not consume measurement-availability budget for SLI-1a (probe executed) but DO consume SLI-2b/SLI-5x budgets if BNQO's own infrastructure caused the gap. Attribution is made from the §8 status model evidence, never inferred.

## 3. Alert SLOs

Beyond TTD (§1.4): alert delivery SLOs per §17.

| SLI | Definition | SLO |
|-----|-----------|-----|
| SLI-A1 Delivery latency | incident opened → notification dispatched to primary channel (email/Telegram/webhook per §17) | p95 ≤60s |
| SLI-A2 Delivery success | notifications accepted by channel / attempted (channel-side outages excluded after 3 retries with backoff) | ≥99.5% monthly |
| SLI-A3 Dedup/grouping correctness | duplicate notifications for one incident (same root event, flapping link) | ≤1 per incident per channel per grouping window (§17 dedup/grouping) |
| SLI-A4 Silence/maintenance correctness | alerts fired during a configured maintenance window (§8, §17) | 0 |

## 4. Monitoring the monitor (§30 Phase 6)

BNQO must not fail silently. Meta-monitoring is independent of the measured-path infrastructure:

1. **Independent blackbox canary**: a minimal external watcher (third region, separate credentials, separate notification path) executes synthetic checks against CP REST health, ingest gateway, and dashboard query API every 60s; alerts out-of-band if any fails 3 consecutive checks.
2. **Canary measurement**: a permanently injected synthetic test series (distinguished `test_id`, negligible rate) flows agent → ingest → storage → query; any break in the canary chain pages the operator within 5 min, independent of real-link alerting.
3. **Pipeline meta-metrics** with dead-man's-switch alerts: batches ingested/min vs 7-day baseline (alert if <50% without explanation), agents reporting vs enrolled (alert on >5% drop), WAL spool depth aggregates, MQ lag, stream-processor checkpoint age. Silence of these metrics themselves triggers the watcher in (1).
4. **Alert-path verification**: monthly synthetic incident injection (game day) proves end-to-end detection + delivery per §1.4 and §3; results recorded against SLI-3a/3b/3c.
5. **Audit-path verification**: sampled sensitive operations monthly; 100% must have corresponding audit events (§22 insider-threat control).
6. **SLO dashboard**: all SLIs above computed from pipeline data by an independent recording rule set; budget burn rates (1h/6h/3d) with §2 thresholds; reviewed weekly in ops, monthly with sponsor.
7. **Dependency watch**: collector/DB/MQ/object-storage health and version drift vs §26 HA requirements; cert expiry horizon for all identities (§17 "cert expiring").

## 5. Review cadence

- SLO definitions: revisited at each phase exit (M1–M6) and after every sev-1/sev-2 postmortem.
- Targets may be tightened, never loosened without sponsor sign-off recorded as an ADR (`docs/adr/`).
