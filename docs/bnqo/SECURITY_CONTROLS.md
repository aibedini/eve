# BNQO — Security Controls Matrix

Document status: Phase 0 design artifact (normative for Phases 1–6)
Scope baseline: RFP_DIGEST.md — controls cover **§10 (identity), §11 (API security), §12 (job security), §15 (WAL integrity/encryption), §18 (pcap), §19 (runtime hardening), §20 (secrets), §21 (supply chain)**, plus the measurement-plane security requirements of **§4.2/§5-002** needed by the threat model.
Companion document: `THREAT_MODEL.md` — `BNQO-TM-xxx` references therein resolve to controls defined here.
Phases per RFP §30: 0 design · 1 measurement core · 2 control plane · 3 observability · 4 security hardening · 5 validation · 6 production.

---

## 1. Control catalogue

Verification methods: **T** = automated test (unit/integration/e2e/netem/security suite), **A** = audit/inspection (config review, log evidence, access review), **S** = automated scan (SAST/dependency/secret/container/IaC), **P** = pentest/red-team.

### 1.1 Agent identity & bootstrap (§10, Zero Trust / NIST SP 800-207)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-001 | SPIFFE workload identity, no static API keys (§10 SR-IDENTITY-001) | SPIRE server + SPIRE agent per host; X.509-SVID with SPIFFE ID `spiffe://bnqo/<tenant>/agent/<agent_id>`; SVID TTL ≤ 1 h, rotated at 50 % TTL. Interim (Phase 2): org private CA (step-ca) with per-agent certs, same TTL/rotation semantics; SPIRE replaces it in Phase 4 without protocol change. | T: mTLS connect with/without valid SVID; A: SPIRE registration entries | 2 (interim CA), 4 (SPIRE) |
| BNQO-SEC-002 | Node + workload attestation (§10 SR-IDENTITY-001) | SPIRE node attestor: `tpm` where TPM 2.0 present, else `join_token` (single-use); workload attestor: `unix` (uid/gid) + systemd unit path; registration entry binds agent_id to node selectors. | T: attestation from wrong uid/unit rejected; A: attestation logs | 4 |
| BNQO-SEC-003 | One-time enrollment (§10 SR-IDENTITY-002) | Enrollment token: 128-bit random, TTL ≤ 5 min, single-use, bound to expected node attributes (hostname, machine-id fingerprint, IP range); consumed token invalidated atomically; every enrollment (success/failure) written to audit chain. | T: token reuse / expiry / wrong-fingerprint rejection (§24 *stolen session* class) | 2 |
| BNQO-SEC-004 | Per-agent key custody (§10 SR-IDENTITY-001) | Keypair (ECDSA P-256, ed25519 for config/job verify) generated on-agent at enrollment; private key file 0400 owned by `bnqo-agent`, never leaves host, never logged; `zeroize` on rotation. | T: key exfil path absent in code review + SAST; A: file perms | 2 |
| BNQO-SEC-005 | Rotation & revocation (§10 SR-IDENTITY-001, §4.3, §17) | Agent-initiated renewal at ≤50 % TTL with jitter; `ROTATE_IDENTITY` job as CP-forced path; revocation via SPIRE revocation list + CRL distributed to collector/CP; revocation effective ≤ 60 s; cert-expiry alerts at 7 d / 1 d (§17). | T: §24 *cert expiration/revocation*; §25 *revoked cert rejected within window* | 2 |
| BNQO-SEC-006 | Agent authorization scoping (§10 SR-IDENTITY-003) | Server-side policy engine: SVID SPIFFE ID must equal `agent_id` claim of every RPC; agent may fetch only its own config/jobs, upload only its own telemetry, probe only peers in its signed config. Policy-as-code (OPA Rego or equivalent embedded evaluator), unit-tested. | T: cross-agent RPC attempts → 403/UNAUTHENTICATED + audit | 2 |
| BNQO-SEC-007 | mTLS transport floor (§10, §11) | rustls, TLS 1.3 only (no 1.2 fallback), suites `TLS_AES_256_GCM_SHA384` / `TLS_CHACHA20_POLY1305_SHA256`; ALPN pinned (`bnqo/1`); mutual auth mandatory on agent gRPC, collector ingest, CP internal RPC; no renegotiation; cert validity checked against CP-synced time, fail-closed. | T: TLS 1.2 handshake fails; wrong-ALPN fails; S: TLS config lint | 2 |

