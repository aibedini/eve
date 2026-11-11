# Live server network rates

Servers and Dashboard use authenticated `GET /api/servers/network`. It returns
cached samples immediately and schedules at most eight status-only requests per
process. Visible pages poll every five seconds, stop after authentication errors,
and back off on failures or rate limits.

The adapter reads `netIO.up` (server send) and `netIO.down` (server receive), in
bytes per second, from the existing status API. Conversion is
`bytes/s * 8 / 1_000_000` Mbps. Cumulative `netTraffic.sent/recv` are not used.
Panels that omit rates remain unavailable rather than showing zero.

Redis shares five-second samples and per-server leases across workers. Without
Redis, caching and coalescing are per process. Local fallback memory is bounded.
The first visit can show waiting until the next poll. Large fleets can show
partial coverage while their oldest samples are refreshed.

Only accessible, enabled, visible servers are sampled. Samples older than 15
seconds, failed reads, and missing rates are excluded from totals; the reporting
count makes partial totals explicit. Zero is a valid measurement. The browser
also ages out values when polling stops or connectivity fails.

Rates include non-VPN traffic and are not per-client measurements or a perfectly
synchronized fleet measurement. Configured panels on the same VPS can double-count
traffic. Tests use mocked panels, fake clocks, and fake Redis. Deployment checks
should compare both directions with 3x-ui, disconnect a panel, then confirm
partial coverage and recovery. A recent-history graph would be a useful next step.

## Verification for 2.22.0

- Ten network unit/access tests cover units, zero, invalid values, shared caching,
  cross-worker leases, asynchronous request bounds, unavailable panels, stale
  totals, and authentication/visibility filtering.
- The affected Tier 1 modules and Tier 2 integrations passed except the existing
  `test_subscription_scope` selector-baseline failure. Replaying that test against
  `HEAD`'s unchanged stylesheet reproduces the same five selectors.
- The design-system audit and release-profile check pass. Playwright checks with
  mocked rates pass at 480/768/1440 px, dark/light themes, RTL, aggregate conversion,
  stale/unavailable states, and no JavaScript errors. Live VPS rates were not tested.
