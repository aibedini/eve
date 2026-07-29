# BNQO — Threat Model (STRIDE)

Document status: Phase 0 design artifact (normative for Phases 1–6)
Methodology: STRIDE-per-element with risk scoring, per RFP §22 (mandatory threat model)
Scope baseline: RFP_DIGEST.md §1–§31. All section references (`§n`) are to `RFP_DIGEST.md`.
Companion document: `SECURITY_CONTROLS.md` — every `BNQO-SEC-xxx` identifier below is defined there.

---

## 1. Methodology

1. Decompose the system into components and data flows (§3 below).
2. Enumerate trust boundaries (§4 below).
3. For every component/data flow crossing a trust boundary, derive threats using STRIDE categories: **S**poofing, **T**ampering, **R**epudiation, **I**nformation Disclosure, **D**enial of Service, **E**levation of Privilege.
4. The register (§7) covers **all 28 threats mandated by RFP §22** plus one additional Repudiation threat (TM-014) added so all six STRIDE categories are represented (§22 has no explicit repudiation entry; audit-trail integrity is required by §11/§14).
5. Each threat is scored for Likelihood and Impact (H/M/L with rationale), mapped to existing and required controls, assigned a residual risk, a concrete validation test, and an owner.

### Risk scales

**Likelihood**
- **H** — exploitable by a remote unauthenticated or low-skill actor, or by the expected on-path adversary, with public tooling.
- **M** — requires a foothold (valid but limited credentials, on-path position, or insider access) or moderate skill.
- **L** — requires deep compromise (root on a hardened host, HSM/KMS breach) or a rare conjunction of failures.

**Impact**
- **H** — loss of measurement integrity at scale (wrong operational decisions), code execution on measured servers, cross-tenant data exposure, or capture/amplification abuse of BNQO infrastructure against third parties.
- **M** — single-link/single-tenant integrity or availability loss, limited data exposure, audit gaps.
- **L** — cosmetic, self-healing, or fully detectable-and-attributable degradation with no decision impact.

**Risk** = Likelihood × Impact on a 3×3 matrix: H×H → **Critical**, H×M / M×H → **High**, M×M → **Medium**, any L combination → **Low/Medium** (L×H and H×L → Medium, L×M/M×L → Low, L×L → Low).

### Conventions

- **Existing Control** = a security property already mandated by the baseline architecture/requirements (§2–§9, §27), i.e. "designed-in" before this threat model added anything. Because Phase 0 is greenfield, these are *specified*, not yet *implemented*; they are verified by the tests in §7/§9 and the acceptance mapping in `SECURITY_CONTROLS.md` §5.
- **Required Control** = additional control from the `BNQO-SEC-xxx` catalogue (`SECURITY_CONTROLS.md`), introduced or made concrete by this threat model.
- **Residual Risk** = risk after Required Controls are implemented and their validation tests pass.
- Validation tests reference the mandatory lab security scenarios of **RFP §24** (quoted in italics) and the security acceptance criteria of **RFP §25**.

---

## 2. Scope

**In scope:** probe agent, secure reflector, local WAL, agent↔reflector probe path, agent→collector→queue→processor→storage telemetry pipeline, control plane (identity, enrollment, scheduler, signed config/jobs, management REST API, agent gRPC API), React dashboard + OIDC, CI/CD build/release pipeline, secrets management, host runtime environment of agent/reflector.

**Out of scope (Phase 0):** physical security of datacenters, the security of third-party IdP/Vault/cloud services beyond correct integration, DDoS of the management plane by the public internet at volumetric scale (handled by upstream provider; management API is not internet-exposed by default), and the monitored production services themselves (panel, tunnels) except where BNQO probes them.

**Adversary model.** Given the deployment context (Iran↔outside paths), the on-path adversary is assumed capable of: passive observation, active packet injection/modification/drop, DPI-based protocol interference, TCP RST injection, DNS/SNI/TLS interference, and BGP-level route manipulation. Additionally modeled: malicious or curious tenant user, external unauthenticated attacker against the management API, compromised agent host, compromised control-plane operator (insider), and supply-chain attacker (dependency or CI).

---

## 3. System decomposition

| # | Component | Plane | Key assets | Hardening baseline |
|---|-----------|-------|-----------|--------------------|
| C1 | Probe Agent (Rust, systemd, non-root) | Measurement + Control edge | SVID private key, WAL (72h of results), signed config, job executor | §4.1, §19 |
| C2 | Secure Reflector (Rust, per endpoint) | Measurement | session keys, peer registry, replay window state | §4.2, §19 |
| C3 | Agent gRPC API (Enroll/Rotate/ControlStream/Jobs/Upload) | Control | enrollment tokens, job signing key (verify side), SVID issuance | §23 |
| C4 | Control plane core (identity, scheduler, policy, config versioning, approval workflow) | Control | job signing key, CA/SPIRE signing capability, tenant DB | §4.3 |
| C5 | Management REST API + API gateway | Control | OIDC sessions, RBAC, tenant data | §11, §23 |
| C6 | Regional OTel Collector / ingest gateway | Observability | mTLS termination, batch validation | §4.4 |
| C7 | NATS JetStream (durable queue) | Observability | queued telemetry | §4.4, §27 |
| C8 | Stream processor (validation/enrichment) | Observability | dedup state, schema registry | §4.4 |
| C9 | Storage: VictoriaMetrics, ClickHouse, PostgreSQL, object storage | Observability | measurements, routes, audit, pcaps, diagnostic bundles | §14 |
| C10 | React dashboard + OIDC client | Observability | user sessions, tokens | §16, §27 |
| C11 | CI/CD + release pipeline | Supply chain | signing keys (Cosign/KMS), source, SBOM | §21 |
| C12 | Secrets infrastructure (Vault/KMS, optional TPM/HSM) | All | root/intermediate keys, DB credentials | §20 |

**Primary data flows:**
- F1: Agent ↔ Reflector authenticated UDP probe packets (§5-002 fields: protocol_version, session_id, test_id, sequence_number, timestamps, nonce, authentication_tag).
- F2: Agent → Collector OTLP/gRPC mTLS measurement batches (agent_id + sequence_number + config_version).
- F3: Collector → JetStream → stream processor → storage (internal, but zero-trust: mutually authenticated, per-request authorized).
- F4: User → Management REST API → PostgreSQL (OIDC bearer, RBAC).
- F5: Control plane → Agent control stream (signed typed jobs, signed config) over mTLS gRPC.
- F6: CI → artifact registry → servers (signed packages/images, verified before deploy).

---

## 4. Trust boundaries