### 1.2 API security (§11, OWASP API Security Top 10 2023)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-010 | User authentication + MFA (§11) | OIDC authorization-code flow + PKCE against org IdP; WebAuthn (preferred) or TOTP MFA enforced by policy for operator/approver/admin roles; no local password store. | T: login flows; A: IdP MFA policy export | 3 |
| BNQO-SEC-011 | RBAC + per-object authorization (§11, §4.3) | Roles: viewer / operator / approver / admin / auditor; middleware derives `tenant_id` (+project/environment) from token claims and injects it as a mandatory predicate in the data-access layer — handlers cannot query without it; PostgreSQL row-level security as defense-in-depth; non-sequential resource IDs (UUIDv7). | T: §24 *cross-tenant* matrix on every endpoint (CI gate); A: role matrix review | 3 |
| BNQO-SEC-012 | Strict schema validation (§11, §31) | OpenAPI 3.1 request validation middleware (unknown fields rejected); protobuf with strict field presence/range checks; no `Any`/free-form maps crossing trust boundaries. | T: fuzzed invalid payloads → 400; S: schema lint | 2 |
| BNQO-SEC-013 | Request size limits (§11) | Gateway-enforced pre-parse caps: REST body ≤ 1 MiB; measurement batch ≤ 8 MiB; diagnostic artifact ≤ 64 MiB streamed in chunks; gRPC `max_recv_message_size` set accordingly. | T: §24 *oversized telemetry* | 2 |
| BNQO-SEC-014 | Rate + concurrency limits (§11) | Token bucket per identity + per IP at gateway (e.g. 100 req/s burst 200 REST; per-agent batch rate per profile); concurrency caps per expensive endpoint (`/v1/diagnostics`); 429 + `Retry-After`; lockout/backoff on auth endpoints. | T: §24 *rate-limit bypass* | 3 |
| BNQO-SEC-015 | Replay protection & idempotency (§11) | Nonce + timestamp (±30 s skew) + per-stream monotonic sequence on agent RPCs; idempotency keys (scoped per tenant, 24 h TTL) required on mutating REST; server-side dedup store (Redis/PostgreSQL unique constraints). | T: §24 *replay*, *duplicate/out-of-order batches* | 2 |
| BNQO-SEC-016 | Immutable audit logging (§11, §4.3, §14) | Append-only `audit_events` table (hash-chained rows: `row_hash = SHA-256(prev_hash ‖ canonical_row)`), daily anchor hash signed by CP signing key and exported to WORM/versioned object storage; app DB role has INSERT/SELECT only; all sensitive ops logged (auth events, job lifecycle, config, pcap, approvals, admin actions). | T: chain-verification job detects tampering; A: WORM export evidence | 2 |
| BNQO-SEC-017 | Pagination & query limits (§11) | Cursor pagination mandatory (max page 500); query complexity/timeout caps (statement_timeout 5 s; ClickHouse `max_execution_time`); no unbounded `SELECT *` endpoints. | T: unbounded-query attempts; load test | 3 |
| BNQO-SEC-018 | Timeouts & circuit breakers (§11) | All egress calls (IdP, DB, queue, object storage) with explicit connect/read timeouts; circuit breaker (failure-ratio based) + bounded retries with jitter. | T: chaos test with blackholed dependencies | 3 |
| BNQO-SEC-019 | Secure error handling (§11) | Uniform error envelope `{error_id, code, message}`; stack traces/internals only in server logs keyed by `error_id`; no user-existence or object-existence oracles (uniform 404 for unauthorized cross-tenant IDs). | T: §24 *shell/SQL injection* error paths; timing oracle test | 2 |
| BNQO-SEC-020 | No secrets in URLs (§11) | Secrets/tokens only in `Authorization` header or request body; URL/query logging middleware strips nothing because nothing sensitive is allowed; OpenAPI forbids `in: query` for credentials (linted). | S: OpenAPI lint rule; T: log inspection | 2 |
| BNQO-SEC-021 | Web session hardening (§11, §27) | Restricted CORS allowlist (exact origins, no `*` with credentials); strict CSP (`default-src 'self'`, no inline scripts, nonces); CSRF tokens on cookie flows; cookies `Secure; HttpOnly; SameSite=Strict`; session idle 8 h / absolute 24 h; OIDC tokens in memory only, never localStorage (§27). | T: browser e2e; S: header scan (ZAP baseline) | 3 |
| BNQO-SEC-022 | Step-up re-auth for dangerous ops (§11) | WebAuthn assertion required within ≤ 10 min for: agent revoke, pcap enable, on-demand throughput test, fleet-wide config push, role grants; combined with dual-control approval where flagged (see -033, -050). | T: op without recent assertion → 403 | 4 |

