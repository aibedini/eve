# Telemetry state transitions and the depletion notification pipeline

Why this document exists: a customer whose traffic ran out could sit at "2 GB
remaining" on the dashboard while X-UI already reported *Volume Ended*, and the
reminder that should have fired at that transition did not exist. This is the
design that removes the possibility, the invariants it relies on, and the evidence
that each one holds.

## The bug, stated precisely

The periodic SMS scan was the **detector**. Every 30 minutes it read the shared
snapshot and asked *"which accounts look depleted right now?"*. Three failures
follow from that shape:

1. **Missed transition.** The scan only ever considers the state in front of it. If
   a customer's traffic ran out between two runs and the snapshot the scan read was
   the one from before the change, no candidate existed and no reminder was queued.
   A later run does not repair it either: by then the account may have renewed, and
   the scan happily classifies it "active".
2. **Two races around a renewal.** A reminder could be classified before a renewal
   and delivered after it (RACE A), or be created from a snapshot that predates the
   renewal (RACE B).
3. **No ordering between panel reads.** Nothing decided which of two in-flight
   reads of one panel was *newer*. `telemetry_updated_at` did not help: it is
   stamped when a result is APPLIED, so a slow read that returns late looks newer
   than the fast read that already landed, and applying it reverts the ledger.

## The shape that replaces it

```mermaid
sequenceDiagram
    autonumber
    participant X as X-UI panel
    participant F as Background fetcher
    participant L as Observed-state ledger
    participant O as Notification outbox
    participant W as Delivery worker
    participant G as GMweb gateway
    participant D as Dashboard (SSE)

    X->>F: fresh client payload (ticket taken before the read)
    F->>F: process_inbounds -> ONE canonical state calculator
    F->>L: compare + record (batched, material changes only)
    L-->>O: one row per transition (UNIQUE event_id = the race barrier)
    F->>D: snapshot revision bump -> delta + client.changed
    W->>O: claim due rows (lease; SKIP LOCKED on PostgreSQL)
    W->>W: re-read lifecycle generation (renewal fence)
    W->>W: recompute state from the live snapshot (state fence)
    W->>G: POST /send with generation + stable Idempotency-Key
    G-->>W: accepted / 429 / failure
    W->>O: sent | retry(ladder) | superseded | skipped | deferred
    Note over W,O: quiet hours, budgets and limits DEFER an event, never drop it
```

The scan is still there, in the opposite role: it **reconciles**. It records what
the snapshot holds into the ledger (so a transition nobody observed becomes an
event instead of a silence) and drains the same outbox.

## Invariants and where each one lives

| Invariant | Mechanism | Test |
| --- | --- | --- |
| Detector is fresh telemetry, not a timer | `schedulers._apply_result` -> `telemetry_state.record_observations` | `test_the_reported_bug_two_gigabytes_to_ended_creates_one_event` |
| One reminder per transition, not per poll | `UNIQUE(event_id)`, `event_id = st:sha256(service_key, state, state_version)` | `test_repeated_polls_of_an_ended_service_never_duplicate` |
| Two workers racing the same transition converge | the INSERT loses to the constraint (no check-then-insert) | `claim` tests + UNIQUE constraint |
| A slow read cannot revert a newer one | `panel/core/fetch_sequence.py` ticket + compare-and-set | `CrossProcessPropagationTests` |
| A renewal retires queued reminders | `lifecycle.handle_successful_service_lifecycle_change` -> `supersede_pending`, plus the worker's generation re-read | `test_a_renewed_service_supersedes_its_queued_reminder` |
| Exactly one worker sends an event | claim UPDATE guarded by status + `FOR UPDATE SKIP LOCKED` | `test_claiming_is_exclusive_and_a_dead_lease_is_reclaimed` |
| A crashed worker's event is not lost | lease timeout -> `retry` | same test |
| A broken gateway cannot retry forever | bounded ladder (30s..3h, then `failed_terminal`) | `test_retry_ladder_is_bounded_and_terminal` |
| Quiet hours and budgets do not lose a reminder | `mark_skipped(..., retry_in=...)` -> `next_attempt_at` | delivery tests |
| A dashboard in the web process speeds up the loop in the background process | shared watch marks in Redis | `test_watch_propagation_crossprocess.py` |
| The per-state trigger the operator configured is honoured | canonical state -> SMS vocabulary translation | `test_shadow_mode_records_instead_of_sending` and the trigger gate |
| A warning cannot consume a terminal transition's budget | the cooldown is keyed by notification kind (`messaging.cooldown_events_for_state`) | `test_yesterdays_warning_does_not_consume_the_ended_transition` |
| A cooldown defers an event, it never drops it | `mark_skipped(..., retry_in=<remaining>)` | `test_a_same_kind_cooldown_defers_with_the_remaining_time` |
| An operator-disabled trigger still delivers when switched on | the trigger gate defers hourly instead of closing the event | `test_enabling_the_trigger_later_delivers_what_it_held` |
| Introducing the ledger cannot text everyone at once | first fresh observation is a silent baseline | `test_first_observation_is_a_baseline_not_a_transition` |
| ...while genuinely missed accounts are still caught | the reconciliation pass may raise an event for an already-actionable service, under every existing cap | `test_reconciliation_detects_a_transition_nobody_observed` |
| The detector never blocks the dashboard | transition recording is best-effort; the snapshot publishes regardless | `schedulers._record_transitions` |
| No PII in telemetry counters | doctor block carries counts only | `test_metrics_never_carry_customer_identifiers` |

