# Repository coding-agent instructions

Follow `AGENTS.md` and `docs/AI_CODEBASE_MEMORY.md`. Use `codebase-memory-mcp` as the mandatory first-line discovery, tracing, coverage, and impact-analysis system for every coding task. Use raw filesystem search only for the documented fallback cases.
For every user-facing UI change, read and follow `.agents/skills/eve-ui/SKILL.md` (mirrored at `.dsh/skills/eve-ui/SKILL.md`) and `docs/UI_DESIGN_SYSTEM.md`. `static/style.css` and `templates/base.html` remain the implementation source of truth. Do not introduce a parallel visual system.