### 1.3 Remote job & config security (§12)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-030 | Typed jobs only (§12, §4.1, §4.3) | Closed enum of 11 job types (RUN_ICMP_PROBE, RUN_UDP_PROBE, RUN_TCP_PROBE, RUN_TLS_PROBE, RUN_MTR, RUN_ROUTE_TRACE, RUN_MTU_DISCOVERY, RUN_THROUGHPUT_TEST, COLLECT_HOST_SNAPSHOT, ROTATE_IDENTITY, UPDATE_SIGNED_CONFIG) decoded into Rust `enum` + typed parameter structs; no free-form command/URL/path field exists; agent binary contains **no exec/spawn code path** (CI-enforced). | T: §24 *shell/SQL injection*; S: CI check `std::process::Command` absent from agent/reflector builds | 2 |
| BNQO-SEC-031 | Job & config signing (§12, §4.1) | ed25519 signature over canonical protobuf encoding of all job/config fields; CP signing key in KMS/HSM (Vault transit or cloud KMS) — signing is an audited KMS operation, key non-exportable; agent pins the CP verify pubkey via its bootstrap config. | T: §24 *invalid config signature*; §25 *unsigned job not executed* | 2 |
| BNQO-SEC-032 | Job/config freshness & anti-rollback (§12) | `not_before`/`expires_at` enforced (±60 s skew against clock-quality-checked time); `job_id` dedup via on-agent LRU (last 10k) + server-side; monotonic `config_version` — lower versions rejected; minimum accepted `protocol_version` signed into config (anti-downgrade). | T: expired/duplicate/old-version jobs & configs rejected + audited | 2 |
| BNQO-SEC-033 | Job policy bounds & approvals (§12, §5-008, §6-D) | Agent-enforced token-bucket shaper caps throughput to `max_bandwidth` from *signed config* (jobs can only narrow, never widen); `max_duration` hard stop; RUN_THROUGHPUT_TEST on-demand requires `approval_id` matching an audited approval by an approver-role user (dual control for fleet-wide); auto-stop on production-harm signals (§5-008: real-traffic error/loss correlation). | T: §25 *throughput limits enforced*; §24 *excessive jobs* | 2 |
| BNQO-SEC-034 | Target allowlist / anti-SSRF (§12, §10 SR-IDENTITY-003) | Agent validates job `peer_id` against signed-config assignments; link targets resolved once at creation and pinned to IPs; DNS-rebinding guard (re-resolution must match pinned set); loopback/link-local/RFC1918/cloud-metadata (169.254.169.254) targets rejected unless explicitly scoped by admin. | T: §24 *arbitrary target/SSRF* | 2 |
| BNQO-SEC-035 | Job lifecycle audit (§12, §16) | Immutable events: created/approved/sent/received/ack/started/completed/rejected (with reason codes: `expired`, `bad-signature`, `duplicate`, `out-of-policy`, `unknown-target`); surfaced in security view (§16) and incident timeline. | T: event presence per scenario; A: audit chain | 2 |

### 1.4 Local WAL integrity & encryption (§15)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-040 | Crash-safe WAL structure (§15) | Append-only segment files; per-record CRC32C + chained SHA-256 (record hash includes previous record hash) + monotonic sequence; fsync per segment roll and per N records (configurable); startup reconciliation truncates torn tail. | T: §24 *agent crash/restart*, kill -9 mid-write; §25 *WAL intact after crash* | 1 |
| BNQO-SEC-041 | WAL encryption at rest (§15) | Per-record AEAD XChaCha20-Poly1305; key file 0600 `bnqo-agent` (or TPM-sealed, -073), independent of identity key; nonce = 96-bit record-sequence-derived (no reuse); key rotated on identity rotation; reads authenticate before parse (tamper evident). | T: bit-flip ciphertext → detect + `wal-integrity-error` event | 2 |
| BNQO-SEC-042 | WAL quota & filesystem protection (§15) | Hard quota (default 2 GiB, configurable) enforced by agent accounting *and* bounded WAL dir allocation; watermark alerts 70/85/95 % (§17 *local queue near capacity*); WAL dir 0700 `bnqo-agent`, the only writable path under `ProtectSystem=strict`; `O_NOFOLLOW` + dir-fd-relative opens (anti-symlink). | T: §24 *72h offline agent* ×2 overflow; OS FS never exhausted | 1 |
| BNQO-SEC-043 | Idempotent ordered upload (§15, §4.1) | Batch = (agent_id, sequence range, config_version); precise batch ACK before WAL segment release; server-side dedup on (agent_id, sequence) via unique constraint; gap detection → `telemetry-delayed`, never silent acceptance; ordered resend after reconnect. | T: §24 *duplicate/out-of-order batches*; §25 *no logical duplicates after reconnect* | 2 |
| BNQO-SEC-044 | Buffer policy (§15) | ≥ 72 h configurable buffer; backpressure to scheduler (non-critical profiles shed first); priority queue retaining incident-window data on eviction; oldest-eviction otherwise; zstd batch compression. | T: outage-duration sweep; eviction-order assertions | 1 |