| ID | Boundary | Crosses flows | Description | Dominant threats |
|----|----------|---------------|-------------|------------------|
| **TB1** | Agent ↔ Reflector probe path over hostile internet | F1 | Authenticated UDP probes traverse networks under an active on-path adversary (DPI, injection, rerouting). No assumption of path confidentiality; integrity comes from AEAD tags, not the network. | Replay, reflection/amplification, forgery, measurement interference, route manipulation |
| **TB2** | Agent → Collector mTLS telemetry | F2 | Agent crosses from an untrusted, possibly compromised host network into the observability plane. Collector is the policy enforcement point: no valid identity, no ingest; agents never touch storage (§4.4). | Agent spoofing, metric poisoning, replay, oversized payload, identity theft |
| **TB3** | Collector → backend internal (JetStream, processor, storage) | F3 | "Internal" is *not* a trust zone (NIST 800-207): every hop mutually authenticated and per-request authorized. Queue and DB are reachable only from authenticated pipeline identities. | Queue flooding, tampering in queue, privilege escalation, lateral movement |
| **TB4** | User → Management API / dashboard | F4 | OIDC-authenticated humans and automation cross from arbitrary networks into the control plane's management surface. Per-object tenant authorization enforced server-side. | Broken auth, cross-tenant access, enumeration, brute force, CSRF/XSS token theft |
| **TB5** | Control plane → Agent control stream | F5 | The most safety-critical boundary: jobs and config flow *down* to privileged-ish measurement software. Everything crossing is typed, schema-valid, signed, expiry-bounded, and policy-checked at the agent — the agent does not trust the network and does not fully trust the CP either (defense against CP compromise, TM-029). | Shell exec via job, config tampering, downgrade, SSRF via target, DoS via job flood |
| **TB6** | Build/release pipeline → production | F6 | Source → CI → signed artifact → verified deploy. Signing keys in KMS; provenance attested; verification gate before any host runs new code. | Malicious dependency, pipeline compromise, artifact substitution, unsigned/malicious update |

---

## 5. Threat register

Entries are grouped by STRIDE category. IDs `BNQO-TM-###` are stable references for tests and audits.

### 5.1 Spoofing

#### BNQO-TM-001 — Agent spoofing
- **Asset:** telemetry pipeline integrity; control stream authorization.
- **STRIDE:** Spoofing (TB2, TB5)
- **Attack path:** attacker without a legitimate agent identity connects to the collector or CP gRPC endpoint presenting a forged/self-signed certificate, a certificate with a wrong SPIFFE ID, or a copied enrollment token, attempting to inject measurements or receive jobs.
- **Likelihood: H** — endpoints are reachable from the agent networks by design; forgery requires no foothold and is the first thing an on-path or external attacker tries.
- **Impact: H** — successful spoofing poisons all downstream measurement truth and could receive jobs/config meant for a real agent.
- **Existing control:** static API keys forbidden (§10 SR-IDENTITY-001); mTLS required on all M2M (§11).
- **Required control:** BNQO-SEC-001 (SPIFFE/SPIRE X.509-SVID), -002 (node+workload attestation), -003 (single-use enrollment token, TTL ≤5 min), -006 (server-side agent authorization: agent may only send its own telemetry / receive its own jobs), -007 (TLS 1.3 only), -093 (ingest binds batch `agent_id` to authenticated SVID identity).
- **Residual risk: Low.**
- **Validation test:** RFP §24-security *"forged cert"* — connect with (a) self-signed cert, (b) valid cert for a different SPIFFE ID, (c) cert signed by wrong CA; all must be rejected at handshake or at first RPC, each producing a security audit event (§16 security view). Also §25: *"no telemetry without valid identity."*
- **Owner:** Identity Lead.

#### BNQO-TM-002 — Agent certificate / private-key theft
- **Asset:** agent SVID private key; WAL encryption key.
- **STRIDE:** Spoofing (TB2)
- **Attack path:** attacker with host access (or via a co-located compromised service) reads `/var/lib/bnqo-agent/` key material, then impersonates the agent from another host.
- **Likelihood: M** — requires host compromise first (agent hosts are single-purpose and hardened), but key material on disk is a realistic exfiltration target once in.
- **Impact: H** — full agent impersonation: forged telemetry, job/config receipt, valid-looking attack traffic toward peers until revocation.
- **Existing control:** dedicated non-root user, minimal caps (§4.1); cert revocation supported (§4.3).
- **Required control:** BNQO-SEC-004 (per-agent keypair generated on-host, 0400/`bnqo-agent`, never transmitted), -073 (TPM 2.0 sealing where available, non-exportable key), -005 (short SVID TTL ≤1h, auto-rotation, revocation enforced at collector/CP within ≤60s), -041 (WAL key separate from identity key so WAL stays readable only on-host), -016 (audit of auth anomalies: same identity from new IP/ASN → alert).
- **Residual risk: Low–Medium** (bounded by ≤1h SVID lifetime + anomaly alerting).
- **Validation test:** §24-security *"stolen session"*-class: copy agent key to a second host, connect; expect detection (concurrent-use / node-attribute mismatch from attestation, §24 *"invalid config signature"* pipeline) and revocation; verify revoked cert rejected per §25 *"revoked cert rejected immediately/within window."*
- **Owner:** Identity Lead.

#### BNQO-TM-003 — Telemetry replay
- **Asset:** freshness/integrity of measurement stream; reflector statistics.
- **STRIDE:** Spoofing (TB1, TB2)
- **Attack path:** on-path adversary captures valid authenticated UDP probe packets or OTLP batches and replays them later/elsewhere: stale probes to the reflector (inflating/deflating loss and RTT), stale batches to the collector (masking a real outage by re-injecting "healthy" data).
- **Likelihood: H** — passive capture is free for the assumed on-path adversary; replay needs no key material.
- **Impact: H** — silently wrong dashboards during a real incident is the worst-case failure of a monitoring system (§8: never show healthy when data absent/stale).
- **Existing control:** every report carries agent_id + increasing sequence_number + config_version (§4.1); reflector enforces timestamp/sequence/nonce checks with replay window, rejects old/dup (§4.2).
- **Required control:** BNQO-SEC-090 (AEAD tag covers timestamp+nonce+sequence; sliding replay window ≥10k packets; reject outside window), -015 (server-side: per-agent monotonic sequence, timestamp skew bound, idempotency keys), -043 (server-side dedup on (agent_id, sequence); out-of-window batch → `replay-rejected` audit), -094 (data-freshness detection: status `telemetry-delayed`, never `healthy` on replayed data).
- **Residual risk: Low.**
- **Validation test:** RFP §24-security *"replay"* and *"invalid HMAC"* — record/replay probes and batches; expect silent drop at reflector, rejection+audit at ingest, dashboard shows stale/unknown rather than healthy. §25: *"replay rejected+audited."*
- **Owner:** Measurement Core Lead.

#### BNQO-TM-004 — Expired / revoked certificate acceptance (and expiry self-DoS)
- **Asset:** identity enforcement continuity.
- **STRIDE:** Spoofing (TB2, TB5) with a Denial-of-Service facet
- **Attack path:** (a) an endpoint accepts an expired or revoked SVID because time validation or revocation checking is missing/fail-open; (b) conversely, rotation failure strands all agents with expired certs → total telemetry outage (self-inflicted, but indistinguishable from attack).
- **Likelihood: M** — a classic implementation bug class (fail-open validation, skipped CRL fetch); rotation bugs are common in young PKI deployments.
- **Impact: H** (a: revoked identity keeps working; b: fleet-wide measurement blindness).
- **Existing control:** short cert lifetime + auto-rotation + revocation required (§10 SR-IDENTITY-001); `cert expiring` alert (§17).
- **Required control:** BNQO-SEC-005 (rotation at ≤50% TTL, jittered; revocation via SPIRE revocation + CRL, cached fail-*closed* at collector; alert at 7d/1d), -094 (clock-quality gating so cert time validation is not fooled by local clock skew), -016 (audit of every auth failure class incl. expired/revoked).
- **Residual risk: Low.**
- **Validation test:** RFP §24 platform failure *"cert expiration/revocation"* — force expiry mid-run and revoke a live cert; verify rejection within the window (§25: *"revoked cert rejected immediately/within window"*) and that rotation completes with zero missed reporting windows in the happy path.
- **Owner:** Identity Lead.

