# ADR-0003: SPIFFE/SPIRE for agent identity, with org-CA fallback

- Status: Accepted
- Date: 2026-07-28
- Satisfies: RFP §10 SR-IDENTITY-001/002/003, §22 (agent spoofing, cert theft, expired cert, telemetry replay), §25 (revoked cert rejected within window), §26 (HA of identity issuance)

## Context

RFP §10 forbids static API keys for agents (SR-IDENTITY-001) and states the preferred
mechanism: SPIFFE ID + SPIRE server/agent, short-lived X.509-SVIDs, mTLS, node +
workload attestation. It names the acceptable alternative explicitly: an org private
CA with per-agent unique certs, TPM 2.0 key storage where available, auto-rotation,
revocation, short lifetimes. Bootstrap must use one-time, minutes-valid, single-agent
enrollment tokens with fingerprint + node-attribute checks and audit
(SR-IDENTITY-002). Authorization must be per-agent scoped (SR-IDENTITY-003).

The question is whether to adopt SPIRE for the initial 3-server scope (where it is
operationally the heaviest option) or start with the private-CA alternative.

## Decision

Adopt **SPIFFE/SPIRE as the identity plane from day one**, including for the
3-server initial scope, with a documented fallback to an org private CA.

Design:

- **SPIFFE IDs**: `spiffe://bnqo.<environment>/<tenant>/agent/<agent_id>` for
  workloads; `spiffe://bnqo.<environment>/cp/<service>` for control-plane services
  (collectors, scheduler, stream processor also authenticate with SVIDs — TB2/TB3 in
  ARCHITECTURE.md §4).
- **Node attestation**: `join_token` plugin for the initial scope — the join token
  *is* the SR-IDENTITY-002 enrollment token (one-time, minutes TTL, single-agent,
  invalidated after use, enrollment audited). Upgrade path: `tpm`/`x509pop`
  attestation where hardware allows, satisfying the TPM preference of
  SR-IDENTITY-001.
- **Workload attestation**: Unix plugin (uid=`bnqo`, binary path/hash) so only the
  agent process on an attested node receives the agent SVID.
- **SVID lifetime**: 1 hour, rotated at ~50% by the SPIRE agent; telemetry mTLS and
  the control stream both present the X.509-SVID (tonic + rustls, cert reload on
  rotation without reconnect storms).
- **Revocation**: SPIRE CRL + bundle propagation; additionally the API gateway and
  collectors consult a denylist cache (Redis, TTL ≤ 60s) fed by the identity service
  so a revoked agent is rejected "immediately/within window" per RFP §25.
- **Authorization binding**: the gateway/collector pin `agent_id` claims to the SVID
  SPIFFE ID — an agent can send only its own telemetry and receive only its own jobs
  (SR-IDENTITY-003).
- **HA** (RFP §26): SPIRE server pair, PostgreSQL datastore (same HA Postgres as
  inventory), agents cache SVIDs so an issuance outage is invisible until cached
  expiry; rotation happens well before expiry.
- **Job/config signatures remain separate**: Ed25519 signing keys for jobs and
  configs (ADR-0009) are independent of SVIDs, so a stolen SVID alone cannot forge
  jobs, and a compromised scheduler key alone cannot impersonate agents.

### Fallback strategy (org private CA)

If SPIRE proves operationally unacceptable (e.g., an environment forbids running the
SPIRE agent, or staffing cannot support it in Phase 2):

1. Stand up a two-tier org CA (offline root, online intermediate in Vault PKI).
2. Issue per-agent client certs (7-day lifetime) with CN/O carrying
   `agent_id`/`tenant`, keys generated into TPM 2.0 where available (SR-IDENTITY-001
   alternative verbatim).
3. Reuse the same enrollment-token bootstrap (SR-IDENTITY-002): the token authorizes
   exactly one CSR for one fingerprinted node.
4. Rotation via an agent-driven CSR flow over the existing control stream; revocation
   via CRL + the same gateway denylist cache.
5. The identity interface inside the agent (`IdentityProvider: current_svid() →
   rustls::ClientConfig`) is already an abstraction seam; the CA fallback implements
   the same trait, so telemetry/control code is unchanged.

The fallback loses automatic workload attestation and short (1h) lifetimes; that
residual risk is recorded in the threat model (RFP §22, "cert theft" row) and
partially compensated by TPM key storage and shorter cert lifetimes.

## Consequences

Positive: satisfies the *preferred* normative option; short-lived identity shrinks
the cert-theft window from days to an hour; uniform identity for agents and
control-plane services simplifies mTLS everywhere.

Negative: two extra daemons (spire-server, spire-agent) to deploy/operate/secure;
SPIRE server becomes a Tier-0 component (mitigated by HA pair + cached SVIDs);
join-token attestation is only as strong as token handling (mitigated by one-time,
minutes-TTL, audited use per SR-IDENTITY-002).

## Alternatives considered

- **Org private CA as primary** (SR-IDENTITY-001 alternative): viable and documented
  as fallback; rejected as primary because it lacks workload attestation, pushes
  rotation automation onto us, and would need re-engineering later to reach the
  preferred posture.
- **Static API keys / bootstrap tokens as steady-state identity**: explicitly
  forbidden by SR-IDENTITY-001.
- **mTLS with cert-per-agent issued manually**: fails auto-rotation and scale to
  1,000 agents (RFP §1); rejected.