### 1.5 Packet capture (§18)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-050 | pcap default-off gate (§18) | Compile-time default off + runtime config flag; enabling requires approver+admin dual approval and step-up auth (-022); every enable/disable in audit chain. | T: enable without approval → denied | 4 |
| BNQO-SEC-051 | Capture constraints (§18) | Mandatory BPF filter (default: BNQO probe ports only); snaplen ≤ 128 B default; duration ≤ 5 min; volume cap; ring buffer, never unbounded; production-interface capture requires explicit scope field. | T: constraint-bypass attempts rejected | 4 |
| BNQO-SEC-052 | Capture protection (§18) | Encrypted before write with per-capture XChaCha20-Poly1305 key; key escrowed in Vault (audited unwrap); upload to restricted object-storage bucket (no public ACL, bucket policy allows ingest role only); auto-delete ≤ 24 h via lifecycle rule. | T: fetch raw object → ciphertext; auto-delete verified; A: bucket policy | 4 |
| BNQO-SEC-053 | Access & redaction (§18) | No payload rendering in UI (metadata only); download via short-lived (≤ 15 min) pre-signed URL after RBAC check; every access in audit chain; header-redaction filter on any exported summary; §25 *no secrets in logs/dumps/UI*. | T: canary-secret capture test; access-attempt audit | 4 |

### 1.6 Runtime hardening (§19) — see §3 for the full systemd blocks

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-060 | Dedicated identity & capabilities (§19, §4.1) | Users `bnqo-agent` / `bnqo-reflector` (nologin); agent ambient caps = `CAP_NET_RAW` only (ICMP); reflector caps = none (UDP sockets unprivileged); no setuid binaries shipped. | A: `getpcaps`, `systemd-analyze security` | 1 |
| BNQO-SEC-061 | systemd sandbox (§19) | Directives per §3 below: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `ProtectKernelTunables/Modules/Logs`, `ProtectControlGroups`, `RestrictNamespaces`, `RestrictSUIDSGID`, `LockPersonality`, `MemoryDenyWriteExecute` (agent: off only if eBPF JIT needs it, documented), `RestrictRealtime`, `SystemCallArchitectures=native`. | T: escape attempts → EPERM; A: `systemd-analyze security` score ≤ 2.0 | 1 (agent), 2 (reflector) |
| BNQO-SEC-062 | Syscall & MAC filtering (§19) | seccomp-bpf allowlist via `SystemCallFilter=@system-service @network-io @io-event` minus dangerous groups (`~@mount @reboot @swap @raw-io @cpu-emulation @obsolete @privileged`); AppArmor profile (Debian) / SELinux policy (RHEL) confining to WAL/config paths. | T: disallowed syscall kills process (audit mode in test) | 4 |
| BNQO-SEC-063 | Resource limits (§19, §25) | `CPUQuota` (idle target <1 %, §25), `MemoryMax=256M` (target <150 MB, §25), `MemoryHigh` soft, `TasksMax`, `LimitNOFILE=4096`, `IOWeight` low; in-agent token-bucket bandwidth shaper (configurable). | T: §25 performance benchmarks under load | 1 |
| BNQO-SEC-064 | Read-only filesystem (§19) | `ProtectSystem=strict` + `ReadWritePaths=/var/lib/bnqo-agent` (WAL, state) only; config delivered via control stream and written by agent into state dir; binary/config dirs read-only to the service user. | T: write attempts outside state dir → EPERM | 1 |
| BNQO-SEC-065 | Network egress policy (§19) | Host nftables/ufw egress rules: allow only CP/collector endpoints (TCP 443), NTP/NTS (UDP 123), assigned peer IPs on probe ports; all other egress dropped and logged; reflector ingress bound to configured IP/interface only (§4.2). | T: egress to arbitrary host blocked (§24 *arbitrary target* red-team) | 4 |
| BNQO-SEC-066 | No runtime code loading (§19) | Single static Rust binary (musl/static or pinned glibc); no plugin system, no dlopen of unverified code, no download-and-run; updates only as signed packages verified pre-install (-084/-085). | S: binary import audit; T: no network fetch code paths | 1 |
| BNQO-SEC-067 | Watchdog & self-health (§19, §4.1) | `WatchdogSec=30s` with agent sd_notify keepalive (hang ⇒ systemd kill+restart); `Restart=on-failure`, `RestartSec=5`, start-limit burst protection; self-health metrics exported (§7 agent process health). | T: hang injection, crash-loop test; §25 auto-recovery | 1 |