#### BNQO-TM-005 — Brute force / credential guessing
- **Asset:** OIDC user accounts, enrollment tokens, session cookies.
- **STRIDE:** Spoofing (TB4)
- **Attack path:** password spraying against the IdP or any local login; guessing enrollment tokens; cookie/session fixation against the dashboard.
- **Likelihood: M** — management plane exposure is limited, but any reachable login attracts spraying; enrollment tokens are high-entropy and short-lived, which lowers this from H.
- **Impact: M–H** — an operator account yields config/job control within its RBAC role; a viewer yields measurement intelligence.
- **Existing control:** OIDC/OAuth2 for users, MFA for sensitive roles (§11); one-time, few-minute enrollment tokens (§10 SR-IDENTITY-002).
- **Required control:** BNQO-SEC-010 (MFA WebAuthn/TOTP, enforced for operator+ roles), -014 (per-IP and per-account rate limits + exponential lockout/backoff at gateway and IdP), -021 (Secure/HttpOnly/SameSite=Strict cookies, absolute session lifetime ≤24h),-003 (token entropy ≥128 bit, single-use, audited), -022 (step-up re-auth for dangerous ops).
- **Residual risk: Low.**
- **Validation test:** RFP §24-security *"rate-limit bypass"* and *"stolen session"* — scripted spraying must hit 429s and lockout; replayed/rotated-out session cookie must fail; enrollment token reuse must fail and alert.
- **Owner:** Control Plane Lead.

### 5.2 Tampering

#### BNQO-TM-006 — Metric poisoning
- **Asset:** measurement records in TSDB/ClickHouse; alerting decisions.
- **STRIDE:** Tampering (TB1, TB2)
- **Attack path:** (a) on-path adversary shapes drops/delays selectively at probe traffic to manufacture "degraded" or mask real degradation; (b) a compromised agent reports fabricated counters; (c) malformed values (NaN, absurd percentiles, wrong units) injected at ingest to corrupt baselines.
- **Likelihood: H** — (a) is intrinsic to measuring through a hostile network; (c) requires no credentials if validation is weak.
- **Impact: H** — false incidents or suppressed real ones; poisoned baselines corrupt §9 detection thresholds long-term.
- **Existing control:** final state from multiple independent signals, never one probe type (§1); bidirectional measurement; multi-window thresholds + confidence score (§9).
- **Required control:** BNQO-SEC-095 (ingest field-bound validation: ranges, monotonic counters, unit/schema conformance; reject+quarantine invalid), -043 (dedup), -094 (cross-signal corroboration rule: no state change without ≥2 independent signals — codifies §1/§9); variant (b) is bounded by the compromised-agent controls of TM-027 (BNQO-SEC-006, -065).
- **Residual risk: Medium** — (a) cannot be fully prevented, only detected and labeled; confidence scoring + corroboration is the mitigation.
- **Validation test:** §24 netem matrix (loss/latency/jitter each direction) + *"excessive jobs/oversized telemetry"* class fuzzing of ingest with out-of-range values; verify quarantine + audit, and that single-signal anomalies never flip status alone.
- **Owner:** Pipeline Lead.

#### BNQO-TM-007 — Result tampering (WAL, in-transit, at-rest)
- **Asset:** stored measurement truth; audit trail.
- **STRIDE:** Tampering (C1 local disk, TB2, C9)
- **Attack path:** modify spooled WAL records on the agent host (root-level attacker or co-tenant process); tamper with batches in the queue (compromised pipeline identity); direct DB modification (compromised DBA/credential).
- **Likelihood: M** — requires a foothold in all three variants.
- **Impact: H** — retroactively rewritten history defeats incident forensics and SLA evidence.
- **Existing control:** per-record checksum + increasing sequence in WAL (§15); mTLS + server-side dedup (§15); PostgreSQL for audit (§14).
- **Required control:** BNQO-SEC-040 (WAL per-record CRC32C + chained hash so record edits break the chain), -041 (WAL AEAD encryption at rest — ciphertext edits are detected on read), -042 (WAL dir 0700 `bnqo-agent`, `ProtectSystem=strict` with WAL as the only writable path), -016 (audit log hash-chained and exported to WORM object storage per §14), -043 (ingest dedup + gap detection on sequence).
- **Residual risk: Low.**
- **Validation test:** bit-flip a spooled WAL record → agent must detect on read/upload and report `wal-integrity-error`; modify a queued batch → schema/hash validation rejects at stream processor; §25 reliability *"WAL intact after crash."*
- **Owner:** Measurement Core Lead.

#### BNQO-TM-008 — Route forgery
- **Asset:** route/hop records; route-change alerting.
- **STRIDE:** Tampering (TB1)
- **Attack path:** on-path adversary manipulates TTL-exceeded ICMP responses (spoofed hop addresses, injected phantom hops, suppressed real hops) so MTR/traceroute draws a false topology or hides a reroute through an inspection point.
- **Likelihood: H** — hop-by-hop ICMP is entirely under on-path control in the assumed environment.
- **Impact: M** — wrong topology misleads diagnosis, but route data is corroborated by loss/latency signatures and never alone declares root cause (§7).
- **Existing control:** route hash + change detection, Paris/flow-stable traceroute, multi-protocol (TCP/UDP/ICMP) probes (§5-006); "mid-hop loss without downstream loss ≠ path failure" (§5-006).
- **Required control:** BNQO-SEC-094 (corroboration rule: route-change alert requires matching loss/latency evidence or multi-protocol agreement), -095 (hop-record validation: RFC1918/bogon filtering, hop monotonicity, destination-reached consistency).
- **Residual risk: Medium** — accepted residual: we detect inconsistency, we cannot force truthful hops.
- **Validation test:** §24 route scenarios — *"route flap, hop change, silent hop, ASN change, mid-hop latency increase"*; plus adversarial lab: forged ICMP TTL-exceeded from phantom addresses must be flagged `low-confidence`, not change status alone.
- **Owner:** Measurement Core Lead.

#### BNQO-TM-009 — Config tampering
- **Asset:** agent signed configuration (targets, rates, thresholds).
- **STRIDE:** Tampering (TB5, C1 disk)
- **Attack path:** modify config in transit (MitM between CP and agent), replace the on-disk last-valid config during CP outage, or rollback to an older config with weaker limits (see TM-010).
- **Likelihood: M** — transit is mTLS-protected, so the realistic vector is on-disk replacement by a host-level attacker or a CP bug.
- **Impact: H** — config controls what the agent attacks (targets/rates): tampered config = SSRF/DoS primitive (TM-025/TM-019).
- **Existing control:** agent verifies digital signature of configs; keeps last valid config during outage; invalid config never replaces valid one (§4.1, §25).
- **Required control:** BNQO-SEC-031 (ed25519 signature over canonical encoding; agent pins CP config-signing pubkey; key in KMS), -032 (monotonic `config_version`, expiry, rollback rejection), -042 (config file 0600 `bnqo-agent`, read-only FS elsewhere), -035 (audit `config-signature-failure` events to security view, §16).
- **Residual risk: Low.**
- **Validation test:** RFP §24 platform *"invalid config signature"* — tampered/unsigned/wrong-key/old-version configs rejected, last valid retained, audit event emitted; §25: *"invalid config never replaces valid one."*
- **Owner:** Control Plane Lead.

