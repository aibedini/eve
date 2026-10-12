# Dashboard account audit and statistics contract

Read-only production audit on 2026-10-12, before the aggregation change.
No accounts, quotas, traffic counters or inbound assignments were changed.
Each visible panel was authenticated once, its inbound list read once, and a
single account traffic endpoint sampled when available. No identifiers were
logged. The hidden TEST server was excluded.

| Server | Version | Authenticated first-class API | Raw membership rows | Unique email accounts |
|---|---|---|---:|---:|
| PLUS #1 | 3.9.0 | supported | 3,103 | 389 |
| bplus #2 | 3.4.1 | supported | 3,861 | 483 |
| ECO1 #6 | 3.3.0 | supported | 9,704 | 1,213 |
| ECO2 #7 | 3.3.1 | supported | 8,146 | 1,018 |
| ECO3 #8 | 3.6.0 | supported | 12,850 | 1,605 |
| ECO4 #9 | 3.3.0 | supported | 6,886 | 862 |
| ECO5 #10 | 3.3.0 | supported | 8,410 | 1,052 |
| Total | | | 52,960 | 6,622 |

The user's earlier screenshot showed 21,879 clients. This is not a simultaneous
measurement with the audit; it cannot establish an exact historical correction.
The audit does establish that membership counts and UUID-first cache statistics
are unsuitable for these first-class account panels. ECO3's cached statistics
reported 9,853 clients against 1,605 emails in the read-only panel response.
PLUS's published block contained zero inbounds despite a successful status with
389 accounts; its live panel response contained twelve inbounds.

## Identity and traffic

First-class v3 account identity is server + normalized email, matching the
email-addressed API. UUID is fallback evidence for accounts without email.
Conflicting UUID/email evidence increments a diagnostic counter and membership
overrides retain the source evidence. Legacy identity includes the inbound;
equal emails on independent legacy inbounds are separate clients.

Across all seven panels, repeated clientStats counters for the same account were
identical. Available sampled `/clients/traffic/{email}` responses matched the
inbound copies. ECO1's sampled global traffic read was unavailable; its repeated
inbound counters were still identical. For this first-class API contract,
inbound copies are account traffic, not independent usage to sum. The aggregator
counts one agreeing counter pair. It excludes contradictory or missing evidence
and exposes `traffic_incomplete_clients`; it does not choose first, maximum or
sum of contradictory account counters. Explicitly labelled independent
membership traffic is summed once per inbound. No per-account API requests are
added to ordinary dashboard aggregation.

## Remaining quota and authorization

`remaining_raw` is finite quota less verified account traffic, once per account,
only when global enable is true, at least one enabled inbound membership exists,
expiry is not reached, and quota remains. Negative expiry (awaiting first use)
is eligible. Unlimited quotas do not contribute finite bytes. Separately,
`unused_allocated_raw` includes disabled or expired accounts, and
`allocated_quota_raw` records their allocation. Reseller authorization filters
memberships before aggregation; the shared snapshot is never modified.
Settings enable and traffic-record enable are independent evidence. The account
enable field follows client settings, not the traffic-record flag. Accounts with
unverified finite capacity are counted in `unknown_clients`, rather than claimed
inactive. Both traffic and remaining labels disclose incomplete evidence.

## Publication and recovery

Publishing one server must not hydrate the entire shared manifest over another
server's fetched but unpublished block. Pending blocks and their statistics are
preserved by snapshot readers until publication. A count mismatch between a
server block and its statistics rejects publication. HTTP synchronization
includes a compact inbound/client-row inventory. A tab with a missing block or
truncated membership list requests a full cache snapshot before acknowledging
the revision; incomplete shared snapshots cannot replace the last coherent view.
This recovery does not enqueue more panel reads.

Aggregation is O(accounts + memberships), with O(accounts) identity state and
membership traffic state only for explicitly independent counters. Normalized
v3 snapshots retain one ordinary email entity plus lightweight memberships.
The HTTP inventory is O(inbounds), not O(clients). No new performance benchmark
has been run locally; regression tests and existing latency/scale gates run in CI.