### 1.7 Secrets management (§20)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-070 | Central secret store (§20) | HashiCorp Vault (or org KMS equivalent) for CP secrets; dynamic PostgreSQL credentials (TTL ≤ 24 h) per service; job/config signing key in Vault transit or cloud KMS/HSM — non-exportable, every use audited; CA root offline, intermediate in KMS. | A: Vault audit log; T: DB cred expiry/renewal | 4 (interim sealed env files Phase 2) |
| BNQO-SEC-071 | Leak prevention (§20, §31, §5-004) | gitleaks + trufflehog in CI and pre-commit; structured-logging field denylist with automatic redaction (`authorization`, `token`, `secret`, `password`, key material); panic/fatal paths scrub env and never dump request bodies; TLS probe logs cert metadata only, never keys (§5-004); synthetic-probe tokens are dedicated least-privilege accounts, never real user credentials (§5-005). | S: CI secret scans; T: §25 *no secrets in logs/dumps/UI* grep harness with canary secrets | 1 (redaction), 2 (CI gates) |
| BNQO-SEC-072 | Rotation cadence (§20) | SVID ≤ 1 h; enrollment token ≤ 5 min single-use; dynamic DB creds ≤ 24 h; WAL key on identity rotation; session keys (reflector) ≤ 24 h (-090); IdP client secrets ≤ 90 d; CA intermediate ≤ 1 y; emergency rotate-and-revoke runbook. | A: rotation evidence; T: forced-rotation drill | 2/4 |
| BNQO-SEC-073 | Hardware key protection (§20, §10) | TPM 2.0: agent identity key and WAL key sealed to PCR policy where hardware present (non-exportable); HSM/KMS for CP signing and CA keys (§12, §20). | T: key export impossible on TPM hosts; A: attestation | 4 |

### 1.8 Supply chain (§21, NIST SSDF, SLSA Build L3 target)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-080 | Change control (§21) | Protected branches; 2 approving reviews (no self-merge, no self-approval of security-relevant paths via CODEOWNERS); signed commits (or CI-verified identity); linear history. | A: branch protection export; PR sample audit | 0–1 |
| BNQO-SEC-081 | CI security gates (§21) | Required checks: unit+integration tests; `-Zsanitizer`/race detector where applicable; `cargo-fuzz` targets on all parsers (probe packet, config, job, batch decoders) with corpus regression; SAST (semgrep + clippy `deny(warnings)`); `cargo-audit` + `cargo-deny` (advisories/bans/sources); license scan; secret scan (-071); container scan (trivy); IaC scan (checkov for Terraform/Ansible); frontend: npm audit + lockfile + `--ignore-scripts`. | S: pipeline definition; T: gate-demo with known-bad input fails build | 1 |
| BNQO-SEC-082 | SBOM (§21) | CycloneDX SBOM (cargo-cyclonedx / syft) generated per release for every artifact incl. container images; published with release; diffed between releases for review; retained for IR. | A: SBOM presence + diff review checklist | 1 |
| BNQO-SEC-083 | Hermetic/reproducible builds (§21) | Pinned Rust toolchain (`rust-toolchain.toml`); `cargo build --locked` with committed lockfiles; no git/patch deps in release profile; ephemeral isolated runners; rebuild-and-compare reproducibility check for agent binary where achievable. | T: reproducibility diff job; A: runner config | 4 |
| BNQO-SEC-084 | Artifact signing & provenance (§21) | Cosign sign (keys in KMS; keyless OIDC identity as secondary) for binaries/images/packages; SLSA Build L3 provenance attestation (GitHub Actions `slsa-github-generator` or equivalent) published to registry; **deploy-time gate**: Ansible/K8s admission verifies signature + provenance before any install; agent self-update verifies cosign signature against pinned key. | T: §24 *malicious update artifact* — unsigned/wrong-signed/no-provenance rejected | 4 |
| BNQO-SEC-085 | Release discipline (§21, §26) | Immutable release tags/artifacts; rollback via previous signed artifact; staged rollout: canary agents (5 %) → 25 % → 100 % with health gates; CP compatibility one version ahead/behind (§26) tested in CI. | T: rollout drill; A: release checklist | 4/6 |
| BNQO-SEC-086 | Vulnerability SLA (§21, §25) | Release gate: zero open critical/high in dependency + container + SAST scans (§25); triage SLA: critical 7 d, high 30 d, tracked exception register with expiry; emergency patch process. | S: gate evidence per release; A: exception register | 5 |

### 1.9 Measurement-plane security (§4.2, §5-002 — required by threat model TB1)