#### BNQO-TM-010 — Protocol / security downgrade
- **Asset:** TLS version/cipher floor; signed-config format; probe protocol version.
- **STRIDE:** Tampering (TB1, TB2, TB5)
- **Attack path:** force TLS 1.2 or weak ciphers in mTLS handshake; strip security fields by negotiating an older `protocol_version` on the probe path; serve an old-but-validly-signed config/job format lacking newer policy fields.
- **Likelihood: M** — TLS downgrade is mitigated by design (1.3-only); versioned-payload downgrade is the subtler residual and a common parser bug class.
- **Impact: H** — weaker crypto or missing policy enforcement re-opens TM-001/003/024.
- **Existing control:** TLS 1.3 required (§11); versioned schemas everywhere (§13, §23); `protocol_version` in every probe packet (§5-002).
- **Required control:** BNQO-SEC-007 (rustls configured TLS 1.3 only; ALPN pinned; no renegotiation), -032 (minimum-accepted `config_version`/`protocol_version` policy signed into config; agents refuse lower), -090 (unknown/lower protocol_version → silent drop + stat).
- **Residual risk: Low.**
- **Validation test:** attempt TLS 1.2 handshake (must fail); replay v0-format probe packets and configs (must drop/reject + audit); fuzz version fields (`cargo-fuzz` target on decoders, BNQO-SEC-081).
- **Owner:** Identity Lead + Measurement Core Lead.

#### BNQO-TM-011 — Time manipulation
- **Asset:** one-way-delay validity; cert/expiry validation; windowed detection.
- **STRIDE:** Tampering (C1, TB1)
- **Attack path:** malicious/ spoofed NTP responses shift agent clock; host-level attacker uses `clock_settime`; CP's NTP source poisoned. Effects: bogus OWD, expired certs accepted or valid ones rejected, replay-window confusion, scheduled tests firing off-window.
- **Likelihood: M** — unauthenticated NTP is trivially spoofable on-path; PTP less so.
- **Impact: M–H** — OWD is a headline metric (§1); wrong time also undermines TM-003/004 defenses if checks use the local clock naively.
- **Existing control:** agent reports NTP/PTP status/offset/uncertainty; OWD stored as `invalid-clock-sync`/`low-confidence` beyond limits, never as precise (§3); monotonic clock for durations (§4.1).
- **Required control:** BNQO-SEC-094 (clock-integrity control: NTS-authenticated NTP (RFC 8915) to ≥2 diverse sources where available; step-change detection — offset jump > threshold ⇒ status `clock-unsynchronized` + alert §17; expiry/replay decisions prefer monotonic + server time over raw local wall clock), -090 (reflector timestamp checks tolerant-but-bounded; skew beyond bound → reject, not clamp).
- **Residual risk: Low–Medium.**
- **Validation test:** §24 platform *"clock drift"* + lab NTP spoofing (step ±5s/±1h): OWD must degrade to `invalid-clock-sync`, alerts fire, no false cert acceptance/rejection storm.
- **Owner:** Measurement Core Lead.

#### BNQO-TM-012 — Compromised build pipeline
- **Asset:** released agent/reflector/CP binaries and images.
- **STRIDE:** Tampering (TB6)
- **Attack path:** attacker gains CI credentials or a runner, injects code post-review, swaps artifacts in the registry, or exfiltrates/uses signing keys.
- **Likelihood: M** — CI compromise is a top real-world vector; likelihood contained by isolated runners and keyless signing but never zero.
- **Impact: H (Critical when combined with fleet auto-update)** — a backdoored agent is a fleet-wide RCE on every measured server.
- **Existing control:** protected branches, mandatory review, signed commits/verified CI identity (§21).
- **Required control:** BNQO-SEC-080 (branch protection + 2 reviewers + signed commits), -083 (hermetic/reproducible builds: locked toolchain, `cargo build --locked`, isolated ephemeral runners), -084 (Cosign sign + SLSA Build L3 provenance; keys in KMS, never on runners), -085 (deploy-time `cosign verify` + provenance policy gate; staged rollout + canary limits blast radius), -082 (SBOM diff review per release).
- **Residual risk: Medium** — canary + provenance gate bounds, but a fully compromised maintainer+CI is a bounded, not eliminated, risk.
- **Validation test:** RFP §24-security *"malicious update artifact"* — unsigned, wrongly-signed, and provenance-mismatched artifacts must be rejected at deploy/update; tampered-artifact canary test.
- **Owner:** Release / Supply-Chain Lead.

#### BNQO-TM-013 — Malicious dependency
- **Asset:** all Rust/Go/npm dependency graphs (agent, backend, web).
- **STRIDE:** Tampering (TB6)
- **Attack path:** typosquatted or hijacked crate/package; compromised upstream maintainer; dependency confusion against an internal registry name; malicious transitive build-script/`build.rs` code execution at compile time.
- **Likelihood: M** — high base rate in ecosystems; Rust's `build.rs` and npm postinstall give compile-time code exec.
- **Impact: H** — same end state as TM-012 without touching CI.
- **Existing control:** NIST SSDF alignment, dependency scanning required (§21).
- **Required control:** BNQO-SEC-081 (`cargo-audit` + `cargo-deny` advisories/bans/sources in CI, fail on critical/high — ties to §25 *"no open critical/high vulns"*), -083 (`Cargo.lock` committed + `--locked`; vetted sources only; no git dependencies in release builds), -082 (SBOM per release for incident response), -086 (vuln SLA + emergency patch process), frontend: npm lockfile + `--ignore-scripts` in CI.
- **Residual risk: Medium** (zero-day in a dependency is accepted-with-monitoring risk).
- **Validation test:** CI gate demo: introduce a crate with a known advisory → build must fail; SBOM must list the dependency for IR; release blocked by open critical (§25).
- **Owner:** Release / Supply-Chain Lead.

### 5.3 Repudiation

#### BNQO-TM-014 — Audit-trail tampering / action repudiation *(added beyond the §22 list to complete STRIDE coverage)*
- **Asset:** audit log (approvals, job issuance, revocations, pcap access).
- **STRIDE:** Repudiation (C4, C9)
- **Attack path:** privileged user or attacker deletes/rewrites audit rows to erase an approval, a job, or a pcap access; or disputes an action that was logged only weakly.
- **Likelihood: M** — requires DB or admin access; insiders are the realistic actor.
- **Impact: M–H** — approval workflow (§4.3) and incident forensics lose evidentiary value; insider accountability collapses.
- **Existing control:** "full audit" of sensitive ops (§4.3, §11); audit retention, WORM preferred (§14).
- **Required control:** BNQO-SEC-016 (append-only audit table, hash-chained rows signed daily, exported to WORM/versioned object storage; DB roles without UPDATE/DELETE on audit), -053 (pcap access audited to the same chain), -035 (job lifecycle events immutable).
- **Residual risk: Low.**
- **Validation test:** attempt UPDATE/DELETE on audit rows as app DB user → permission denied; tamper one row → chain verification job fails and alerts; §24 *"stolen session"* actions remain attributable.
- **Owner:** Security Engineer.

