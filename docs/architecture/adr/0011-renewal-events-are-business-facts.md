# ADR-0011: Renewal events are business facts, not inferred usage-counter resets

* Status: accepted
* Date: 2026-09-13
* Supersedes the implicit contract that a usage-counter decrease means a renewal

## Context

The subscription page recommends a package from usage history. The model in place
(`usage-fit-v4`) computed an average daily rate over a rolling 31-day window and divided by a
calendar span, deliberately ignoring renewal events: the only renewal rows that existed were
created by the usage collector whenever a counter decreased, and a counter can decrease for
reasons that are not a renewal at all — the panel reset one inbound, the canonical inbound
changed, an account was re-created, the panel restarted. Using those rows as cycle boundaries
produced false short cycles and wildly inflated forecasts, so v4 stopped using them entirely.

That fixed the false cycle and created a worse failure: a customer who consumes heavily after a
renewal is averaged back into the previous month. Reported case: 60GB over 31 days (1.94
GB/day) with 30GB in the 8 days since the renewal (3.75 GB/day, +93%). The recommendation was
a package sized for 1.94 GB/day.

## Decision

1. **Telemetry, business events and analytics are separate layers.** `UsageCounterState` /
   `UsageHourly` / `UsageDaily` describe what the counters recorded. `RenewalEvent` describes
   what happened to the customer's commercial cycle. Analytics combine the two and are never
   written back as either.
2. **Only an explicit, verified mutation creates a cycle boundary.** When EVE performs a
   renewal it writes the panel, reads it back, matches the expected state and *then* records
   `RenewalEvent(event_type='renewal'|'package_change', verified=True, operation_id=…)` inside
   the same transaction. A renewal therefore does not need the usage counter to reset, and a
   failed write or read-back cannot create a boundary.
3. **A counter decrease is recorded as telemetry:** `event_type='inferred_reset'`,
   `source='counter_reset'`, `verified=False`. It can never be read as a renewal, and it is
   deduplicated against a verified renewal inside a ±5 minute window so one renewal does not
   become two events.
4. **Rollover is stored apart from purchase.** `granted_volume_bytes` is what the customer
   bought, `carried_over_bytes` is the unused volume that survived, and
   `new_volume_limit_bytes` is the resulting panel cap (which in Eve also covers traffic already
   consumed). Analytics size packages from `granted`, never from the cap: a 50GB purchase on top
   of 10GB of leftover is not a 60GB purchase.
5. **Recent verified behaviour is never silently averaged away.** The recommendation computes
   the current cycle and the rolling window independently, detects the trend between them, and
   weights the forecast towards the cycle when the evidence warrants it (0.80/0.20 on a strong
   increase or an early exhaustion, 0.65/0.35 for a mature stable cycle, the history alone when
   there is no verified cycle).
6. **Idempotency is a database constraint**, not a code convention:
   `UNIQUE(operation_id, event_type)`.
7. **Rollout is explicit.** `EVE_USAGE_RECOMMENDATION_V5` (or the `usage_recommendation_v5`
   system setting) selects `on` (default), `shadow` (compute both, answer with v4) or `off`
   (v4). v4 stays in the tree as the rollback path for one release.

## Consequences

* The recommendation can now change when a customer's behaviour changes, which is the point:
  the reported case now forecasts ~105GB instead of 60GB and recommends a package that covers
  it.
* A panel that never reports a verifiable renewal read-back yields no cycle and falls back to
  the rolling history with lower confidence, rather than a guessed cycle.
* Legacy `renewal_events` rows were created by the counter observer, so migration
  `b8d2e3f4a5c6` backfills them as `inferred_reset` / `counter_reset` / `verified=false` with
  `created_at = renewed_at`. They remain visible as history but cannot become cycle boundaries.
* The recommendation depends on the correctness of the renewal write path: a mutation that
  skips the verified event simply leaves the account on its rolling history. That is a
  deliberate trade — a missing boundary degrades the answer, a wrong one corrupts it.
* Deprecated payload fields (`source`, `fast_cycle`, a single `confidence` label) stay for one
  release so templates and Telegram automations keep working; the v5 vocabulary
  (`forecast_basis`, `signals.early_exhaustion`, `confidence.data` / `behavior_stability`)
  replaces them afterwards.
* Removal (RFP section 58): `usage-fit-v4`, the legacy `fast_cycle`/`source` semantics and the
  deprecated aliases are deleted one release after activation, once the shadow and activated
  metrics show the model behaving.

## Alternatives considered

* **Keep ignoring renewal events and just shorten the window.** Rejected: a short window is
  noisy, and the RFP's requirement is that the *cycle* is the unit of consumption, not that the
  window is shorter.
* **Use counter decreases as cycle boundaries but flag them.** Rejected: the false-boundary
  problem is inherent to inferring a commercial fact from telemetry, and a flag invites exactly
  the misreading the model must not allow.
* **Store the cycle start on the client row instead of an event table.** Rejected: business
  events are history (audit, idempotency, migration provenance); an overwritten column loses it.
* **Model the forecast with a fitted trend line.** Rejected for now: the RFP asks for a
  deterministic, explainable model; blend weights and threshold tables can be reviewed and
  tested, a fitted model cannot be explained to a customer in one sentence.
