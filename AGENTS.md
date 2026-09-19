# Repository Guidelines

## Validation Budget (read this before running any test)

Do **not** run the whole suite after an ordinary edit. Use the changed-files → affected-tests
mapping and the tier that fits the moment:

| Tier | When | Target | What |
|------|------|--------|------|
| 1 | default after every edit | ≤ 30 s | unit tests of the changed modules, syntax/import, targeted regression. No real sleeps, no real Redis, no benchmarks. |
| 2 | before the final commit, or for a cross-cutting change | ≤ 2 min | Tier 1 plus the integration suites the change can touch (renew consistency, scheduler, snapshot/fake Redis, SSE, UI audit). |
| 3 | release / CI / nightly only | — | full suite, real-Redis multi-process harness, capacity and scale benchmarks. **Never in a normal iteration.** |

`scripts/affected_tests.py` implements the mapping (`--tier 1|2`, `--changed <paths>`,
`--list`). It runs modules sequentially in high-signal order and **stops at the first
failure** so the cheapest tier catches the break; do not paper over a red Tier 1 by moving
on.

Test-writing rules that keep Tier 1 fast and honest:

- No real sleeps for time-based logic (cadence, TTL, backoff, wake). Inject the clock
  (`now=`), drive the loop with an event the test releases, or simulate in virtual time.
- No real Redis and no network in Tier 1/2 tests: the file-backed fake is the always-on
  guard, the real-backend harness is Tier 3.
- Benchmarks are their own scripts under `scripts/`, and their numbers belong in
  `docs/performance/`; a test that measures wall-clock throughput is a Tier 3 test.
- Prefer asserting a decision (order, schedule arithmetic, counters) over a duration.

## Versioning & Releases

The project uses the `2.x.y` version scheme. `APP_VERSION` in `app.py` is the single source of truth.

- Increment `y` by one in every commit that changes code or behavior. Each such commit must include the corresponding `APP_VERSION` patch bump (for example, `2.3.2` to `2.3.3`).
- Increment `x` only when the user explicitly requests a release. For a release, increment the current minor version and reset `y` to `0` (for example, `2.3.7` to `2.4.0`).
- Never cut a release without an explicit user request. Do not create or push a tag, or create or edit a GitHub release, on your own. Ordinary commits with the required patch bump are allowed and expected.
- When the user explicitly requests a release, bump the minor version, reset the patch version, update `CHANGELOG.md` and `RELEASE_NOTES.md`, then create the tag and GitHub release.

## Upgrade Maintenance

- Use the durable `system_migrations` ledger for long-running data cleanup; never rely only on the application version.
- Make migrations idempotent and resumable. Advance their cursor atomically with every data batch, and validate converted data before deleting its source.
- Use `eve-maintenance.service` as the standard post-update runner. Preserve the in-app worker fallback for upgrades launched by an older, already-running `eve` CLI.
- Warn the user before required maintenance that it may take time and that the panel may be slower or briefly unavailable.
- Before creating an update backup, prune stale `${APP_DIR}.bak.*` directories. Retain at most two backups afterward.

## Codebase Memory Policy

This repository uses `codebase-memory-mcp` v0.10+ as its code-intelligence layer, regardless of the model or agent (Codex, Claude, Gemini, Qwen, Kimi, DeepSeek through an MCP-capable client, Copilot, Cursor, Cline, Windsurf, OpenCode, and similar tools). It answers **structure** questions — where a symbol lives, what calls it, what a change touches. It is not a source of truth: the current source and the tests are the final authority. Reasoning about **why** a decision was made (for example why an X-UI backup must be deleted after a verified Telegram send, `docs/security/BACKUP_POLICY.md`) belongs in this file or another policy document, never in the graph.

Use it for every task that needs to understand the codebase: architecture, finding an implementation, checking dependencies, tracing a flow, or a change that spans several files. It is **not** required for a small, fully localized change — a typo, one label, one colour, a version bump — where the graph costs more time and tokens than it saves.

### The workflow

