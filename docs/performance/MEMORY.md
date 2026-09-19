# Memory: attribution before optimization

## Why this document exists first

"3.45 GB of 3.78 GB is used" is not yet a finding. On a Linux host that number includes
Redis, PostgreSQL, nginx, Xray children and the page cache, and it says nothing about which
part is Eve, which part is reclaimable, or whether anything is growing. Two specific traps
made the earlier reading unreliable:

* **RSS is not additive.** Summing RSS across Eve's processes counts the Python
  interpreter, libc and every shared wheel once per process, which turns a normal
  multi-process install into a fake memory problem. `panel/core/memory_report.py` therefore
  aggregates on **PSS** (shared pages divided by the processes that map them).
* **Page cache is not application memory.** Linux deliberately fills free RAM with cache.
  `used` is computed as `total - MemAvailable`, and the cache is reported separately, so
  reclaimable memory is never presented as pressure.

The order is deliberate: measure, then rank, then change representation. Nothing in this
document proposes a fix that has not been measured on the install it applies to.

## What now measures it

| Surface | What it answers |
|---|---|
| `panel/core/memory_report.py` | host totals/pressure, per-process RSS/PSS/USS/threads/peak/uptime, Eve's roles, snapshot duplication, Redis snapshot bytes, caches, bounded trend |
| `GET /api/system/memory` (superadmin) | the same payload Settings → Overview renders |
| `POST /api/system/memory/analyze` (superadmin + step-up) | explicit, bounded deep Python sample: largest allocations as file/line/size only |
| Settings → Overview → Memory | the human-readable version: host, Eve PSS, roles table, snapshot, caches, trend, health notes |
| `health_watchdog` | one sample a minute into a Redis ring (`LPUSH` + `LTRIM`, capped at 1440 entries) |

The trend is what separates the two very different explanations of a high number:

```
600 MB -> 700 MB -> 850 MB -> 1.1 GB -> 1.5 GB     a leak
restart -> 400 MB -> full dashboard -> 950 MB, flat  retained snapshot architecture
```

Sources: `/proc/meminfo`, `/proc/pressure/memory`, `/proc/<pid>/smaps_rollup` (RSS, PSS,
PSS_Anon/File/Shmem, Private_*), `/proc/<pid>/status` (VmHWM peak, Threads, VmSwap),
`/proc/<pid>/cmdline` (role). No new dependency: `psutil` is not required and was not added.
Where `/proc` is absent every section reports `available: false` with a reason instead of
inventing zeros, which is what a Windows/dev checkout shows.

Nothing in the payload is a credential, a command, an environment value or customer data -
only counts, byte sizes, pids, roles and ages - and a unit test asserts that
(`tests/test_memory_report.py::ReportContractTests`). A process command line is reduced to
the executable basename for the same reason.

Collect it on the host:

```bash
curl -s -b "session=<admin-cookie>" http://127.0.0.1:<app_port>/api/system/memory | python -m json.tool
```

## The architecture as it stands (code-level, before any measurement)

Roles, from the systemd units the installer writes (`setup.sh:1351-1422`) and the
auto-sizing it applies to the web worker (`setup.sh:1324-1331`: 3 workers above ~4 GB RAM,
2 above ~2 GB, **1 on a 4 GB host**):

```
eve-manager                 gunicorn (1 worker on 4 GB) + N threads
eve-manager-background      background_worker.py -> fetcher, scheduler, jobs
eve-manager-telegram-egress telegram_egress_worker.py
eve-manager-telegram-bot    telegram_bot_worker.py
Redis, PostgreSQL, nginx, managed Xray children
```

The snapshot is the big object, and it plausibly lives in **three** Python processes:

```
Redis (compressed, one key per server)
   |
   +-- background  process_inbounds -> GLOBAL_SERVER_DATA   (the owner; publishes)
   +-- web         snapshot_reader_worker -> load_snapshot_from_redis()
   |               "Pulls the shared snapshot from Redis into local memory so requests are
   |                served fast & in-process" (panel/jobs/schedulers.py:713)
   +-- telegram bot  imports GLOBAL_SERVER_DATA (telegram_bot_worker.py:28) and calls
                     load_snapshot_from_redis() on updates (telegram_bot_worker.py:5406)
```

And a single row is heavier than it needs to be:

```
processed client
  email, id, up, down, up_formatted, down_formatted, totalGB, totalGB_formatted,
  remaining, remaining_formatted, expiry..., service_state*, comment,
  raw_client  <-- the same configuration again, nested
```

plus, on v3, the same client appearing once per assigned inbound. That is what
`snapshot.client_rows` vs `snapshot.unique_clients` (and `duplication_ratio`,
`rows_with_raw_client`, `rows_with_formatted_strings`) are for.

## Suspects, and how each one is measured

