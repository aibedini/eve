# Usage intelligence: telemetry, business events and derived analytics

## The rule

Three layers answer three different questions, and none of them may impersonate another
(RFP section 70):

| Layer | Question | Lives in |
|-------|----------|----------|
| Telemetry | what did the counters record? | `usage_counter_state`, `usage_hourly`, `usage_daily`, the live X-UI snapshot |
| Business events | what happened to the customer's commercial cycle? | `renewal_events` (RenewalEvent v2) |
| Analytics | what does that mean for the next package? | `panel/services/usage_intelligence/` |

Concretely: a counter decrease is **telemetry** and is recorded as `inferred_reset` /
`counter_reset` / `verified=false`; a renewal EVE performed itself is a **business fact** and is
recorded as `renewal` / `explicit_renew` / `verified=true` **after the panel write has been read
back and matched**; the recommendation is a **derived** answer that reads both and is never
written back as either.

The bug this replaced: the recommendation averaged the rolling 31-day window and deliberately
ignored renewal events, because the only events that existed were inferred from counter
movements and a false short cycle would have distorted the forecast. A customer who consumed
30GB in the 8 days after a renewal was therefore diluted into a 1.94 GB/day month and offered a
60GB package. Recent behaviour must never be silently averaged away by stale history.

## Layers in detail

### Telemetry (`panel/jobs/schedulers.py`)

* `UsageCounterState` — the latest observed counter per account (one row, updated in place).
* `UsageHourly` (48h, mutable) and `UsageDaily` (one compact row per account per Tehran day,
  one year) — the rollups the analytics read. `upload_bytes`/`download_bytes` are that period's
  deltas; `opening_*`/`closing_*` are the raw counters at the period's edges.
* The collector computes reset-safe deltas (`_usage_delta`) and, when a counter drops, records
  an inferred reset signal through the business-event writer — deduplicated against a verified
  renewal inside a ±5 minute window, so one real renewal cannot become two events.
* Tehran day boundaries use a fixed +03:30 offset (no DST since 2022). `metrics.TEHRAN_OFFSET`
  mirrors the collector's constant, because the collector imports this package.

### Business events (`panel/services/usage_intelligence/events.py`)

`RenewalEvent` v2 (migration `b8d2e3f4a5c6`) stores:

* `event_type`: `renewal`, `quota_topup`, `traffic_reset`, `package_change`,
  `expiry_extension`, `inferred_reset`;
* `source`: `explicit_renew`, `admin_reset`, `telegram_renew`, `api_mutation`,
  `counter_reset`, `inferred`, `migration`;
* the rollover accounting kept apart: `previous_volume_limit_bytes`,
  `new_volume_limit_bytes`, `previous_remaining_bytes`, `carried_over_bytes`,
  `granted_volume_bytes`;
* `operation_id` with `UNIQUE(operation_id, event_type)` for idempotent replays;
* `verified` / `verified_at`, true only after the panel read-back;
* indexes `(server_id, sub_id, renewed_at)` and `(server_id, sub_id, verified, renewed_at)`.

**Only a verified event of type `renewal` or `package_change` starts a cycle**
(`RenewalEvent.is_cycle_boundary`). A quota top-up adds volume to the current cycle rather than
restarting it; everything else is a signal.

### Analytics (`panel/services/usage_intelligence/`)

| Module | Responsibility |
|--------|----------------|
| `schemas.py` | the contracts: model version, maturity/trend/forecast/margin tables, metric dataclasses |
| `metrics.py` | the only place history is read: bounded, indexed queries; freshness; reset-safe counter math |
| `cycles.py` | the current cycle, the rolling 31 days, the pre-cycle baseline |
| `analysis.py` | one pass that gathers every window, the exhaustion signals and the query count |
| `trend.py` | cycle rate vs rolling rate, with confidence-aware thresholds |
| `forecast.py` | the blend, the winsorization bounds and the safety margin |
| `confidence.py` | `data_confidence` and `behavior_stability` as separate answers |
| `packages.py` | the recommended and comfort offers, capacity limits, unlimited rules |
| `copy.py` | the Persian/English explanation sentences |
| `recommendation.py` | the v5 payload and the rollout flag |
| `observability.py`, `shadow.py` | counters, the PII-free log line, the shadow comparison |

The split is enforced by direction: `schemas` ← `metrics` ← `cycles`/`analysis` ←
`trend`/`forecast`/`confidence`/`packages` ← `recommendation`. Everything from `trend`
downwards is a pure function over metrics, so the algorithm is unit tested without Flask, a
database or a panel.

