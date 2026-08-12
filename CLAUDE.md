## Versioning & releases

Version scheme: `2.x.y` (single source of truth: `APP_VERSION` in `app.py`).

- **`y` (patch)**: bump by 1 on **every commit** that changes code/behavior. So each commit raises `APP_VERSION` (e.g. `2.3.2` → `2.3.3`).
- **`x` (minor)**: bump by 1 **only when the user explicitly asks for a release**. On release, look at the current version, increase `x` by one, and reset `y` to `0` (e.g. `2.3.7` → `2.4.0`).
- **Do NOT cut a release until the user explicitly says so.** Never create a git tag, push a tag, or create/edit a GitHub release on your own. Committing (with the `y` bump) is fine and expected; releasing is not.
- When the user does ask to release: bump `x`, reset `y`, update CHANGELOG.md and RELEASE_NOTES.md, then create the tag and GitHub release.

## Upgrade maintenance

- Long-running data cleanup must use the durable `system_migrations` ledger; never rely only on the app version.
- Migrations must be idempotent and resumable, advance their cursor atomically with each data batch, and validate converted data before deleting the source.
- `eve-maintenance.service` is the standard post-update runner. Keep the in-app worker fallback for upgrades launched by an older in-memory `eve` CLI.
- Show a warning before required maintenance: it may take time and the panel can be slower or briefly unavailable.
- Prune stale `${APP_DIR}.bak.*` directories before creating the next update backup, and retain at most two afterward.

## Codebase Memory

The mandatory repository-wide code-intelligence policy is in `AGENTS.md` and `docs/AI_CODEBASE_MEMORY.md`. For every coding task, use `codebase-memory-mcp` v0.10+ first: verify/index the project, discover and trace through graph tools, check coverage before relying on results, and run `detect_changes` after edits. Use raw grep/read only for non-code content, literals, or verified coverage gaps. `graphify-out/` is legacy fallback material, not the routine workflow.

## Modular structure

The code lives in the `panel/` package (models, services, adapters, routes as blueprints, jobs, migrate); `app.py` is the thin app core plus a compatibility re-export surface. Schema changes go through Alembic revisions (`alembic/`); migrations run via the single file-locked `panel.migrate` runner. See `AGENTS.md` → "Modular Structure" for the full conventions (dependency direction, deferred imports, blueprint endpoints).
