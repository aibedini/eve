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
