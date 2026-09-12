# Mutation / cache / UI regression suite

## Why a named suite

The mutation -> cache -> UI program (phases 0-14) changed how an edit reaches the screen:
the panel answers, the worker commits the cache itself, the response carries the verified
state, the browser patches one card, and other tabs are told over SSE. Each phase has its
own tests, but the failure that matters in production is a *combination* - a renew that
commits the cache but never reaches the second tab, a delete that removes the row without
announcing it, a refresh that started before the edit and reverts it.

`tests/test_regression_matrix.py` is that place. Run it before a release:

```
python -m pytest tests/test_regression_matrix.py -q
```

It is also part of the focused `unit-tests` job in `.github/workflows/tests.yml`, so a
regression here fails CI by name.

## What it covers

Every operation goes through its **real route** with a stubbed 3x-ui panel, and each one
asserts the same four post-conditions: the panel write carried what the operator asked
for, the shared cache reflects it immediately (no manual refresh), other tabs are told
(`client.changed` with the operation), and the browser's cursor (the snapshot revision)
moved.

| Operation | Route | Also asserted |
|-----------|-------|---------------|
| Renew | `POST /api/client/<s>/<i>/<email>/renew` | the response carries `client_state` (what the card is patched from) and the verified read-back |
| Disable | `POST /api/client/<s>/<i>/toggle` (`enable: false`) | `#nosms` opt-out tag, panel write carries `enable: false` |
| Enable | same route, `enable: true` | the opt-out tags come back off |
| Expiry edit | `POST /api/client/<s>/<i>/<email>/edit` | `cache_sync: true`, the new `expiryTime`/`totalGB` in the cache |
| Usage reset | `POST /api/client/<s>/<i>/reset` | up/down zeroed and reformatted in the cache |
| Delete | `POST /api/client/<s>/<i>/<email>/delete` | the row is gone from the cache, the event carries `deleted: true` |
| Read after a mutation | `GET /api/refresh?mode=cache` | **zero** outbound HTTP: the read path never calls a panel |

Cross-cutting scenarios:

* **Cache miss** - the panel answered, this worker has no matching row: the renew response
  still carries the verified state, a targeted repair is queued, and the mutation is still
  announced (with a forced snapshot revision, so a viewer's cursor reaches it).
* **Stale refresh** - a refresh that started before the edit must not revert it: the cycle
  discards its own result for that server *and* keeps the live block when it commits the
  snapshot, and the operator's values survive.
* **Multi-tab** - the event carries the client, the operation and the verified state (a
  delete travels as a delete), replayed oldest-first and idempotently from a cursor.
* **Subscription** - a cached subscription is served without reading the panel, and a
  burst of misses for one key produces exactly one render (single-flight).

## Related suites

The phase-level details live next to it and are still run by the full suite:
`test_renew_enable.py` (renew/enable semantics and the verify path),
`test_client_mutation_result.py` (the `ClientMutationResult` contract),
`test_config_vs_telemetry.py` (config vs telemetry stamps), `test_refresh_reconcile.py`
(the Refresh button as a tracked reconcile), `test_client_events.py` and
`test_sse_updates.py` (the SSE fast path), `test_subscription_cache.py` (stale serving,
pre-warm), `test_server_polling.py` (per-server cadence),
`test_latency_slo.py` and `test_mutation_scale.py` (the budgets).
