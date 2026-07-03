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

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
