---
auto_tasks: true
---

# Documentation Consistency Audit & Update Plan

## Background & Scope

This plan systematically addresses documentation inconsistencies across Agent Queue's four documentation layers:

1. **Root docs** — `README.md`, `CLAUDE.md`, `profile.md`
2. **User-facing guides** — `docs/` (index.md, getting-started.md, architecture.md, discord-commands.md, hook-pipeline.md, migrations.md, git-sync-*.md)
3. **Specs** — `specs/` (source of truth for behavior)
4. **API reference** — `docs/api/` (auto-generated from source docstrings via mkdocstrings)

### Key Issues Identified

- **ChatAgent → Supervisor rename** not reflected consistently across docs
- **9 specs missing from mkdocs.yml nav** (supervisor.md, agent-profiles.md, chat-analyzer.md, chat-observer.md, llm-logging.md, prompt-builder.md, reflection.md, rule-system.md, setup-wizard.md, tiered-tools.md)
- **19 source modules missing API docs** (supervisor, command_handler, memory, tool_registry, prompt_builder, etc.)
- **docs/specs/ is a stale copy** of specs/ — missing newer specs, has a file (adapters/development-guide.md) not in specs/
- **profile.md codebase map** lists `chat_agent.py` as core but doesn't mention supervisor.py, tool_registry.py, prompt_builder.py, or other newer modules
- **CLAUDE.md** still lists `chat_agent.py` in core files
- **State machine spec** doesn't note that transitions are advisory-only (not enforced)
- **mkdocs nav** still references `specs/chat-agent.md` under "Chat Agent" — should be "Supervisor"
- **Hook pipeline guide** references may be out of date with current hook spec

### Conventions to Follow

- Specs are source of truth → update specs first, then docs
- `docs/specs/` files should mirror `specs/` exactly (or be replaced with symlinks/includes)
- API docs use mkdocstrings `::: src.module` directives
- All source modules should have Google-style docstrings
- Mermaid diagrams for architecture/state machines
- Async-first language (use `a`-prefixed method names)

---

## Phase 1: Root Documentation & Naming Consistency

Update the three root-level docs to reflect current architecture, especially the ChatAgent → Supervisor rename.

**Files to update:**
- `CLAUDE.md` — Replace `chat_agent.py` with `supervisor.py` in core files list; add newer subsystems (tokens, memory, prompt_builder, tool_registry, rule_manager); verify all referenced files still exist
- `profile.md` — Update codebase map: add supervisor.py, tool_registry.py, prompt_builder.py, rule_manager.py, reflection.py, chat_observer.py, llm_logger.py, schedule.py; remove or note chat_agent.py as backward-compat shim; update architecture diagram if needed; update database schema table count; review design decisions section for accuracy
- `README.md` — Verify feature list matches current capabilities; update any references to ChatAgent; ensure Discord example commands still work

**Validation:** Each file should be internally consistent and cross-reference correctly.

## Phase 2: Sync docs/specs/ with specs/ and Update mkdocs.yml Nav

The `docs/specs/` directory is a partial copy of `specs/` that has drifted. Sync it and add all missing specs to the mkdocs navigation.

**Tasks:**
- Copy all specs from `specs/` to `docs/specs/` to ensure they match (or set up a build step to do this automatically — document whichever approach is chosen)
- Check if `docs/specs/adapters/development-guide.md` exists only in docs/specs — if so, move it to `specs/adapters/` as the canonical location
- Add the 9 missing specs to `mkdocs.yml` nav under appropriate sections:
  - Orchestration: supervisor.md, prompt-builder.md, reflection.md, rule-system.md, tiered-tools.md, chat-analyzer.md, chat-observer.md
  - Core: agent-profiles.md, llm-logging.md
  - Guides or Setup: setup-wizard.md
- Rename the nav entry "Chat Agent" → "Supervisor" (or "Supervisor (Chat Agent)")
- Verify all nav entries point to files that exist

## Phase 3: Add Missing API Reference Docs

Create mkdocstrings stub files for the 19 source modules missing from `docs/api/`, and add them to the mkdocs.yml nav.

