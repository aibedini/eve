# Repository Guidelines

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

## Codebase Knowledge Graph

This project maintains a knowledge graph under `graphify-out/`, including god nodes, community structure, and cross-file relationships.

- When `graphify-out/graph.json` exists, begin codebase questions with `graphify query "<question>"`. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts.
- Use `graphify-out/wiki/index.md` for broad navigation when it exists.
- Read `graphify-out/GRAPH_REPORT.md` only for broad architecture reviews or when query, path, and explain results are insufficient.
- After modifying code, run `graphify update .` to refresh the graph. This update is AST-only and has no API cost.

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