1. `index_status` (after `list_projects`) to establish graph state.
2. `detect_changes` to see the current blast radius and risk before reading code.
3. If the project is missing, stale, or its coverage is partial, `index_repository` before structural exploration.
4. Discover with the graph first: `search_graph` for symbols and routes, `get_code_snippet` for exact source, `query_graph` for multi-hop questions, `trace_path` for callers/callees/data flow, `get_architecture` for broad structure, `search_code` for graph-augmented text search, and `check_index_coverage` for every material path.
5. Before editing, confirm the graph's answer against reality: read the exact symbol with `get_code_snippet` and open the related files directly. Never edit from a graph result alone.
6. After the change, run `detect_changes` again and the relevant tests, then let the watcher or `index_repository` bring the index current.

### Graph output is never committed

`index_repository` may write `.codebase-memory/artifact.json` and `.codebase-memory/graph.db.zst` into the checkout. Both are machine-local caches that record the local checkout path, and the database is a multi-megabyte binary. `.gitignore` excludes `.codebase-memory/`; every developer builds their own index (see the "Local index" section of `docs/AI_CODEBASE_MEMORY.md`). Never commit, force-add, or review-request those files.

### Evidence discipline

- Use graph tools before filesystem search. Raw `rg`, globbing, and file reads are fallbacks for string literals, error messages, configuration/non-code files, generated/vendor assets, or verified coverage gaps.
- A truncated graph result is not a complete result; check pagination. A clean coverage response means no recorded gap, not proof that every dynamic behavior is modeled.
- For negative or exhaustive claims (dead code, no callers, full impact), paginate all relevant results and state the remaining coverage limitation.
- Do not infer a complete impact surface from a filename search.
- If the MCP frontend is unavailable, use CLI mode instead of abandoning the graph: `codebase-memory-mcp cli <tool> '<json-args>'`.
- When delegating, pass the project name, index generation/freshness, qualified symbols, traces, coverage findings, and unresolved questions to the child agent.
- `graphify-out/` is legacy reference material only. Do not run Graphify for routine work unless Codebase Memory is unavailable and the fallback is explicitly noted.

## Modular Structure (`panel/` package)

The former `app.py` monolith (34.7k lines) is now modularized into the `panel/` package; `app.py` (~4.8k lines) keeps app setup, security, shared helpers, blueprint registration, and the compatibility re-export surface.