| ID | Control (requirement source) | Implementation mechanism | Verification | Phase |
|----|------------------------------|--------------------------|--------------|-------|
| BNQO-SEC-090 | Authenticated probe protocol (§4.2, §5-002) | Packet AEAD tag (AES-256-GCM or ChaCha20-Poly1305) over protocol_version, session_id, test_id, sequence_number, sender_timestamp, nonce, payload; session keys HKDF-derived per session from provisioned secret, rotated ≤ 24 h; sliding replay window (≥ 10k packets) on (session_id, sequence); timestamp bound ± skew; **silent drop** (zero bytes out) for unauthenticated/invalid/replayed packets; invalid/replay/auth-failure counters exported (§16 security view). | T: §24 *replay*, *invalid HMAC*; §25 *reflector silent to unknown packets* | 1 |
| BNQO-SEC-091 | Anti-amplification & rate limits (§4.2) | Structural: response length = min(request length, hard cap) ⇒ amplification factor ≤ 1.0; per-peer token-bucket rate limit + concurrent-session cap with auto-expire; global egress cap; packets above size cap dropped; bind only to configured IP/interface. | T: amplification harness measures bytes-in ≥ bytes-out under spoofed flood | 1 |
| BNQO-SEC-092 | Payload integrity accounting (§5-002) | Corrupted/tampered packet counters distinct from loss; integrity failures reported as security events, excluded from loss math (an integrity failure is not "packet loss" evidence). | T: bit-flip fuzz over probe packets; counter assertions | 1 |
| BNQO-SEC-093 | Telemetry identity binding (§4.1, §10) | Every batch carries agent_id + sequence_number + config_version; ingest gateway rejects batch if agent_id ≠ authenticated SVID SPIFFE ID or config_version unknown/revoked. | T: mismatched-identity batch rejected + audited (§25 *no telemetry without valid identity*) | 2 |
| BNQO-SEC-094 | Clock integrity (§3, §4.1, §7) | NTS-authenticated NTP (RFC 8915) to ≥ 2 diverse sources where available; agent reports source/offset/uncertainty; step-change detector: offset jump > 500 ms (configurable) ⇒ status `clock-unsynchronized` + alert (§17); OWD beyond quality limits stored as `invalid-clock-sync`/`low-confidence`, never precise; expiry/replay decisions use monotonic + server time, not raw local wall clock. | T: §24 *clock drift* + NTP-spoof lab; OWD flagged correctly (§25 accuracy) | 1 (detection), 3 (corroboration rules) |
| BNQO-SEC-095 | Ingest validation & decompression safety (§11, §15, §13) | Per-field bounds on all §13 record fields (ranges, units, monotonic counters, enum membership); streaming zstd decode with absolute uncompressed cap and ratio cap (anti-bomb); invalid records quarantined + counted, never partially applied. | T: §24 *oversized telemetry*, value-fuzz batch tests | 2 |

---

## 2. Control → requirement traceability (summary)

Every catalogue row cites its digest section inline. In reverse:

| RFP section | Controls |
|-------------|----------|
| §10 SR-IDENTITY-001/002/003 | -001, -002, -003, -004, -005, -006, -007, -073, -093 |
| §11 API security | -007, -010 … -022, -013, -095 (partial) |
| §12 Job security | -030 … -035, -031, -033, -034 |
| §15 WAL | -040 … -044, -095 (partial) |
| §18 pcap | -050 … -053, -022 |
| §19 Runtime hardening | -060 … -067 |
| §20 Secrets | -070 … -073, -071 |
| §21 Supply chain | -080 … -086 |
| §4.2 / §5-002 reflector & probe | -090, -091, -092 |
| §3 / §4.1 clock | -094 |

---

## 3. systemd unit hardening blocks (§19)

Shipped under `deploy/systemd/` per repo layout (§28). Both units assume state dir `/var/lib/bnqo-<role>` (mode 0700, owned by the role user) and config at `/etc/bnqo/<role>.toml` (read-only to the service).

### 3.1 `bnqo-agent.service`

```ini
[Unit]
Description=BNQO Probe Agent
Documentation=https://bnqo/docs/agent
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=notify
User=bnqo-agent
Group=bnqo-agent
ExecStart=/usr/bin/bnqo-agent --config /etc/bnqo/agent.toml
Restart=on-failure
RestartSec=5
WatchdogSec=30s

# --- Privilege floor (BNQO-SEC-060) ---
NoNewPrivileges=true
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW
SecureBits=keep-caps-locked noroot-locked

# --- Filesystem (BNQO-SEC-064) ---
ProtectSystem=strict
ReadWritePaths=/var/lib/bnqo-agent
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectProc=invisible
ProcSubset=pid

# --- Kernel / namespace surface (BNQO-SEC-061/-062) ---
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true                # blocks clock_settime (TM-011)
ProtectHostname=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictRealtime=true
SystemCallArchitectures=native
SystemCallFilter=@system-service @network-io @io-event
SystemCallFilter=~@mount @reboot @swap @raw-io @cpu-emulation @obsolete @privileged @debug
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK

# --- Resources (BNQO-SEC-063; §25 targets: idle CPU <1%, RAM <150MB) ---
CPUQuota=50%
MemoryHigh=200M
MemoryMax=256M
TasksMax=64
LimitNOFILE=4096
IOWeight=10

# --- Environment ---
Environment=BNQO_LOG_FORMAT=json
UMask=0077

[Install]
WantedBy=multi-user.target
```