### 5.4 Information Disclosure

#### BNQO-TM-015 — Credential leakage
- **Asset:** OIDC client secrets, DB passwords, Vault tokens, agent keys, job-signing key, synthetic-probe tokens (§5-005).
- **STRIDE:** Information Disclosure (all boundaries)
- **Attack path:** secrets committed to git; leaked in logs (request logging of headers, panic messages, SQL error echo), stack traces, crash dumps, pcap/diagnostic bundles, URLs/query strings, container image layers, or dashboard UI.
- **Likelihood: M** — the classic multi-surface leak; every surface is individually plausible.
- **Impact: H** — a leaked job-signing key or DB credential cascades into TM-012/TM-026/TM-007.
- **Existing control:** "never log private keys/credentials" (§5-004); no secrets in URL/query (§11); no secrets in source/CLI/logs/images (§20).
- **Required control:** BNQO-SEC-070 (Vault/KMS for CP secrets; dynamic DB credentials ≤24h), -071 (gitleaks/secret scanning in CI + pre-commit; structured-log field denylist with automatic redaction; panic handler strips env; crash dumps exclude memory of key material via `mlock`+zeroize), -020 (secrets only in headers/body), -072 (rotation cadence), -052 (diagnostic bundles scanned/redacted before upload); synthetic-probe accounts are dedicated, least-privilege, scope-limited — never real user credentials (§5-005, enforced by -071 account-scoping review).
- **Residual risk: Low–Medium.**
- **Validation test:** §25: *"no secrets in logs/dumps/UI"* — automated grep of test-run logs, crash dumps, bundle exports and UI responses for known canary secrets; CI secret-scan gate; RFP §24-security *"shell/SQL injection"* error paths must not echo internals (BNQO-SEC-019).
- **Owner:** Security Engineer.

#### BNQO-TM-016 — API enumeration
- **Asset:** tenant/link/agent inventory metadata; user existence; endpoint map.
- **STRIDE:** Information Disclosure (TB4)
- **Attack path:** enumerate sequential `/v1/agents/{id}`, `/v1/links/{id}` across tenants (IDOR/BOLA); user-enumeration via distinguishable auth errors/timing; unauthenticated discovery of API surface and versions; verbose error messages revealing internals.
- **Likelihood: H** — requires only API reachability; BOLA is OWASP API #1 for a reason.
- **Impact: M** — reconnaissance for TM-026/TM-005; leakage of network topology intelligence (which links exist is itself sensitive here).
- **Existing control:** per-object authorization + RBAC (§11); OpenAPI-documented surface (§23).
- **Required control:** BNQO-SEC-011 (object-level check on every handler: tenant scope from token, never from client-supplied ID alone), -012 (strict schema validation), -019 (uniform secure errors, no stack traces, no user-existence oracle), -014 (rate limits making bulk enumeration noisy), -016 (audit of repeated 403/404 patterns → alert), non-sequential resource IDs (UUIDv7/ULID, not serial integers).
- **Residual risk: Low.**
- **Validation test:** RFP §24-security *"cross-tenant"* and *"shell/SQL injection"* — fuzzed ID sweeps as tenant A against tenant B objects must yield uniform 404; timing-difference test on login; §25-related: no stack traces in any 4xx/5xx.
- **Owner:** Control Plane Lead.

#### BNQO-TM-017 — pcap data leakage
- **Asset:** captured packets (may contain production payloads, credentials in plaintext protocols, customer traffic).
- **STRIDE:** Information Disclosure (C1, C9)
- **Attack path:** pcap enabled too broadly (no filter/full snaplen) captures production secrets; captures exfiltrated via object-storage misconfiguration or over-broad dashboard access; retained beyond policy; displayed unredacted in UI.
- **Likelihood: M** — gated by RBAC+approval by design, but misconfiguration and over-capture are realistic operator errors.
- **Impact: H** — pcap of a TLS-terminating or tunnel interface can contain exactly the credentials BNQO itself protects.
- **Existing control:** default OFF; RBAC+approval; filter/duration/volume/snaplen limits; dedicated encryption key; audit; auto-delete; redaction (§18).
- **Required control:** BNQO-SEC-050 (default-off compile/config gate + step-up auth + approval), -051 (mandatory BPF filter, snaplen ≤128 default, duration ≤5 min, volume cap, probe-ports-only default), -052 (per-capture XChaCha20-Poly1305 key escrowed in Vault; restricted bucket, no public ACLs, auto-delete ≤24h), -053 (no payload rendering in UI; short-lived pre-signed URLs; all access in audit chain).
- **Residual risk: Low.**
- **Validation test:** §25: *"no secrets in logs/dumps/UI"*; enable pcap without approval → denied; attempt unauthenticated object fetch → denied; verify auto-deletion job; canary-secret capture test confirms redaction/encryption at rest.
- **Owner:** Security Engineer.

### 5.5 Denial of Service

#### BNQO-TM-018 — UDP reflection / amplification via reflector
- **Asset:** reflector egress bandwidth; BNQO's reputation (abuse of our servers to attack third parties).
- **STRIDE:** Denial of Service (TB1)
- **Attack path:** attacker spoofs victim source IPs and floods the public reflector port, hoping responses amplify toward the victim; or floods unauthenticated garbage to exhaust reflector CPU/sessions.
- **Likelihood: H** — the reflector is internet-facing by design; spoofed UDP floods are commodity.
- **Impact: H** — becoming an amplifier is an existential design failure for a "secure reflector"; host-level exhaustion also blinds measurement.
- **Existing control:** §4.2 in full: registered peers only, no reply to unauthenticated packets, response never larger than request, rate + concurrent-session limits, bind to configured IP only, auto-expiring sessions.
- **Required control:** BNQO-SEC-090 (silent drop — zero bytes emitted for unauthenticated/invalid packets, including no ICMP administratively-prohibited games from the reflector path), -091 (amplification factor ≤1.0 enforced structurally: response length = min(request, cap); per-peer-token-bucket + global cap; session cap with LRU eviction), -065 (host nftables egress allowlist), -016 (invalid/replay/auth-failure counters exported to security view + alert on flood signature, §16/§17 *"reflector auth attack"*).
- **Residual risk: Low.**
- **Validation test:** RFP §24-security *"invalid HMAC"* + amplification harness: spoofed-source flood → measure emitted bytes = 0 for unauthenticated, ≤ received bytes for authenticated peers; §25: *"reflector silent to unknown packets."*
- **Owner:** Measurement Core Lead.