- `panel/extensions.py` — `db` and `limiter`, constructed unbound and bound in `app.py` via `init_app`. Import `db` from here, never from `app`.
- `panel/core/` — app-independent helpers (`redis_client.py` incl. `GLOBAL_SERVER_DATA`/`GLOBAL_REFRESH_LOCK`, `phone.py`).
- `panel/models/` — all SQLAlchemy models, split by domain (`core.py`, `finance.py`, `telegram.py`, `ops.py`); `panel/models/__init__.py` re-exports every name.
- `panel/services/` — business logic: `ownership.py`, `billing.py`, `subscription.py`, `backup.py` (DB/Telegram backup; `TELEGRAM_BACKUP_TMP_DIR` bound via `init_backup_tmp_dir(app)` from `app.py`), `bnqo_crypto.py` (BNQO CP Ed25519 key at `instance/bnqo_cp_key`, canonical-JSON signing, agent request-signature verification).
- `panel/adapters/` — external panel adapters (`xui.py`: 3x-ui/X-UI session auth, v3 client API, inbound/status fetchers; owns `XUI_SESSION_CACHE`/`XUI_CAPABILITY_CACHE`).
- `panel/routes/` — all routes as Flask blueprints (`auth`, `pages`, `system`, `pulse`, `bnqo`, `royalty`, `merger`, `monitor`, `dashboard`, `usage`, `clients`, `admin`, `finance`, `packages`, `receipts`, `bank_cards`, `custom_subs`, `subscription_pages`, `telegram`, `settings`, `content`, `files`, `messaging`, `templates_api`, `backups`), plus `common.py` session auth guards. `bnqo.py` implements the BNQO control plane (wire contract `docs/bnqo/EVE_API_CONTRACT.md`): agent API `/api/bnqo/agent/*` (bearer + Ed25519 request signature), admin API `/api/bnqo/*`, pages `/pulse/links*`. Endpoints are blueprint-prefixed (`pages.dashboard`, `auth.login`, ...) — keep `url_for`/`request.endpoint` references in sync when moving routes.
- `panel/jobs/` — background work: `refresh.py` (refresh/bulk-job/cached-client pipeline), `messaging.py` (WhatsApp/Telegram/SMS workers + their config-key constants), `schedulers.py` (data fetcher, snapshot reader, backup scheduler, health watchdog, usage rollup + legacy usage migration, pulse scheduler, `ensure_background_threads_started`), `bnqo.py` (BNQO link status/detection engine, incident reconciliation, 14d raw → hourly rollup, Telegram alerts; `bnqo_scheduler_worker` singleton `bnqo_scheduler`, 15 s tick).
- `panel/migrate.py` — schema migrations: one file-locked runner (`db.create_all` + legacy per-column catch-up + Alembic stamp/upgrade + idempotent seeds). Runs at app import unless `EVE_SKIP_IMPORT_MIGRATIONS=1`; the Docker entrypoint runs `python -m panel.migrate` first and sets that flag for gunicorn/background processes.
- `alembic/` — Alembic scaffolding. The baseline revision adopts existing databases (stamped, never replayed). **Every new schema change must be an Alembic revision** (`alembic revision --autogenerate`), never a runtime ALTER.
- Dependency direction is one-way: `core` <- `models` <- `services`/`adapters` <- `routes`/`jobs`. Code inside `panel/` must never import `app` at module level; use deferred in-function imports (see `panel/models/_helpers.py`) for unavoidable reverse dependencies — this also keeps `patch('app.X')` in tests working.
- `app.py` re-exports all extracted symbols, so existing `from app import X` callers (workers, tests) keep working. Migrate remaining callers to `panel.*` imports incrementally; keep the surface until tests are updated.

## UI Changes (mandatory)

For every user-facing UI change, read and follow `.agents/skills/eve-ui/SKILL.md`
(mirrored at `.dsh/skills/eve-ui/SKILL.md`) and the design-system reference in
`docs/UI_DESIGN_SYSTEM.md`. `static/style.css` and `templates/base.html` remain the
implementation source of truth. Do not introduce a parallel visual system. The skill
documents the single stylesheet
(`static/style.css`), the `:root` tokens and `html[data-theme="light"]` overrides,
the component vocabulary (`.btn*`, `.form-group`, `.form-select`,
`.checkbox-label` + `.checkmark`, `.toggle-switch` + `.slider`, `.badge` variants,
`.modal*`, `.field-note*`, `.label-note`, `.hidden`) and the rules that keep a UI
change consistent: use tokens (never a hardcoded colour), use `.hidden` instead of
`style.display`, use the project checkbox/toggle components instead of a bare
`input[type=checkbox]` inside a `.form-group`, keep inline scripts behind the CSP
`nonce`, and keep `static/style.css` valid UTF-8 (never append to it with a shell
redirect: a previous `>>` wrote ~28 KB of it as UTF-16LE and those rules stopped
applying — and a wrong byte-order decode of that block leaves stray non-ASCII
characters that silently kill the recovered rules while the file still looks valid).
`tests/test_ui_design_system.py` guards the encoding, the non-ASCII allowlist, the
brace balance, the component classes, the skill (in both locations), the design-system
document and the rule in these agent instruction files. The templates still carry
measured drift (inline `style="..."`, hardcoded colours, `style.display`, bare
checkboxes, emoji): `scripts/ui_design_audit.py` counts it per template,
`tests/ui_design_baseline.json` is the ceiling, and `python
scripts/ui_design_audit.py --check` must pass. A page may go below its baseline (rerun
the tool with `--write-baseline`), never above it.

