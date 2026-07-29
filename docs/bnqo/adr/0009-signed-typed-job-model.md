# ADR-0009: Signed typed-job model — no remote shell, ever

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §12 (typed jobs only; free-form command/shell/arbitrary URL/executable path FORBIDDEN; job field set), §4.1 (no arbitrary shell from control plane; verifies digital signature of configs), §4.3 (NO general remote shell), §22 (shell exec via job, SSRF via arbitrary target, config tampering, compromised CP), §25 (no duplicate job execution, unsigned job not executed, invalid config never replaces valid one), §31 (no arbitrary remote command)

## Context

The control plane must trigger on-demand diagnostics on agents (MTR bursts, MTU
discovery, throughput tests, host snapshots, identity rotation, config updates —
RFP §12's job list). This is the highest-blast-radius channel in the system: an
agent executes control-plane instructions on production hosts with `CAP_NET_RAW`.
RFP §22 lists the exact threats: shell execution via job, SSRF via arbitrary
targets, config tampering, and a compromised control plane itself. RFP §12 fixes
the allowed job types and the mandatory field set; the design decision is the
enforcement architecture that makes "typed, signed, scoped" a property of the
agent binary rather than a policy promise of the control plane.

## Decision

**A closed, signed, typed-job model.** The agent contains no command parser, no
shell invocation, no plugin loader, and no path from network input to
`exec`/`spawn` (RFP §19: no downloading/running unknown binaries at runtime).

- **Job schema** (protobuf `bnqo.jobs.v1.Job`, canonical deterministic encoding):
  `job_id, job_type, agent_id, peer_id, parameters, created_at, not_before,
  expires_at, max_duration, max_bandwidth, approval_id, config_version, signature`
  — the RFP §12 field set verbatim. `job_type` ∈
  {`RUN_ICMP_PROBE, RUN_UDP_PROBE, RUN_TCP_PROBE, RUN_TLS_PROBE, RUN_MTR,
  RUN_ROUTE_TRACE, RUN_MTU_DISCOVERY, RUN_THROUGHPUT_TEST, COLLECT_HOST_SNAPSHOT,
  ROTATE_IDENTITY, UPDATE_SIGNED_CONFIG`}. `parameters` is a per-type protobuf
  message with validated enums/ranges — e.g. `RunMtrParameters{cycles:1..64,
  protocol: ICMP|UDP|TCP, max_hops:1..32}`; there is **no free-form string that
  reaches a shell, a URL fetch, or a filesystem path**.
- **Signature**: Ed25519 over the canonical encoding, signed by the scheduler's
  job-signing key; the public key set ships in the signed config (key pinning with
  rotation via `UPDATE_SIGNED_CONFIG`). Config signatures work identically
  (RFP §4.1: agent verifies digital signature of configs).
- **Agent-side admission control** (defense in depth against a compromised CP,
  RFP §22):
  1. signature valid against pinned key set — else reject + audit;
  2. `agent_id` matches this agent; `peer_id` ∈ the agent's signed peer registry
     (kills SSRF/arbitrary-target jobs, SR-IDENTITY-003);
  3. `not_before ≤ now ≤ expires_at`; `config_version` ≥ current (anti-rollback);
  4. `job_id` not in the persisted executed-jobs ledger (exactly-once execution,
     RFP §25);
  5. `approval_id` present and valid for heavy types (`RUN_THROUGHPUT_TEST`,
     Profile C/D triggers) — the approval is minted by the RBAC/approval workflow
     (RFP §4.3, §5-008) and verified by signature, not by trusting the scheduler's
     say-so;
  6. `max_duration`/`max_bandwidth` clamped by the agent's own hard policy
     ceilings — a job can never exceed agent-side caps even if signed.
- **Execution**: each job type maps to one Rust enum variant handled by the
  corresponding engine (ADR-0001: the match is exhaustive at compile time — adding
  a job type is a code change, not a config change). Results stream to the WAL as
  priority records; job acks via `AcknowledgeJob` (RFP §23) with
  executed/failed/rejected + reason, all audited.
- **Audit**: every admission decision (accepted/rejected + reason) is logged
  locally and reported as a `security_events` record (RFP §16 security view,
  §22 "suspicious jobs").

## Consequences

Positive: "unsigned job not executed", "no arbitrary shell", and "no duplicate job
execution" (RFP §25) become testable properties of the binary; a fully compromised
scheduler cannot exceed the agent's peer registry, ceilings, or approval rules —
residual risk reduced to what signed-and-approved typed jobs legitimately allow;
the threat-model rows for shell-exec/SSRF/config-tampering (RFP §22) map to
concrete validation tests in `tests/security/`.

Negative: every new diagnostic capability requires an agent release (accepted —
that friction is the security property); job-signing key management becomes
critical (keys held in Vault/KMS per RFP §20, signing happens in the scheduler
with quorum-approved key ceremonies); operators used to ad-hoc shell debugging lose
that path — replaced by `COLLECT_HOST_SNAPSHOT` and the diagnostic job set.

## Alternatives considered

- **Restricted shell / allow-listed commands** (e.g. permit `mtr` with flag
  filtering): still a parser-security problem (flag injection, shell metacharacters,
  PATH manipulation) and explicitly forbidden by RFP §12/§31. Rejected.
- **Signed scripts / WASM plugins**: runtime code loading violates RFP §19 (no
  downloading/running unknown binaries) and blows up the audit/attestation surface.
  Rejected.
- **Pull-only config with no jobs** (diagnostics as config changes): too slow for
  incident response and conflates steady-state config with one-shot actions;
  RFP §12 mandates the job channel. Rejected.