#### BNQO-TM-019 — Throughput-test DoS abuse
- **Asset:** measured-link capacity; production traffic sharing the link.
- **STRIDE:** Denial of Service (TB5, TB1)
- **Attack path:** a stolen operator session or a CP compromise (TM-029) issues RUN_THROUGHPUT_TEST / iperf3-adapter jobs at max rate repeatedly to saturate the very links being monitored; or a tampered config raises `max_bandwidth`.
- **Likelihood: M** — needs control-plane authority, which is exactly why the agent must not blindly trust it.
- **Impact: H** — the monitoring system DoSing production is both an availability and reputational catastrophe.
- **Existing control:** throughput profiles bounded: scheduled off-peak windows, on-demand requires RBAC+approval+max rate+duration; iperf3 only as audited, ephemeral, firewall-restricted adapter with auto-stop if production harmed (§5-008, §6-D).
- **Required control:** BNQO-SEC-033 (agent-enforced hard caps: token-bucket shaper, `max_duration`/`max_bandwidth` from *signed config* — CP cannot raise them via job alone; approval_id validated against audit log), -034 (target restricted to assigned peer), -033 also covers production-harm auto-stop (agent aborts the test on real-traffic error/loss correlation, §5-008), -022 (approval + step-up auth for on-demand capacity tests), -035 (audit + alert on repeated capacity jobs).
- **Residual risk: Low–Medium.**
- **Validation test:** §25: *"throughput limits enforced"* — request 10× cap via API (rejected at API), via forged job (rejected at agent), via tampered config (signature failure); measure actual shaper throughput vs cap; auto-stop triggers under injected production errors (§24 *"excessive jobs"*).
- **Owner:** Control Plane Lead.

#### BNQO-TM-020 — Disk exhaustion
- **Asset:** agent host filesystem; WAL survivability; host stability.
- **STRIDE:** Denial of Service (C1)
- **Attack path:** CP/collector outage longer than buffer → WAL grows unbounded; verbose logging or pcap/diagnostic bundles fill disk; attacker induces error storms to inflate logs; agent fills OS filesystem and takes down co-located services.
- **Likelihood: M** — outages beyond 72h are rare but the failure mode is deterministic if quotas are wrong.
- **Impact: M–H** — on a shared production server, a full disk is a host-level incident (§7 host faults must never be *caused by* the monitor).
- **Existing control:** §15: storage quota, backpressure, oldest-eviction, priority queue for incidents, "agent must never fill the OS filesystem"; ≥72h configurable buffer.
- **Required control:** BNQO-SEC-042 (hard quota enforced by agent *and* systemd: `LimitNOFILE`, WAL dir on bounded allocation; watermark alerts at 70/85/95% — §17 *"local queue near capacity"*),-044 (zstd compression of batches; eviction prefers oldest non-incident data), -040 (quota accounting crash-safe, reconciled on startup).
- **Residual risk: Low.**
- **Validation test:** §24 platform *"72h offline agent"* + forced 2×-buffer outage: verify quota holds, eviction order correct (incident-priority retained), OS FS never > threshold, recovery uploads in order (§25: *"≥72h buffer"*, *"no logical duplicates after reconnect"*).
- **Owner:** Measurement Core Lead.

#### BNQO-TM-021 — Queue flooding (JetStream / collector)
- **Asset:** telemetry pipeline availability; legitimate measurements in flight.
- **STRIDE:** Denial of Service (TB2, TB3)
- **Attack path:** compromised or malfunctioning agent floods batches; replay floods (TM-003); misconfigured fleet (1,000 agents × verbose profiles) exceeds queue capacity; JetStream growth exhausts CP storage.
- **Likelihood: M** — fleet scale (§1: 1,000 agents) makes aggregate misconfiguration realistic; a compromised agent is the adversarial case.
- **Impact: H** — backpressure into collectors ⇒ global telemetry loss, exactly during incidents when data matters most.
- **Existing control:** durable queue in the architecture (§4.4); agent rate limits and jittered intervals (§4.1, §6-A).
- **Required control:** BNQO-SEC-014 (per-identity rate+concurrency limits at collector with 429/backoff), -043 (dedup blunts replay floods), -095 (batch size caps), JetStream limits policy (per-stream max msgs/bytes/age, discard-old, work-queue retention where applicable) + per-agent stream/subject partitioning so one agent cannot fill a shared stream, -016 (flood alerts §17).
- **Residual risk: Low–Medium.**
- **Validation test:** §24-security *"rate-limit bypass"* + load test: single identity at 50× quota → throttled, others unaffected; queue at 90% → alerts fire, oldest-eviction per policy, no collector OOM.
- **Owner:** Pipeline Lead.

#### BNQO-TM-022 — Oversized payload
- **Asset:** collector/processor memory and CPU; gRPC/HTTP parsers.
- **STRIDE:** Denial of Service (TB2, TB4)
- **Attack path:** multi-GB measurement batch or diagnostic artifact; decompression bomb (tiny zstd payload expanding hugely); deeply nested/oversized JSON against the management API; oversized probe packets to reflector.
- **Likelihood: H** — unauthenticated-adjacent (pre-auth parsing) and trivial to attempt.
- **Impact: M** — per-connection OOM/crash; mitigated structurally so single-shot impact stays bounded.
- **Existing control:** request size limits (§11); strict input validation (§31).
- **Required control:** BNQO-SEC-013 (hard caps: REST body ≤1 MiB, batch ≤8 MiB, artifact ≤64 MiB chunked, enforced at gateway *before* parse), -095 (decompression ratio cap + absolute uncompressed cap; streaming decode, never inflate-then-check), -091 (reflector drops packets > configured MTU cap), fuzz targets on all decoders (BNQO-SEC-081).
- **Residual risk: Low.**
- **Validation test:** RFP §24-security *"oversized telemetry"* — send 10× cap batches, 1000:1 zstd bombs, nested-JSON bombs: expect immediate 4xx/drop, bounded memory, audit event.
- **Owner:** Pipeline Lead.

### 5.6 Elevation of Privilege

#### BNQO-TM-023 — Shell / arbitrary command execution via job channel
- **Asset:** measured servers (agent/reflector hosts).
- **STRIDE:** Elevation of Privilege (TB5)
- **Attack path:** attacker with CP access (or a job-schema bug) smuggles a command: free-form string executed via shell, executable path, arbitrary URL fetched-and-run, SQL in job parameters reaching a shell, or prototype-style injection into a typed field concatenated into a command line.
- **Likelihood: M** — the control channel exists and is powerful; severity of the bug class is why §12 forbids free-form anything. Realized only if implementation deviates from the typed-job design.
- **Impact: H (Critical)** — RCE on every measured server; the single worst agent-side outcome.
- **Existing control:** §12: typed jobs only from a fixed enum; free-form command/shell/URL/path FORBIDDEN; "NO arbitrary shell" (§4.1, §4.3, §31).
- **Required control:** BNQO-SEC-030 (jobs decode into a closed Rust enum — there is no `exec` code path to reach; parameters are typed structs with per-field validation; nothing is ever passed to a shell), -031 (ed25519 job signature), -032 (expiry/duplicate/policy checks), -034 (target allowlist), -081 (fuzz job/config decoders; SAST rule: no `std::process::Command` in agent binary — enforced in CI).
- **Residual risk: Low** — structurally eliminated, continuously verified.
- **Validation test:** RFP §24-security *"shell/SQL injection"* — craft jobs with `; rm -rf`, backticks, `$(...)`, SQL fragments in every string field: all rejected at API schema or agent decode; §25: *"unsigned job not executed"*, *"no arbitrary shell"*; CI check proves no `Command` symbol in agent build.
- **Owner:** Measurement Core Lead + Control Plane Lead.

