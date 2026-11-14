# Renewal progress and request budget

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