Note: `MemoryDenyWriteExecute=true` is dropped only if an eBPF-enabled build requires JIT; that variant is a separate unit file with an ADR (BNQO-SEC-062). `ProtectClock=true` is deliberate: the agent *reports* clock state but must never *set* it (TM-011).

### 3.2 `bnqo-reflector.service`

```ini
[Unit]
Description=BNQO Secure Reflector
Documentation=https://bnqo/docs/reflector
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=notify
User=bnqo-reflector
Group=bnqo-reflector
ExecStart=/usr/bin/bnqo-reflector --config /etc/bnqo/reflector.toml
Restart=on-failure
RestartSec=5
WatchdogSec=30s

# --- Privilege floor: reflector needs NO capabilities (UDP only) ---
NoNewPrivileges=true
CapabilityBoundingSet=
SecureBits=noroot-locked

# --- Filesystem: reflector is fully read-only ---
ProtectSystem=strict
ReadWritePaths=/var/lib/bnqo-reflector
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectProc=invisible
ProcSubset=pid

# --- Kernel / namespace surface ---
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictRealtime=true
SystemCallArchitectures=native
SystemCallFilter=@system-service @network-io @io-event
SystemCallFilter=~@mount @reboot @swap @raw-io @cpu-emulation @obsolete @privileged @debug
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

# --- Resources ---
CPUQuota=50%
MemoryHigh=128M
MemoryMax=192M
TasksMax=32
LimitNOFILE=2048
IOWeight=10

Environment=BNQO_LOG_FORMAT=json
UMask=0077

[Install]
WantedBy=multi-user.target
```

Egress restriction (BNQO-SEC-065) is applied by a companion nftables unit (`deploy/ansible` role), not by systemd, because egress policy must reference peer IPs and CP endpoints managed by configuration.

---

## 4. Compliance cross-reference

OWASP API = OWASP API Security Top 10 (2023). NIST ZT = NIST SP 800-207 zero-trust tenets (T1 resources; T2 secure comms regardless of location; T3 per-request access; T4 dynamic policy; T5 asset integrity monitoring; T6 dynamic strict authN/Z; T7 telemetry for posture). SSDF = NIST SP 800-218 practices (PO/PW/PS/RV groups). SLSA = SLSA v1.0 Build level the control contributes to.

| Control(s) | OWASP API 2023 | NIST SP 800-207 | NIST SSDF v1.1 | SLSA |
|------------|----------------|-----------------|----------------|------|
| BNQO-SEC-001..007 (identity/mTLS) | API2 Broken Authentication | T2, T3, T6 | PW.1, PW.4 | — |
| BNQO-SEC-010, -021, -022 (user authn/session) | API2 | T3, T6 | PW.1 | — |
| BNQO-SEC-011 (RBAC/per-object) | API1 BOLA, API5 BFLA | T3, T4 | PW.1 | — |
| BNQO-SEC-012, -013, -019, -020 (validation/errors) | API3, API8 Security Misconfiguration | T3 | PW.1, PW.5 | — |
| BNQO-SEC-014, -017, -018 (resource limits) | API4 Unrestricted Resource Consumption | T3, T5 | PW.1 | — |
| BNQO-SEC-015, -043, -093 (replay/idempotency) | API2, API6 Sensitive Business Flows | T3 | PW.1 | — |
| BNQO-SEC-016, -035 (audit) | API9 Improper Inventory/Logging | T7 | PO.3, RV.1 | — |
| BNQO-SEC-030..035 (jobs) | API5, API7 SSRF, API8 | T3, T4 | PW.1, PW.5 | — |
| BNQO-SEC-034 (anti-SSRF) | API7 SSRF | T4 | PW.5 | — |
| BNQO-SEC-040..044 (WAL) | API8 (data integrity), API4 | T5 | PS.2, PW.4 | — |
| BNQO-SEC-050..053 (pcap) | API1, API3 (sensitive data), API8 | T3, T4 | PW.1, PW.5 | — |
| BNQO-SEC-060..067 (runtime hardening) | API8 Security Misconfiguration | T5 | PW.4, PW.7 | — |
| BNQO-SEC-070..073 (secrets) | API2, API8 | T5, T6 | PW.4, PO.5 | — |
| BNQO-SEC-080, -081 (change control, CI gates) | — | T5 | PO.1, PO.2, PO.4, PW.4, PW.7, PW.8 | Build L1–L2 |
| BNQO-SEC-082 (SBOM) | API9 Inventory | T5 | PO.4, PW.4, RV.1 | Build L1 |
| BNQO-SEC-083, -084 (hermetic build, signing, provenance) | — | T5 | PS.1, PS.2, PS.3 | **Build L3 (target)** |
| BNQO-SEC-085, -086 (release discipline, vuln SLA) | — | T5, T7 | RV.1, RV.2, RV.3 | Build L3 (sustained) |
| BNQO-SEC-090..095 (measurement plane) | API4, API8 | T2, T5, T7 | PW.1, PW.5 | — |

