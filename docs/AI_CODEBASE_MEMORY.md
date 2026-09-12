# Codebase Memory workflow for all coding agents

This file is the model-neutral contract for working on this repository. The same rules apply whether the reasoning model is Codex, Claude, Gemini, Qwen, Kimi, DeepSeek, or another model. The client hosting that model must expose `codebase-memory-mcp` v0.10+ through MCP or CLI.

## Objective

Use the persistent code knowledge graph to reduce repeated source loading and token use without weakening correctness. Graph results guide discovery and impact analysis; exact source inspection, coverage checks, tests, and engineering judgment still decide the change.

## Required workflow

1. Establish graph state with `list_projects` and `index_status`. Run `index_repository` if this repository is missing or stale.
2. Choose the smallest evidence tier that fits the task:
   - Scout: quick positive lookup; never use it for exhaustive or negative claims.
   - Verify: default for implementation and diagnosis; query the relevant symbols, traces, exact snippets, and coverage.
   - Auditor: reviews, refactors, security-sensitive work, and exhaustive claims; use a current generation, complete pagination, both trace directions where material, and explicit limitations.
3. Discover with `search_graph`; use `search_code` when the need is textual but still within indexed code.
4. Trace behavior and impact with `trace_path` (`calls`, `data_flow`, or `cross_service`). Use `query_graph` for relationships that need multi-hop Cypher queries.
5. Read only the required implementation with `get_code_snippet`. Use `get_architecture` for broad system orientation instead of opening many files.
6. Call `check_index_coverage` for all material source paths. Fall back to targeted file reads or `rg` only for reported gaps, literals/error strings, configs, documentation, generated code, or vendor assets.
7. Make the change and run focused tests. Afterward call `detect_changes` to inspect affected symbols and risk, then ensure the graph is current via its watcher or `index_repository`.

## Tool priority

1. `search_graph`
2. `trace_path`
3. `get_code_snippet`
4. `query_graph`
5. `get_architecture`
6. `search_code`
7. Targeted source read or `rg` only when justified above

Always check pagination metadata. A truncated graph result is not a complete result. A clean coverage response means no recorded gap; it is not proof that every dynamic behavior is modeled.

## Client setup

Install the server, then let it configure the clients it knows:

```bash
npm install -g codebase-memory-mcp   # downloads a verified native runtime set
codebase-memory-mcp install          # configures every detected client surface
```

Pin 0.10.8 or newer: 0.10.7 is deprecated because its postinstall fetches a release tag that does not exist.

`install` knows 43 client surfaces but **not DeepSeek Harness yet**, so DSH is wired by hand. Add the MCP client row to the home-level patch layer `$DSH_HOME/cordis.patch.yml`, which applies to every DSH profile (`web`, `headless`, `acp`, `sdk`); put it in one profile's `cordis.patch.yml` instead to scope it there:

```yaml
- insert:
    - id: mcp-codebase-memory
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: codebase-memory
        transport: stdio
        command: codebase-memory-mcp   # Windows: the absolute path to the .cmd shim
        args: []
        toolCallTimeoutMs: 600000      # a full index_repository runs for minutes
```

A profile with `patchReload: live` picks the row up with no restart, and the tools register as `mcp__codebase-memory__<tool>`. DSH 0.1.5-rc.1 has no MCP settings page — Settings → Plugins configures only the host-plane cards — so the patch file is the supported surface. On Windows the npm shim is a `.cmd`, which the bundled MCP client resolves through `cross-spawn`; the absolute path avoids depending on the DSH process `PATH`.

The project identifier is derived from the absolute checkout path with the separators folded to dashes. Run `list_projects` first and use the name it reports; the examples below write it as `<project>` because the name is machine-local.

## Local index (never committed)

The graph is a per-machine cache, not a repository artifact:

* The index lives in the tool's own cache directory (`~/.cache/codebase-memory-mcp/<project>.db` on Linux/macOS, the equivalent under `%USERPROFILE%` on Windows), keyed by the checkout path.
* `index_repository` may also write a compressed copy plus an `artifact.json` into the repository's `.codebase-memory/` directory. Both name the machine-local checkout path in their `project` field and the database is a multi-megabyte binary, so **that directory is ignored by `.gitignore` and must never be committed**.
* Every developer builds their own index on first use:
  ```text
  codebase-memory-mcp cli index_repository '{"repo_path":"<absolute path to your checkout>"}'
  ```
* A fresh clone therefore starts with no graph. That is expected: run `list_projects`, and if the project is absent or stale, run `index_repository` before the first structural query.
* Sharing an index between machines is out of band (copy the compressed artifact), never a Git commit.

The graph holds structure — where a symbol lives and what it calls. It does not hold project decisions. Why a backup file must be deleted after a verified Telegram send belongs in a policy document such as `docs/security/BACKUP_POLICY.md`, not in the graph.

## CLI fallback

If a client cannot expose MCP tools, keep the same workflow through the installed CLI:

```text
codebase-memory-mcp cli list_projects '{}'
codebase-memory-mcp cli index_status '{"project":"<project>"}'
codebase-memory-mcp cli search_graph '{"project":"<project>","name_pattern":".*Handler.*"}'
```

Do not silently fall back to broad recursive file loading. If Codebase Memory is unavailable, state that limitation and keep fallback reads narrowly scoped.

## Multi-agent handoff

Before delegating code work, the parent agent must pass the evidence tier, exact project identifier, index generation/freshness, qualified symbols, traces already performed, pagination state, coverage gaps, exact source paths already checked, and unresolved questions. A child without MCP access must not claim graph access; it works from the supplied graph evidence and verifies only the scoped source gaps.

## Legacy graph

`graphify-out/` remains historical reference material. Codebase Memory is the default and mandatory primary system. Use Graphify only when Codebase Memory is unavailable, and record that fallback in the task result.