**Modules needing API docs:**
- `supervisor.py`, `command_handler.py`, `memory.py`, `tool_registry.py`, `prompt_builder.py`, `prompt_manager.py`
- `rule_manager.py`, `reflection.py`, `chat_observer.py`, `llm_logger.py`, `schedule.py`
- `setup_wizard.py`, `health.py`, `file_watcher.py`, `logging_config.py`
- `agent_names.py`, `task_names.py`, `known_tools.py`, `chat_agent.py` (shim — note as deprecated alias)

**For each module:**
1. Create `docs/api/<module>.md` with standard mkdocstrings directive (`::: src.<module>`)
2. Add to `mkdocs.yml` nav under the appropriate API Reference subsection
3. Verify the source module has adequate Google-style docstrings — if a module docstring is missing or minimal, add one following the pattern established by orchestrator.py and models.py

**Validation:** Run `mkdocs build` to verify all API docs render without errors.

## Phase 4: User Guide Content Audit

Review each user-facing guide for accuracy against current specs and source code.

**Files to audit:**
- `docs/index.md` — Verify feature claims match implementation; update any screenshots if architecture has changed
- `docs/getting-started.md` — Verify setup steps still work; add mention of Supervisor (not just implicit chat); verify config file paths and example commands
- `docs/architecture.md` — Update mermaid diagram to include newer components (Supervisor, PromptBuilder, RuleManager, Reflection, ChatObserver); verify component descriptions
- `docs/discord-commands.md` — Cross-reference every command against `specs/command-handler.md` and `src/discord/commands.py`; flag any commands that are documented but not implemented (or vice versa); verify parameter names and types
- `docs/hook-pipeline.md` — Cross-reference against `specs/hooks.md`; verify all context step types, trigger types, and config fields are accurate
- `docs/migrations.md` — Check which migrations have been completed and remove them; verify remaining migration plan is accurate against current `database.py`
- `docs/git-sync-current-state.md` and `docs/git-sync-gaps.md` — Verify which gaps have been resolved; update status of each gap

**For each file:** Document what changed and why in the commit message.

## Phase 5: Spec Internal Consistency Pass

Review each spec for internal consistency and accuracy against current implementation.

**Priority specs to audit:**
- `specs/chat-agent.md` — Verify it properly redirects to supervisor.md; consider whether it should be removed entirely or kept as a deprecation notice
- `specs/supervisor.md` — Verify it accurately describes the current Supervisor class behavior, tool-use loop, activation modes
- `specs/models-and-state-machine.md` — Add note that state machine transitions are advisory/logged, not enforced; verify all enum values match source; verify all dataclass fields match source
- `specs/command-handler.md` — Verify all command signatures match implementation; check plan approval workflow documentation
- `specs/orchestrator.md` — Verify task lifecycle, plan parsing, workspace preparation steps match current code
- `specs/database.md` — Verify table count, schema, and migration list match current `database.py`

**For newer specs** (agent-profiles, chat-analyzer, chat-observer, llm-logging, prompt-builder, reflection, rule-system, setup-wizard, tiered-tools):
- Verify each spec has a corresponding implementation in `src/`
- If the spec describes unimplemented features, mark those sections clearly as "Planned" or "Not yet implemented"

## Phase 6: Source Docstring Standardization

Ensure all source modules have consistent, comprehensive docstrings that generate good API docs.

**Standards (based on existing good examples in orchestrator.py, models.py, database.py):**
- Module-level docstring: 1-line summary, blank line, description of purpose/responsibilities, key design decisions, reference to spec file
- Class-level docstring: purpose, key attributes, usage pattern
- Method-level docstring: Google-style with Args/Returns/Raises sections for public methods
- All async methods should note they are coroutines

**Modules most likely to need docstring improvements** (based on being newer or missing API docs):
- `supervisor.py`, `tool_registry.py`, `prompt_builder.py`, `prompt_manager.py`
- `rule_manager.py`, `reflection.py`, `chat_observer.py`, `llm_logger.py`
- `memory.py`, `schedule.py`, `setup_wizard.py`
- `health.py`, `file_watcher.py`, `command_handler.py`

**Validation:** Run `mkdocs build` after changes to verify docstrings render correctly.
