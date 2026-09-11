# Live updates over server-sent events

## Why

Phase 11 made a dashboard poll cheap (7.2 ms / 140 B when nothing changed, see
DELTA_SYNC.md), but a client still has to ask. SSE inverts that: the server tells
the client the moment the snapshot revision moves, so updates are immediate and
idle clients stop asking at all.

## Protocol

`GET /api/refresh/stream` (authenticated, `text/event-stream`):

```
retry: 3000
event: hello    data: {"revision": 42, "last_update": "...", "tick_seconds": 1.0, "max_seconds": 120}
event: changed  data: {"mode": "delta", "revision": 43, "last_update": "..."}
: keep-alive
event: bye      data: {"reason": "max_lifetime"}
```

The stream deliberately carries **no snapshot data**: `changed` is a nudge and the
client then calls `/api/refresh?since=<revision>`, so the full/delta/unchanged
decision and the client-side merge stay in exactly one place. A client that
connects without a revision (`?since=` or `X-Eve-Snapshot`) starts at "now"; its
first HTTP load bootstraps the data.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| EVE_SSE_ENABLED | 0 (off) | serve live updates at all |
| EVE_SSE_MAX_STREAMS | 8 | concurrent streams this process will hold (over that: HTTP 503, client keeps polling) |
| EVE_SSE_MAX_SECONDS | 120 | maximum stream lifetime; the client reconnects after it |
| EVE_SSE_TICK_SECONDS | 1.0 | how often the revision is checked |
| EVE_SSE_HEARTBEAT_SECONDS | 20 | keep-alive comment interval while nothing changes |

## Capacity and why it is opt-in

With gunicorn sync workers every open stream occupies a worker thread for up to
EVE_SSE_MAX_SECONDS. A stream is cheap when idle (one in-memory revision check per
tick, `~0.01 ms`) but the thread is not available for other requests, so an
operator enables this deliberately and sizes it: keep

    EVE_SSE_MAX_STREAMS x gunicorn workers <= threads available

or give the stream its own worker class. The cap returns 503 instead of queueing,
and the dashboard falls back to its polling interval whenever the stream is
unavailable, whatever the reason.

## Client behaviour

- The EventSource is opened only when the page was rendered with
  `sse_enabled` true, so the server flag is the single switch.
- Every `changed` event runs the existing silent refresh (HTTP with `since`), so
  deltas, the "unchanged" fast path and the merge are reused unchanged.
- EventSource reconnects automatically after a `bye` or a dropped connection.
- The auto-refresh interval stays as a safety net and is slowed to at least 30 s
  while live updates are enabled.

## Proxy notes

- `X-Accel-Buffering: no` and `Cache-Control: no-store` are set on the response;
  nginx must not buffer `text/event-stream` and its `proxy_read_timeout` should be
  larger than the heartbeat interval.
- The response is not compressed (the compress middleware targets JSON/HTML) and
  the security headers are applied as for any other authenticated response.

## Multi-worker

Each stream periodically re-hydrates the local snapshot from Redis (every 5
ticks), so a change published by another gunicorn worker still reaches clients on
this worker. Without Redis each worker only sees its own snapshot.

## Measured

Same process and dataset as the phase 10/11 measurements (test client, 18k clients
for the snapshot, 200 idle ticks):

| Item | Cost |
|------|------|
| connect + `hello` | 8.0 ms, 121 byte event |
| idle revision check (per tick, per stream) | 0.013 ms |
| `changed` nudge | 75 bytes |
| the HTTP call the nudge triggers | phase 11 numbers: 7.2 ms / 140 B unchanged, 135 ms / 27 KB for one changed inbound |

So an idle dashboard with live updates costs one thread and ~0.013 ms per second
instead of a poll every few seconds, and a change costs one nudge plus the same
delta a poll would have fetched.

## Tests

`tests/test_sse_updates.py`: event formatting, environment limits with invalid
input, disabled -> 404, unauthenticated -> 401, stream cap -> 503, stream headers,
`hello` + `changed` (delta) after a snapshot update, unknown revision announced as
`full`, connect-without-revision staying quiet, keep-alive comments, and
`bye` at the max lifetime with the stream slot released.
