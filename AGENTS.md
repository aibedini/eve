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

The project is being modularized out of the `app.py` monolith into the `panel/` package (phase 1 done: extensions, redis/phone helpers, all models).

- `panel/extensions.py` — `db` and `limiter`, constructed unbound and bound in `app.py` via `init_app`. Import `db` from here, never from `app`.
- `panel/core/` — app-independent helpers (`redis_client.py`, `phone.py`).
- `panel/models/` — all SQLAlchemy models, split by domain (`core.py`, `finance.py`, `telegram.py`, `ops.py`); `panel/models/__init__.py` re-exports every name.
- `panel/services/` — extracted services (`ownership.py`, `billing.py`, `subscription.py`, `backup.py`). `backup.py` owns the DB backup/restore, full-migration bundle, and Telegram-backup pipeline; its `TELEGRAM_BACKUP_TMP_DIR` is bound via `init_backup_tmp_dir(app)` called from `app.py` right after the re-export import.
- `panel/adapters/` — external panel adapters (`xui.py`: 3x-ui/X-UI session auth, v3 client API, inbound/status fetchers; owns `XUI_SESSION_CACHE`/`XUI_CAPABILITY_CACHE`).
- `app.py` re-exports all extracted symbols, so existing `from app import X` callers (workers, tests) keep working. Do not break this surface until the cleanup phase.
- Dependency direction is one-way: `panel.core` ← `panel.models` ← (later) services ← routes. Code inside `panel/` must never import `app` at module level; use deferred in-function imports (see `panel/models/_helpers.py`) for unavoidable reverse dependencies.
- Tests may monkeypatch `app.<name>`; a function that tests patch must stay in `app.py` (or keep its lookup in `app`'s namespace) until tests are updated in the cleanup phase.