| # | Suspect | How it is measured now | What would confirm it |
|---|---|---|---|
| A | background holds the full snapshot | `eve.roles.background.pss_bytes`; its own `snapshot.*` when the endpoint is called in that process | role PSS in the hundreds of MB, flat after a full fetch |
| B | the web worker also hydrates the whole snapshot | `eve.roles.web.pss_bytes` + `snapshot.*` from a web process | web PSS close to background's |
| C | the Telegram bot hydrates it too | `eve.roles.telegram-bot.pss_bytes` | a third copy of similar size |
| D | `raw_client` duplicates config per row | `snapshot.rows_with_raw_client / client_rows` | a large share of rows carry it |
| E | v3 clients repeat per assigned inbound | `snapshot.duplication_ratio` | ratio well above 1.0 |
| F | formatted strings are cached | `snapshot.rows_with_formatted_strings` | near `client_rows` |
| G | a big `/api/refresh` spikes RSS | `eve.roles.web.peak_rss_bytes` (VmHWM) before/after a full dashboard load | peak ≫ steady after a load |

The instrumentation deliberately does not compute the true retained size of nested objects
on every request; that is the on-demand deep analysis, which is bounded, admin-only and
returns file/line/size only.

## Optimization plan (ranked, not implemented)

Measured impact can only come from the numbers above; the ordering below is by expected
value and risk, and each item states what to measure before and after.

| # | Change | Expected saving | Complexity | Risk | Migration | Performance |
|---|---|---|---|---|---|---|
| 1 | Telegram bot stops holding the full snapshot; read one service state from a small Redis index (`eve:service_state:<server>:<client>`) | its whole snapshot copy (C) | medium | low-medium (bot paths must be enumerated: 4 read sites today, `telegram_bot_worker.py:521,1111,1702,3513`) | none (additive index, backfilled by the fetcher) | bot latency improves (one key instead of a full hydrate) |
| 2 | Drop `raw_client` from the hot snapshot where an authoritative client read already exists | D, minus what the mutation path re-reads | high | medium-high (renew/edit/rotate depend on it; the targeted read path must cover legacy too) | none | mutation latency unchanged (it already re-reads on v3); dashboard read unchanged |
| 3 | Normalise v3 client entities: one entity per server, inbound membership by reference | E | high | high (touches every consumer of the snapshot shape and the delta fingerprint) | snapshot format change (versioned payload) | JSON/delta and gzip shrink proportionally |
| 4 | Stop storing `*_formatted` strings in the canonical cache; format at render | F | medium | low-medium (templates and the API consumers must format) | API shape annotation | smaller payloads, slightly more CPU in the browser |
| 5 | Web hydration made lazy/per-server instead of forever-whole-fleet | B | high | medium-high (global search and the overview need an index or a server-side query) | none | first paint equal or better; search needs the index |
| 6 | Server-side search index so search does not need every client in RAM | enables 5 | medium | medium | none | search stays fast with less memory |
| 7 | Split bootstrap so background/telegram workers do not import the whole web app | interpreter + route + cache overhead per worker | medium | medium (blueprint/cache imports move behind factories) | none | faster worker start-up |

Two things deliberately **not** in the plan: calling `gc.collect()` on hot paths (it trades
CPU for nothing when the retention is architectural), and solving pressure with swap or
zram (that hides the OOM instead of preventing it). A small swap file remains reasonable as
an OOM safety net, not as a fix.

## Runtime configuration (Phase 8 findings)

* **Web workers are already sized to the host**: 1 on a 4 GB machine
  (`setup.sh:1324-1331`). There is no evidence of worker-count bloat, and the measurement
  above will show it directly (`eve.roles.web.processes`).
* **No `max_requests` / `max_requests_jitter` is configured anywhere** (`gunicorn_config.py`
  sets worker class, threads, timeouts and logging only). Adding them is a safety net
  against slow fragmentation, not a fix for retained snapshot: a worker restart drops that
  worker's hydrated snapshot and warms it again, so on a small host it should be enabled
  *after* the trend shows growth that a restart actually reclaims. If enabled, keep the
  value high enough that restarts do not become the steady state (e.g. 800 with jitter 100)
  and watch `trend` across restarts.
* No `MemoryHigh`/`MemoryMax` should be set before the attribution exists; a limit chosen
  without numbers turns a memory problem into a restart loop.

## Acceptance

Settings → Overview → Memory is expected to answer, for the reported host:

* how much RAM the host has, how much is **available**, how much is cache, and how much is
  swap;
* Eve's own total as **PSS**, and which role uses the most;
* how many full snapshot copies exist (three roles with a snapshot section of comparable
  size = three copies) and how large each is;
* how much of a snapshot row is duplication (`duplication_ratio`,
  `rows_with_raw_client`, `rows_with_formatted_strings`);
* how much Redis holds for the compressed snapshot, and how big the out-of-snapshot caches
  are;
* whether memory is stable or growing, from a bounded trend.

Only then is a representation change worth making, and the ranking above says which one to
start with.