---

## 5. Acceptance-test mapping (RFP §25 Security criteria)

Test IDs `SAT-SEC-nn` live in `tests/security/` (repo layout §28); executed in Phase 5 validation and as a release gate thereafter (§30).

| §25 Security acceptance criterion | Proving test(s) | Primary controls |
|-----------------------------------|-----------------|------------------|
| No telemetry without valid identity | SAT-SEC-01: connect/upload attempts with no cert, self-signed cert, wrong-SPIFFE cert, expired enrollment — all rejected, each audited (§24 *forged cert*) | -001, -006, -007, -093 |
| Revoked cert rejected immediately/within window | SAT-SEC-02: revoke live SVID; measure rejection ≤ 60 s at collector and CP; expiry mid-run handled without false acceptance (§24 *cert expiration/revocation*) | -005, -007 |
| Replay rejected + audited | SAT-SEC-03: recorded probe packets replayed to reflector (silent drop, counter increments); recorded batch replayed to ingest (dedup reject + audit event) (§24 *replay*) | -090, -015, -043 |
| Reflector silent to unknown packets | SAT-SEC-04: fuzz of unauthenticated/malformed/wrong-HMAC packets incl. spoofed sources; packet capture on egress proves zero response bytes; amplification ratio ≤ 1.0 for authenticated peer flood (§24 *invalid HMAC*) | -090, -091 |
| Unsigned job not executed | SAT-SEC-05: unsigned, wrongly-signed, tampered-field, expired, duplicate, wrong-config_version jobs — none executed; rejection reasons audited (§24 *invalid config signature* class) | -030, -031, -032, -035 |
| No arbitrary shell | SAT-SEC-06: injection strings in every job/config string field (§24 *shell/SQL injection*); CI artifact check: no `std::process::Command`/exec symbols in agent+reflector binaries; attempted exec via any channel fails | -030, -081 |
| Throughput limits enforced | SAT-SEC-07: API request above cap (rejected); forged job above cap (agent-clamped); tampered config raising cap (signature reject); shaper measured ≤ cap ± tolerance; production-harm auto-stop triggered under injected errors (§24 *excessive jobs*) | -033, -034, -067 |
| No secrets in logs/dumps/UI | SAT-SEC-08: plant canary secrets (fake tokens/keys) through all flows; grep full test-run logs, crash dumps, WAL dump tooling output, pcap export path, and UI API responses — zero hits; CI secret-scan gate green | -071, -052, -053, -019 |
| No open critical/high vulns before release | SAT-SEC-09: release-gate pipeline job aggregates cargo-audit/cargo-deny, trivy, semgrep, npm audit results; any open critical/high without a tracked exception blocks the release tag | -081, -086 |

Two §25 criteria with security overlap are covered elsewhere: *OWD only with valid clock confidence* → SAT-SEC-03-adjacent clock tests under BNQO-SEC-094 (§24 *clock drift*); *invalid config never replaces valid one* → SAT-SEC-05 plus the §24 *invalid config signature* platform scenario.

---

## 6. Phasing summary

| Phase | Controls landing |
|-------|------------------|
| 0 — Design | This matrix + threat model; -080 change control |
| 1 — Measurement core | -040, -042, -044, -060, -061(agent), -063, -064, -066, -067, -071(redaction), -081, -082, -090, -091, -092, -094(detection) |
| 2 — Control plane | -001..-007 (interim CA), -012, -013, -015, -016, -019, -020, -030..-035, -041, -043, -070(interim), -072(partial), -093, -095 |
| 3 — Observability | -010, -011, -014, -017, -018, -021, -094(corroboration) |
| 4 — Security hardening | -001/-002 (SPIRE), -005(CRL dist), -022, -050..-053, -061(reflector), -062, -065, -070..-073, -083, -084, -085 |
| 5 — Validation | SAT-SEC-01..09 full run; pentest; -086 gate live |
| 6 — Production | -085 staged rollout executed; continuous audit (-016 verification job); quarterly access review |
