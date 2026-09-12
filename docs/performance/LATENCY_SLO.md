# Latency SLOs: mutation -> cache -> UI

## Why

Every phase of the mutation/cache/UI program removed a specific delay. Without a number
attached, "fast enough" is opinion, and a regression (a per-request X-UI call creeping
back into the read path, a mutation that stops patching the cache, a stream that only
delivers on the next full poll) would be invisible until an operator noticed it. These
five budgets are the contract; `scripts/benchmark_latency_slo.py` measures each one on
the real code path and **exits non-zero when a p95 misses**, so the CI job fails.

## The budgets

| SLO | Budget (p95) | What it covers |
|-----|--------------|----------------|
| `mutation_cache_commit_ms` | < 100 ms | the write-through commit after the panel answered: `patch_cached_client` patches every cached copy, recomputes the server stats, marks the delta dirty and invalidates the subscription cache |
| `browser_visible_ms` | < 300 ms | the same mutation plus the canonical `client_state` and the JSON body the browser patches the card from - everything the operator waits for over the network |
| `cache_read_ms` | < 50 ms | `GET /api/refresh?mode=cache` through the real Flask app against a warm snapshot: the read path that must never call X-UI |
| `other_tabs_ms` | < 1 s | a `client.changed` event recorded by one process until a real SSE stream (`/api/refresh/stream`, reader thread) observes it |
| `external_xui_ms` | < 3 s | an external change in X-UI for a panel the operator watches (or one an Eve mutation just touched): the panel is marked hot and the real periodic cycle is polled until it is fetched again |

`browser_visible_ms` is a network budget on purpose. The DOM patch it ends in is a
synchronous single-card update (`applyClientMutation`, no refetch, no await);
`tests/test_ui_design_system.py` guards that it stays a patch and does not fall back to
a full refetch, so a regression there fails a test rather than being hidden by this
number.

## Measured

`python scripts/benchmark_latency_slo.py --json docs/performance/latency-slo.json`
(200 cached clients in the snapshot; 120 mutation/cache iterations, 8 stream events,
5 external-change cycles):

| SLO | p95 | mean | max | budget |
|-----|-----|------|-----|--------|
| mutation_cache_commit_ms | 23.5 | 14.0 | 41.7 | 100 |
| browser_visible_ms | 23.6 | 14.1 | 41.8 | 300 |
| cache_read_ms | 20.6 | 13.3 | 30.5 | 50 |
| other_tabs_ms | 50.8 | 22.4 | 50.8 | 1000 |
| external_xui_ms | 2038.3 | 2022.8 | 2038.3 | 3000 |

`other_tabs_ms` is dominated by the stream tick (`EVE_SSE_TICK_SECONDS`, 1 s by default,
0.05 s in the benchmark), so the production number is ~1 s + the drain cost - still inside
the budget, and the reason the event is pushed per client instead of waiting for a
snapshot revision.

`external_xui_ms` is dominated by `EVE_SERVER_POLL_ACTIVE_SECONDS` (2 s): the mark an Eve
mutation or a browser view sets pulls the next poll in to that interval, so an external
change is visible within one active interval plus the fetch. See
`docs/performance/SERVER_POLLING.md`.

## Running it

```
python scripts/benchmark_latency_slo.py --quick          # ~10 s, CI gate
python scripts/benchmark_latency_slo.py --json docs/performance/latency-slo.json
```

`--quick` reduces the sample counts; the budgets are identical, so a quick run is a real
gate, not a weaker one. CI runs it in the `latency-slo` job (`--quick`).

## Limits

* Measured in-process on sqlite with no Redis: a real deployment adds one Redis round
  trip to the write-through path and the SSE drain, and PostgreSQL costs a little more
  than sqlite. The budgets were chosen with that headroom in mind (3-4x for the cache and
  mutation metrics).
* The benchmark never calls X-UI, by design: the read path must not depend on a panel
  being up. `external_xui_ms` measures the schedule, with the panel fetch stubbed.
* A single-process measurement, so it does not see cross-worker Redis latency; that is
  the documented limitation of the in-process harness, not of the SLO.

## Tests

`tests/test_latency_slo.py` runs the script in quick mode and fails when any budget is
missed, when the JSON artifact loses a metric or its budget, when the documented budgets
drift from `SLOS` in the script, or when the CI workflow stops running the gate.
