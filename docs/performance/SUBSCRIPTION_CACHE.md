# Subscription response cache

## Problem

The public subscription route `/s/<server_id>/<sub_id>` performed two to four live
X-UI panel round trips on **every** request: authenticate (`get_xui_session`), read
the inbound list, resolve the client, and read profile metadata for the fast
config-only path (plus the live usage/expiry read for the statistics path). VPN
clients poll subscriptions on their own schedule and thousands of clients can share
a handful of subscription ids, so the request path paid the panel latency and the
panel paid the load every time.

## Change

New module `panel/core/subscription_cache.py` caches the **rendered response**
(body, status, headers) per `(server, subscription id, variant)`:

* short TTL (30 s for the live view, 300 s for the config-only fast path), a
  bounded LRU (`EVE_SUBSCRIPTION_CACHE_MAX_ENTRIES`, 2000) and counters;
* single-flight bookkeeping (`begin`/`end`/`wait_for_fill`) so a burst of misses
  for the same key renders once and the followers reuse the result;
* `invalidate_server(server_id)`, called by the four cached-client write-through
  helpers (patch/add/remove/clone) that change a subscription's credentials.

The route returns a cached response with `X-Eve-Cache: hit` (a live render gets
`miss`); with the cache disabled the header is absent. Cached responses keep their
`no-store` header, so clients and CDNs still see the route as uncacheable - the
cache is server-side only.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_SUBSCRIPTION_CACHE_ENABLED | 1 | master switch |
| EVE_SUBSCRIPTION_CACHE_TTL_SECONDS | 30 | live view (usage/expiry) |
| EVE_SUBSCRIPTION_CONFIG_CACHE_TTL_SECONDS | 300 | config-only fast path |
| EVE_SUBSCRIPTION_CACHE_MAX_ENTRIES | 2000 | LRU bound |
| EVE_SUBSCRIPTION_CACHE_WAIT_SECONDS | 5 | follower stampede wait |

## Measured

`scripts/benchmark_subscription_cache.py` (`--json docs/performance/subscription-cache.json`),
400 requests spread over 20 subscription keys with a 20 ms simulated panel read:

| | panel reads | wall time | hit rate |
|---|---|---|---|
| without cache | 400 | 8,328 ms | - |
| with cache | **20** | **418 ms** | 95% |

That is **19.9x faster** on that workload and 95% fewer panel reads, plus:
20 concurrent callers asking for the same missing key produce **1 render**.

## Limits

* The cache is per process; another worker's cache only learns about a credential
  change through its TTL (30 s for the live view, 300 s for the config path) unless
  the change happened in that worker. Invalidation covers the write-through helpers
  in the worker that performed the mutation.
* The live view (statistics enabled) can serve usage/expiry values up to the TTL
  old; operators who need exact per-request values can disable the cache or lower
  the TTL.
* Admin-facing live reads (`/api/client/direct-link/...`) are intentionally not
  cached: they are low-volume and expected to be authoritative.
* The periodic background fetch does not invalidate the cache (that would defeat
  the hit rate); only credential-changing mutations do.

## Tests

`tests/test_subscription_cache.py`: TTL expiry, LRU eviction, per-server
invalidation, disabled cache, single-flight bookkeeping (begin/end/wait), metrics;
route-level first-miss/then-hit with a stubbed panel (panel call count stays at 1),
invalidation forcing a fresh read, the disabled path advertising no cache header,
a 404 without caching, and the measurement script.
