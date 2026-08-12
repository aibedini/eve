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

## CLI fallback

If a client cannot expose MCP tools, keep the same workflow through the installed CLI:

```text
codebase-memory-mcp cli list_projects '{}'
codebase-memory-mcp cli index_status '{"project":"C-Users-Mahna-Documents-Github-Repos-eve-xui-manager"}'
codebase-memory-mcp cli search_graph '{"project":"C-Users-Mahna-Documents-Github-Repos-eve-xui-manager","name_pattern":".*Example.*"}'
```

Do not silently fall back to broad recursive file loading. If Codebase Memory is unavailable, state that limitation and keep fallback reads narrowly scoped.

## Multi-agent handoff

Before delegating code work, the parent agent must pass the evidence tier, exact project identifier, index generation/freshness, qualified symbols, traces already performed, pagination state, coverage gaps, exact source paths already checked, and unresolved questions. A child without MCP access must not claim graph access; it works from the supplied graph evidence and verifies only the scoped source gaps.

## Legacy graph

`graphify-out/` remains historical reference material. Codebase Memory is the default and mandatory primary system. Use Graphify only when Codebase Memory is unavailable, and record that fallback in the task result.