## Rollout: `EVE_DEPLETION_EVENT_PIPELINE`

| Value | Detector | Sender | Use |
| --- | --- | --- | --- |
| `off` | pipeline records nothing | periodic scan only | escape hatch; the pre-migration behaviour |
| `shadow` | pipeline records transitions | periodic scan only; the pipeline marks events `shadowed` | prove the detector on real traffic without risking a duplicate SMS |
| `on` (default) | pipeline records transitions | the pipeline delivers; the scan reconciles | steady state |

The invariant the flag protects: **the legacy scan and the outbox never both send
for one logical transition.** `off` and `shadow` keep the scan as the sender and
the pipeline silent; `on` moves sending to the outbox and reduces the scan to
reconciliation.

## Ordering: why a ticket and not a timestamp

Panel reads overlap (periodic fan-out, "refresh now", the targeted recheck a
candidate triggers, a read-back after a write) and X-UI reports counters, not an
observation time. `fetch_sequence.begin()` takes a ticket before the read;
`accept()` is a compare-and-set that refuses any result whose ticket is older than
the newest one already applied for that server. The watermark lives in Redis so
the web process, the background fetcher and the CLI share it. Without Redis the
same semantics hold per process -- correct for a single-process install, and never
silently reordered inside one process.

## What may suppress a terminal notification

Four different gates produce the same symptom -- the customer reaches
`volume_ended` and no SMS arrives -- so the policy has to be stated, not inferred:

| Gate | Effect on the event | Why |
| --- | --- | --- |
| no email / no recipient | terminal `skipped` | nothing can be sent, ever |
| SMS automation disabled | defer 10 min | an operator usually turns it back on; the transition is real |
| **this state's trigger is off** | defer 1 h | operator policy, but a material transition only creates an event once: closing it would leave a hole that switching the trigger on could never fill |
| reseller-owned by policy | terminal `skipped` | the owner's automation is the one that should message |
| lifecycle generation advanced | `superseded` | the renewal already answered it |
| live state no longer matches | `superseded` | the claim is stale |
| **same-kind cooldown active** | **defer to its expiry** | a duplicate within the window; the event is not wrong, only early |
| quiet hours, hourly/daily budget, pace, gateway | defer | temporary by definition |
| no template, opted out, unlimited skip, expired/ended too old | terminal `skipped` | configuration or policy, not a race |

The cooldown is **per (account, server, notification kind)**, and a kind is the
state plus its channel twins: `sms_low_volume` and `tg_low_volume` are the same
notification, `sms_low_volume` and `sms_ended` are not. The legacy combined
`depletion` row counts for the two warning kinds only, so an install that ran the
pre-granular trigger does not re-text its warnings on upgrade while terminal
transitions ignore it. The reason this matters is a real production case: a
`low_volume` warning sent 24 h earlier made the `volume_ended` delivery branch
close the event as a terminal `skipped` with no retry, so the terminal notice was
lost permanently -- not delayed.

### Forensics: "why did this account not get its SMS?"

```bash
python scripts/forensic_sms_delivery.py --email <account> --server-id <id> --hours 72
```

Read-only (it never sends, writes or claims anything) and prints the observed
state, every notification event for the service, the pipeline mode, the settings
that gate delivery, the automation log the cooldown reads, the SMS send log, and
then walks the real delivery gates in their real order and names the one that
refuses. Values only: no API key, template body or phone number is printable
(`SAFE_SETTINGS` is a whitelist, and a test asserts it).

## Operational surface

`GET /doctor/summary` (Eve Doctor) carries a `telemetry_pipeline` block:

```json
{
  "mode": "on",
  "detection_enabled": true,
  "delivery_enabled": true,
  "legacy_sender_active": false,
  "outbox": {
    "pending": 0,
    "by_status": {"sent": 12, "superseded": 1},
    "oldest_pending_age_seconds": null,
    "overdue": 0,
    "observed_services": 431,
    "max_attempts": 7,
    "backoff_seconds": [30, 120, 600, 1800, 3600, 10800]
  },
  "fetch_sequence": {"backend": "redis", "tracked_servers": 4},
  "watch_marks": {"shared_backend": "redis", "shared": {"6": "dashboard"}}
}
```

Counters only: never a phone number, an email address or a message body. The SMS
send log keeps the per-attempt audit (state, lifecycle generation, correlation id,
idempotency key, gateway response), which is what answers "was this message created
before or after the renewal that supposedly answered it?".

## What is deliberately NOT done

* No X-UI call from a public subscription request -- the public path keeps its cache
  and stale-while-revalidate protection.
* No per-second full-panel polling and no per-second SMS scan. The delivery worker
  ticks every few seconds but touches only a few indexed rows and does no panel I/O;
  the fast cadence follows an explicit watch mark, not the whole install.
* No `sleep()` used as synchronisation; pacing is a bounded delay before a send and
  nothing else depends on it.
* No phone number as identity. Identity is `eve:<server_id>:<client_uuid>`
  (`panel/services/lifecycle.py`); one phone may own several services.
* No process-local lock for a cross-process fact. Distinct-state work uses database
  constraints and leases; schedule state uses Redis.