#### BNQO-TM-024 — SSRF via arbitrary target
- **Asset:** internal networks reachable from agents/CP; third-party hosts.
- **STRIDE:** Elevation of Privilege (TB5, TB4)
- **Attack path:** job or link definition pointing at 169.254.169.254 (cloud metadata), RFC1918 management hosts, loopback services, or any internet host — turning the agent into an attack proxy or a scanner; DNS-rebinding to bypass hostname allowlists.
- **Likelihood: M** — requires job/config authority or an authorization gap in link creation (OWASP API7).
- **Impact: H** — metadata-credential theft, internal pivot, third-party abuse attribution to BNQO.
- **Existing control:** §10 SR-IDENTITY-003: agent may only probe assigned links / defined peers; "no arbitrary internet targets."
- **Required control:** BNQO-SEC-034 (agent-side allowlist: job `peer_id` must match signed-config assignments; IP-literal targets resolved once and pinned, DNS-rebinding guard, block link-local/loopback/metadata ranges unless explicitly scoped), -011 (link-creation authorization: only admin role, tenant-scoped, audited), -030 (no free-form URL field exists in job schema).
- **Residual risk: Low.**
- **Validation test:** RFP §24-security *"arbitrary target/SSRF"* — jobs/links targeting metadata IP, 127.0.0.1, 10.x, and an unassigned public host: rejected at API (unauthorized) and at agent (not in signed config), with audit events.
- **Owner:** Control Plane Lead.

#### BNQO-TM-025 — Cross-tenant access
- **Asset:** all tenant-scoped data (links, measurements, incidents, agents), per §13 tenant_id on every record.
- **STRIDE:** Elevation of Privilege (TB4, TB3)
- **Attack path:** BOLA/IDOR on REST resources; missing tenant filter in ClickHouse/PostgreSQL queries; shared JetStream subjects readable across tenants; dashboard rendering another tenant's cached data.
- **Likelihood: H** — multi-tenancy (§4.3) + per-object auth bugs are the highest-base-rate API flaw class.
- **Impact: H** — measurement data reveals infrastructure topology, peak hours, and vulnerabilities of other tenants.
- **Existing control:** RBAC + per-object authorization (§11); tenant model (§4.3).
- **Required control:** BNQO-SEC-011 (mandatory tenant-scope middleware: every query carries `tenant_id` predicate derived from token claims — enforced in the data-access layer, not per-handler memory), -016 (cross-tenant attempt auditing + alerting), row-level security in PostgreSQL as defense-in-depth, -015 (idempotency keys scoped per tenant).
- **Residual risk: Low–Medium** (structural middleware + tests keep it bounded).
- **Validation test:** RFP §24-security *"cross-tenant"* — automated test suite: every endpoint, tenant A token vs tenant B object IDs (including UUIDs swapped into URLs and idempotency keys): uniform 404/403; negative tests in CI for every new endpoint (PR gate).
- **Owner:** Control Plane Lead.

#### BNQO-TM-026 — Host privilege escalation (agent/reflector → root)
- **Asset:** measured server root; kernel; co-located production services.
- **STRIDE:** Elevation of Privilege (C1, C2)
- **Attack path:** memory-safety bug in parser (mitigated by Rust), setuid/capability abuse of CAP_NET_RAW, writable-path escape from the sandbox, symlink attack on WAL/config paths, kernel exploit via unfiltered syscall surface.
- **Likelihood: L–M** — Rust removes the dominant bug class (§4.1); residual is logic/sandbox-escape, requiring local code exec first.
- **Impact: H** — root on a production-adjacent server.
- **Existing control:** §19 in full: dedicated user, minimal caps, NoNewPrivileges, PrivateTmp, ProtectSystem/Home/Kernel*, RestrictNamespaces/AddressFamilies, seccomp, AppArmor/SELinux, resource limits, no runtime binary downloads.
- **Required control:** BNQO-SEC-060 (dedicated users, agent caps = CAP_NET_RAW only, reflector caps = none), -061 (systemd sandbox block — see SECURITY_CONTROLS.md §3), -062 (seccomp-bpf allowlist profile; AppArmor), -063 (resource limits), -064 (read-only FS, WAL sole writable path, `O_NOFOLLOW` + fd-relative opens on WAL files), -066 (single static binary, no plugins).
- **Residual risk: Low.**
- **Validation test:** run agent under `strace`/seccomp audit — syscalls outside profile kill the process; attempt writes outside WAL dir → EPERM; `systemd-analyze security` score ≤ 2.0 ("OK"); capability bounding set audit; §25 perf targets hold under sandbox (idle CPU <1%, RAM <150MB).
- **Owner:** Platform / SRE Lead.

#### BNQO-TM-027 — Compromised agent
- **Asset:** everything reachable from the agent host; telemetry truth; peers.
- **STRIDE:** Elevation of Privilege (C1 → TB1/TB2)
- **Attack path:** attacker roots the agent host (unrelated to BNQO) or exploits the agent: now holds a valid identity, can fabricate measurements (TM-006b), attack the assigned peer within policy, exfiltrate WAL, and probe internal network if egress is open.
- **Likelihood: M** — agent hosts are internet-exposed measurement boxes; assume breach.
- **Impact: M–H** — bounded by design: identity is scoped (can only be *that* agent), targets are allowlisted, keys are short-lived; but measurement truth for that agent is lost and the peer reflector faces an authenticated attacker.
- **Existing control:** least privilege + sandboxing (§4.1, §19); authorization scoping (§10 SR-IDENTITY-003); revocation (§4.3).
- **Required control:** BNQO-SEC-065 (nftables egress: CP/collector endpoints + assigned peer IPs/ports only — no lateral movement), -006 (server-side per-agent rate and target enforcement: even a valid identity cannot exceed policy), -090 (reflector per-peer limits bind an authenticated hostile agent), -005 (rapid revocation + re-enrollment runbook), -016 (behavioral anomaly detection: reporting pattern shifts, version/clock anomalies → security view).
- **Residual risk: Medium** — accepted: a rooted host cannot be trusted to report truthfully; we bound blast radius and detect anomalies. Documented in ops runbook.
- **Validation test:** red-team scenario: from a rooted agent VM, attempt (a) probing non-assigned targets (blocked by egress + reflector), (b) 100× reporting rate (throttled server-side), (c) forged peer traffic (reflector caps), (d) lateral SSH (no credentials present); verify detection events.
- **Owner:** Platform / SRE Lead + Security Engineer.

#### BNQO-TM-028 — Compromised control plane
- **Asset:** fleet-wide job/config authority; identity issuance; all stored data.
- **STRIDE:** Elevation of Privilege (C4 → TB5)
- **Attack path:** attacker gains CP admin (phished operator, CP software RCE, DB credential theft): issues malicious jobs fleet-wide, pushes hostile signed configs, mints rogue agent identities, reads all tenants' data.
- **Likelihood: L–M** — highest-value target, best-defended; but phishing of an operator is perennially plausible.
- **Impact: H (Critical)** — the fleet-authority scenario; mitigations cap what "admin" can do unilaterally.
- **Existing control:** NO general remote shell exists to abuse (§4.3); typed jobs only (§12); approval workflow for heavy tests (§4.3); full audit (§4.3).
- **Required control:** BNQO-SEC-033 (agent-side hard caps survive CP compromise: signed-config ceilings cannot be raised by job), -031 (job-signing key in KMS/HSM — an admin cannot export it; signing operations are audited), -022 (step-up MFA + dual-control approval for fleet-wide/dangerous ops), -011 (least-privilege RBAC: operator ≠ approver ≠ admin), -016 (immutable audit to WORM — CP compromise cannot erase its own tracks), -030 (even a fully malicious CP cannot emit a shell command: the type does not exist).
- **Residual risk: Medium** — residual accepted and disclosed: data confidentiality of the CP DB and availability of the fleet remain CP-trust-dependent; agent-side caps prevent the worst (RCE/DoS) outcomes.
- **Validation test:** game-day: with full CP admin credentials, attempt (a) shell-exec job (impossible — schema), (b) throughput above config cap (agent-clamped + alert), (c) unapproved fleet-wide capacity test (blocked by dual control), (d) audit deletion (WORM denies); verify alerts on each.
- **Owner:** Security Engineer + Control Plane Lead.

