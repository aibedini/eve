# Renewal progress and request budget

## Parallel dispatch and carrier truth (2.23.12)

New renewal requests start the SMS worker immediately after the panel accepts
the write, before the first verification read. SMS dispatch no longer waits for
activation or a manual re-check. Its durable marker survives business finalization
and verification errors; recovery uses the same gateway idempotency key.
Validation still determines account status, charging and the business record.

Without a gateway request ID the timeline stays on preparation. Queue acceptance,
Android submission and carrier delivery are distinct. Renewal history and detail
now use the same signed evidence projection as SMS Center, including multipart
receipts and the unconfirmed receipt window. Evidence is read for all visible
logs in one bounded query; callbacks can update the modal without a gateway poll.

Background polling continues after terminal successful gateway submission while
the carrier receipt is pending. It stops on delivery or an explicitly unavailable
receipt, and bounds missing receipt polling to six hours. Expired receipt lookup
cannot undo a confirmed send, and stale pending responses cannot undo delivered.
No production gateway delivery latency has been measured locally.

## History and gateway status fixes (2.23.11)

History contains the current operator's latest 50 renewals across all time,
subject to current account access. It is not a rolling date window. History and
detail polling now have a dedicated 6000/hour, 40000/day limit: the default
500/hour limit was below a visible dialog's 1.5-second polling rate. The browser
keeps existing rows on errors, distinguishes failure from an empty history, and
honors Retry-After with backoff instead of repeatedly hitting a blocked endpoint.

Renewal and notification use separate horizontal timelines. Notification states
distinguish preparation, gateway queue/acceptance, and carrier delivery; neither
worker scheduling nor gateway acceptance is described as delivered. A confirmed
renewal never reverts to an unknown renewal outcome because history polling fails.

Re-check reads account verification and the operation's matching SMS gateway
record in parallel. It displays a timestamped changed, unchanged, partial, or
failed result. The gateway read is bounded to eight seconds and never sends a
message, repeats a renewal, or charges again. Manual-review outcomes do not offer
an unsafe duplicate send. Success notifications still wait for verified renewal.

Browser validation covers 320, 375, 480, 768 and 1440 pixel widths, RTL/LTR,
light/dark themes, four persistent footer actions, history reload, rate-limit
recovery, explicit re-check feedback and late-response isolation. Gateway tests
use mocks; no production delivery time or speedup is claimed.

The progress modal and the global Activity button use the durable `ClientOperation`
ledger. Routine progress requests read EVE's database, not the remote panel.
History is restricted to the current operator and rechecks current reseller access.
The list is bounded to 50 entries, batches SMS-log lookups, and omits customer text
and template variables. Detail is fetched only when the selected record changes.

## Changes to waiting and remote work

| Path | Previous behavior | Current behavior |
| --- | --- | --- |
| Renewal button | Timers guessed the current stage at 700 ms and 3500 ms | Opens the progress modal immediately; ledger state determines the stage |
| Automatic browser observation | Repeated remote-panel verification POSTs while the modal was open | Read-only ledger GETs; observation also runs after closing the modal |
| Inline activation repair | Up to 3 repair/read cycles, each with a 400 ms sleep | At most 1 repair/read cycle, without an artificial sleep |
| Background activation | 30-second tick, 10-second minimum age | 5-second tick, 2-second minimum age; existing singleton and batch limit retained |
| Notification identity | Transactional key derived from the current second | Stable opaque key per renewal operation and explicit failed-SMS retry |

An unconfirmed activation remains pending and resumable. These budgets do not
turn an accepted write into a verified success, and never bypass verification,
renew twice, reset traffic twice, or charge a second time. SMS retry is offered
only for a confirmed terminal gateway failure and checks current activation again;
an ambiguous gateway outcome cannot be retried from this control.

Gateway acceptance and carrier delivery have separate labels. Scheduling the SMS
worker is not labelled as acceptance or delivery. Historical operations without a
linked notification are shown as having unavailable SMS status.

## Validation and limits

`test_inline_activation_repairs_once_without_blocking_sleep` drives the real renewal
route against a controlled panel that keeps one membership disabled. It verifies
one repair, one transaction, a pending outcome, no success SMS, and no blocking
sleep. `tests.test_renew_progress` checks owner isolation, access revocation,
read-only progress, delivery semantics, stable identity, and guarded SMS retry.

Browser checks with mocked transport covered 320, 375, 480, 768 and 1440 px,
Persian RTL and English LTR, light/dark themes, background history after reload,
SMS-only retry, and a late recheck response while viewing a different operation.

No production panel or SMS gateway was contacted for these measurements. Remote
panel latency, gateway throughput and carrier delivery time remain external;
there is no claimed production speedup in milliseconds. The reconciliation tick
is a scheduling interval, not a completion guarantee, especially under load.

The existing static deployment check
`test_the_subscription_page_references_only_its_own_builds_assets` fails because
the subscription page versions `sub-inbound-status.js` with the build identity
rather than its content version. The same failure was reproduced from an archive
of unchanged HEAD `88076f0`; that subscription asset and template are outside this
change. Dashboard asset versioning, including the new progress script, passes.