## The model

1. **Cycle boundary.** The latest verified `renewal`/`package_change` for the account. No
   verified event means *no* current cycle (the rolling history answers, with a low-confidence
   basis) — never a guessed one.
2. **Current cycle metrics.** Elapsed time from precise seconds, floored at 0.25 days so
   minutes-old evidence cannot produce an absurd forecast, and bucketed as
   `insufficient` (<6h) / `very_early` (<1d) / `early` (<5d) / `medium` (<14d) / `mature`.
   Usage comes from the exact counter anchor (`previous_limit − previous_remaining`, or the
   counter itself after a traffic reset) and falls back to summing the daily rows observed since
   the boundary, reporting which it used.
3. **Rolling 31 days and the pre-cycle baseline.** Both are computed independently; neither
   replaces the other. The baseline answers "did behaviour change relative to the month before
   the cycle?".
4. **Trend.** `cycle_rate / rolling_rate` classified as `strong_decrease` / `decreasing` /
   `stable` / `increasing` / `strong_increase`, with wider thresholds while the cycle is still
   short evidence. A missing denominator yields no ratio rather than an invented one.
5. **Exhaustion.** If the quota ran out, the elapsed share of the expected package duration
   decides severity (`critical` <35%, `high` <60%); an early exhaustion outranks a stable trend.
6. **Forecast.** A weighted blend of the cycle and rolling rates — 0.80/0.20 on a strong
   increase or early exhaustion, 0.65/0.35 for a mature stable cycle, 0.45/0.55 for a young
   cycle, the history alone without a cycle — over daily observations winsorized at
   `max(3 × median, P90)`.
7. **Package choice.** The smallest finite offer that covers the forecast *for its own
   duration*, plus the safety margin (10/15/20/25%), a comfort step up, `capacity_limited` with
   its shortfall when even the largest offer is insufficient, and unlimited only when nothing
   finite can cover the demand.
8. **Confidence.** `data_confidence` (elapsed days, observed days, samples, telemetry freshness
   with stale/unknown downgrading) and `behavior_stability` (trend, daily variance, baseline
   drift, exhaustion) are reported separately, so `data=high, behavior_stability=low` is a
   supported answer.
9. **Explanation.** The payload carries Persian and English sentences quoting the measured
   rates, and the subscription page prints them with four evidence rows.

## Failure semantics

* A panel write or read-back failure creates **no** verified event, so no cycle can start from
  a failed mutation (the event is written inside the caller's transaction, after verification).
* A duplicate event insert is refused by `UNIQUE(operation_id, event_type)`; the writer checks
  first and returns the existing row, so a retry is idempotent.
* A recommendation never calls a panel (the benchmark fails any outbound HTTP request) and
  never scans the global snapshot (the live counter is passed in by the caller).
* Recommendation failures fall back to v4 and are counted, never surfaced as an error to the
  customer.

## Privacy

The recommendation reads and returns usage numbers, package ids and rates. The analytics log
line and the shadow records carry `server_id`, a stable non-reversible account reference
(`acct-<12 hex>`), the metrics and the package ids — never an email, a phone number, a UUID or a
subscription token. The RenewalEvent `client_email_snapshot` exists for support lookups and is
excluded from `to_dict()` unless redaction is explicitly disabled.

## Operations

* `/api/doctor` → `checks.usage_intelligence`: the rollout mode, the recommendation counters
  (basis, trend, exhaustion, stale telemetry, capacity limits, errors), the latency summary and
  the shadow comparison metrics plus recent extreme cases.
* Budgets and index evidence: `docs/performance/USAGE_INTELLIGENCE_PERF.md`.
* Rollout and rollback: `EVE_USAGE_RECOMMENDATION_V5` (`on` default, `shadow` to compare,
  `off` to restore v4) or the `usage_recommendation_v5` system setting.
* The required regression list (RFP 45/46/48/49): `tests/test_usage_intelligence_regression.py`.

## Related documents

* ADR-0011 in `docs/architecture/adr/0011-renewal-events-are-business-facts.md`
* `docs/performance/USAGE_INTELLIGENCE_PERF.md` — latency, statement budget and indexes
* `docs/performance/SERVER_POLLING.md` — how the live counters reach the process
* `docs/performance/REGRESSION_SUITE.md` — the mutation/cache/UI suite this builds on