#### BNQO-TM-029 — Insider threat
- **Asset:** all planes; especially audit integrity, signing keys, tenant data.
- **STRIDE:** Elevation of Privilege (cross-cutting; C4/C9/C11)
- **Attack path:** malicious or negligent operator/DBA/developer: exports tenant data, approves their own dangerous job, tampers with audit, exfiltrates keys, ships a subtle backdoor through review.
- **Likelihood: M** — small team, broad access in early phases; the most likely "advanced" adversary.
- **Impact: H.**
- **Existing control:** mandatory review (§21); approval workflow (§4.3); full audit (§4.3).
- **Required control:** BNQO-SEC-011 (least privilege + separation of duties: no self-approval; deployer ≠ approver ≠ key-holder), -016 (WORM audit + daily verification job + access-to-audit is itself audited), -070 (keys in KMS/HSM — no human sees plaintext), -080 (2-person review + signed commits), -022 (step-up auth), periodic access recertification + joiner/mover/leaver process; pcap access (BNQO-SEC-050..053) as the canonical dual-control flow.
- **Residual risk: Medium** — insider risk is reducible, not eliminable; detection + attribution is the strategy.
- **Validation test:** quarterly access review evidence; self-approval attempt blocked; key-export attempt impossible/denied and audited; audit-verification drill.
- **Owner:** Compliance Officer + Security Engineer.

---

## 6. Risk summary table

| ID | Threat | STRIDE | TB | L | I | Inherent risk | Residual risk |
|----|--------|--------|----|---|---|---------------|----------------|
| TM-001 | Agent spoofing | S | TB2/TB5 | H | H | Critical | Low |
| TM-002 | Cert/key theft | S | TB2 | M | H | High | Low–Med |
| TM-003 | Telemetry replay | S | TB1/TB2 | H | H | Critical | Low |
| TM-004 | Expired/revoked cert acceptance | S | TB2/TB5 | M | H | High | Low |
| TM-005 | Brute force | S | TB4 | M | M–H | High | Low |
| TM-006 | Metric poisoning | T | TB1/TB2 | H | H | Critical | **Medium** |
| TM-007 | Result tampering | T | C1/TB2/C9 | M | H | High | Low |
| TM-008 | Route forgery | T | TB1 | H | M | High | **Medium** |
| TM-009 | Config tampering | T | TB5/C1 | M | H | High | Low |
| TM-010 | Downgrade | T | TB1/TB2/TB5 | M | H | High | Low |
| TM-011 | Time manipulation | T | C1/TB1 | M | M–H | High | Low–Med |
| TM-012 | Compromised build pipeline | T | TB6 | M | H | High | **Medium** |
| TM-013 | Malicious dependency | T | TB6 | M | H | High | **Medium** |
| TM-014 | Audit tampering / repudiation | R | C4/C9 | M | M–H | High | Low |
| TM-015 | Credential leakage | I | all | M | H | High | Low–Med |
| TM-016 | API enumeration | I | TB4 | H | M | High | Low |
| TM-017 | pcap data leakage | I | C1/C9 | M | H | High | Low |
| TM-018 | UDP reflection/amplification | D | TB1 | H | H | Critical | Low |
| TM-019 | Throughput-test DoS abuse | D | TB5/TB1 | M | H | High | Low–Med |
| TM-020 | Disk exhaustion | D | C1 | M | M–H | High | Low |
| TM-021 | Queue flooding | D | TB2/TB3 | M | H | High | Low–Med |
| TM-022 | Oversized payload | D | TB2/TB4 | H | M | High | Low |
| TM-023 | Shell exec via job | E | TB5 | M | H | High (Critical impact) | Low |
| TM-024 | SSRF via arbitrary target | E | TB5/TB4 | M | H | High | Low |
| TM-025 | Cross-tenant access | E | TB4/TB3 | H | H | Critical | Low–Med |
| TM-026 | Host privilege escalation | E | C1/C2 | L–M | H | High | Low |
| TM-027 | Compromised agent | E | C1→TB1/TB2 | M | M–H | High | **Medium** |
| TM-028 | Compromised control plane | E | C4→TB5 | L–M | H | High (Critical impact) | **Medium** |
| TM-029 | Insider threat | E | cross | M | H | High | **Medium** |

## 7. Top-10 risk ranking (by inherent risk, tie-broken by exploitability in the assumed environment)

| Rank | ID | Threat | Why it ranks here |
|------|----|--------|-------------------|
| 1 | TM-006 | Metric poisoning | H×H, intrinsic to measuring through a hostile network; corrupts the product's core value; only partially mitigable (→ residual Medium). |
| 2 | TM-003 | Telemetry replay | H×H, free for the on-path adversary, directly defeats "never show healthy when data is absent" (§8). |
| 3 | TM-018 | UDP reflection/amplification | H×H, internet-facing reflector is a standing amplification candidate; structural ≤1.0 cap required from day one. |
| 4 | TM-001 | Agent spoofing | H×H, gateway threat to every integrity property; cheap to attempt. |
| 5 | TM-025 | Cross-tenant access | H×H, highest-base-rate API flaw class (OWASP API1) in a multi-tenant design. |
| 6 | TM-023 | Shell exec via job | Impact is Critical (fleet RCE); likelihood held at M only by the typed-job structural design — must be continuously proven (CI check: no exec path exists). |
| 7 | TM-028 | Compromised control plane | Critical-impact scenario; agent-side caps + dual control are what keep residual at Medium. |
| 8 | TM-012 | Compromised build pipeline | One success = fleet-wide backdoor; provenance + canary bound it. |
| 9 | TM-029 | Insider threat | Small-team reality; detection/attribution strategy, never fully preventable. |
| 10 | TM-008 | Route forgery | H likelihood in a DPI/active-interference environment; impact capped at M by corroboration rules, residual Medium. |

## 8. Validation program linkage

Every register entry's validation test is instantiated in `tests/security/` and the netem matrix (`tests/netem/`) per the repo layout (§28). The RFP §24 security scenario set (*replay, forged cert, invalid HMAC, oversized telemetry, excessive jobs, arbitrary target/SSRF, shell/SQL injection, cross-tenant, stolen session, rate-limit bypass, malicious update artifact*) is covered end-to-end by TM-001/003/018/022/019/024/023/025/002/005/012 respectively, and the RFP §25 security acceptance criteria are mapped to concrete acceptance tests in `SECURITY_CONTROLS.md` §5. Phase 4 ends with a third-party pentest scoped to this register (§30); Phase 5 re-runs the full security scenario matrix as a release gate.
