---
auto_tasks: true
---

# Documentation Consistency Audit, Deprecation Cleanup & Update Plan

## Background & Scope

This plan systematically addresses documentation inconsistencies across Agent Queue's four documentation layers, **removes deprecated code and specs**, and ensures specs describe the current codebase (not historical artifacts).

### Documentation Layers

1. **Root docs** — `README.md`, `CLAUDE.md`, `profile.md`
2. **User-facing guides** — `docs/` (index.md, getting-started.md, architecture.md, discord-commands.md, hook-pipeline.md, migrations.md, git-sync-*.md)
3. **Specs** — `specs/` (source of truth for behavior)
4. **API reference** — `docs/api/` (auto-generated from source docstrings via mkdocstrings)

### Deprecated Items Inventory

The following are deprecated and should be **removed** (not just marked):

#### Code to Remove

| Item | Location | Replacement | Notes |
|------|----------|-------------|-------|
| `chat_agent.py` shim | `src/chat_agent.py` | `src/supervisor.py` | Backward-compat re-export of `Supervisor as ChatAgent`. Nothing in production imports from `chat_agent.py` that can't import from `supervisor.py` directly. |
| `ChatAnalyzerConfig` | `src/config.py` | `supervisor.observation` config | Full class with validation — emits deprecation warning. Remove the class, the `chat_analyzer` field on `AppConfig`, its validation, its reload, and its presence in `KNOWN_CONFIG_SECTIONS`. |
| `ChatAnalyzerSuggestion` model | `src/models.py` | None (observation mode uses different model) | Dataclass for the old analyzer's suggestion table. |
| Deprecated analyzer commands | `src/command_handler.py` | None | `analyzer_status()`, `analyzer_toggle()`, `analyzer_history()` — all return deprecation messages. Remove the methods and their registration. |
| Deprecated analyzer tools | `src/tool_registry.py` | None | `analyzer_status`, `analyzer_toggle`, `analyzer_history` tool definitions (commented out / marked deprecated Phase 6). |
| `chat_analyzer_suggestions` DB table | `src/database.py` | None | If schema still defines it, add a migration to drop it. |
| Legacy `_discover_and_store_plan` | `src/orchestrator.py` | `_phase_plan_discover` via Supervisor | The "12a-legacy" fallback path. All production uses go through Supervisor now. |
| Legacy `_phase_plan_generate` | `src/orchestrator.py` | Supervisor-based plan discovery | Old single-step plan generation. |
| `discord_control_channel_id` column | `src/database.py` | `discord_channel_id` | Legacy column with fallback logic in `_row_to_project`. If any rows still use it, migrate them to `discord_channel_id` then drop the column. |
| `workspace_path` on projects table | `src/database.py` | `workspaces` table | Deprecated/unused column per spec. Remove from schema and `_row_to_project`. |
| Git sync methods | `src/git/manager.py` | Async `a`-prefixed methods | Synchronous API retained "for backward compat and tests only." Update tests to use async, then remove sync wrappers. |

#### Specs to Remove

| Spec | Reason |
|------|--------|
| `specs/chat-agent.md` | Fully replaced by `specs/supervisor.md`. Currently a 389-line deprecated spec kept "for historical reference" — but it actively misleads since it describes the old architecture in detail. Delete it. |
| `specs/chat-analyzer.md` | Replaced by `specs/chat-observer.md`. Code is already removed/commented out. |

#### Spec Sections to Remove/Rewrite

| Spec | Section | Action |
|------|---------|--------|
| `specs/supervisor.md` §Backward Compatibility | Lines about `chat_agent.py` re-export | Remove after `chat_agent.py` is deleted |
| `specs/tiered-tools.md` | References to `src/chat_agent.py` | Update to reference `src/supervisor.py` |
| `specs/hooks.md` §LLM Invocation | References to `ChatAgent` | Rewrite to reference `Supervisor` |
| `specs/llm-logging.md` §5.2 | "ChatAgent (`src/chat_agent.py`)" section | Rewrite as "Supervisor (`src/supervisor.py`)" |
| `specs/chat-providers/providers.md` | References to `ChatAgent` | Update to `Supervisor` |
| `specs/command-handler.md` | References to `ChatAgent` | Update to `Supervisor` |
| `specs/discord/discord.md` | `ChatAgent` constructor references | Update to `Supervisor` |
| `specs/database.md` | `workspace_path` and `discord_control_channel_id` docs | Remove deprecated column documentation after columns are dropped |
| `specs/database.md` | Legacy migration entries | Remove entries for migrations that add now-dropped columns |
| `specs/orchestrator.md` §12a-legacy | Legacy plan discovery fallback | Remove entire section after code is removed |
| `specs/config.md` | `chat_analyzer` config section | Remove after code cleanup |

### Conventions to Follow

- Specs are source of truth → update specs first, then docs
- `docs/specs/` files should mirror `specs/` exactly (or be replaced with symlinks/includes)
- API docs use mkdocstrings `::: src.module` directives
- All source modules should have Google-style docstrings
- Mermaid diagrams for architecture/state machines
- Async-first language (use `a`-prefixed method names)

---

## Phase 1: Remove Deprecated Code — ChatAgent & ChatAnalyzer

Remove all deprecated code related to the ChatAgent→Supervisor rename and the ChatAnalyzer→ChatObserver replacement.

**Removals:**
- Delete `src/chat_agent.py` entirely
- Update all imports that reference `chat_agent` to import from `supervisor` directly:
  - `src/hooks.py` — update `ChatAgent` import to `Supervisor`
  - `src/chat_providers/__init__.py` — update docstring references
  - `src/chat_providers/types.py` — update docstring references
  - Any test files that import from `chat_agent`
- Remove `ChatAnalyzerConfig` class from `src/config.py` and the `chat_analyzer` field from `AppConfig`
- Remove `chat_analyzer` from `KNOWN_CONFIG_SECTIONS` in `src/config.py`
- Remove `check_deprecations()` method (or just the chat_analyzer warning from it)
- Remove deprecated analyzer commands from `src/command_handler.py`: `analyzer_status()`, `analyzer_toggle()`, `analyzer_history()`
- Remove deprecated analyzer tool definitions from `src/tool_registry.py`
- Remove `ChatAnalyzerSuggestion` dataclass from `src/models.py`
- Add a database migration to drop `chat_analyzer_suggestions` table if it exists
- In `src/supervisor.py`: remove backward-compat comments about `chat_agent.py`, clean up `SYSTEM_PROMPT_TEMPLATE` stub

**Validation:** `pytest tests/` passes. `ruff check src/` clean. No remaining imports of `chat_agent`.

## Phase 2: Remove Deprecated Database Columns & Legacy Orchestrator Code

**Database cleanup:**
- Remove `discord_control_channel_id` from the projects schema in `src/database.py`
- Add migration to copy any non-null `discord_control_channel_id` values to `discord_channel_id`, then drop the column
- Remove `workspace_path` from the projects schema
- Remove the fallback logic in `_row_to_project` that reads `discord_control_channel_id`
- Remove the `workspace_path` ignore in `_row_to_project`
- Remove legacy migration entries that add these now-dropped columns (or mark them as no-ops)

**Orchestrator cleanup:**
- Remove `_discover_and_store_plan` (the legacy 12a fallback) from `src/orchestrator.py`
- Remove `_phase_plan_generate` if it still exists
- Remove the "no supervisor available, using legacy plan discovery" fallback path
- Update the plan discovery flow to require Supervisor (it's always present now)

**Git manager consideration:**
- Audit sync methods in `src/git/manager.py` — if tests have been migrated to async, remove sync wrappers. If not, note this as a future cleanup (don't block on test migration in this phase).

**Validation:** `pytest tests/` passes. Database migrations run cleanly.

## Phase 3: Delete Deprecated Specs & Update All Specs for Current Code

**Delete:**
- `specs/chat-agent.md` — fully replaced by `specs/supervisor.md`
- `specs/chat-analyzer.md` — fully replaced by `specs/chat-observer.md`

**Rewrite `specs/supervisor.md`:**
- Remove all "Backward Compatibility" section referencing `chat_agent.py`
- Expand the spec to be comprehensive (the current spec is only ~80 lines vs the old chat-agent.md's 389 lines). It should cover:
  - Full tool-use loop behavior (currently only mentioned briefly)
  - System prompt construction (reference to prompt-builder but document the flow)
  - History compaction interface
  - Active project handling
  - All three activation modes in detail
  - Initialization and provider setup
  - Streaming behavior (or lack thereof)

**Update specs that referenced ChatAgent:**
- `specs/hooks.md` — Replace all `ChatAgent`/`chat_agent` references with `Supervisor`/`supervisor`
- `specs/llm-logging.md` — Rewrite §5.2 as "Supervisor" section; update caller strings
- `specs/chat-providers/providers.md` — Replace `ChatAgent` with `Supervisor` throughout
- `specs/command-handler.md` — Replace `ChatAgent` references with `Supervisor`
- `specs/discord/discord.md` — Replace `ChatAgent` constructor/initialization references with `Supervisor`
- `specs/tiered-tools.md` — Replace `src/chat_agent.py` references with `src/supervisor.py`
- `specs/config.md` — Remove `chat_analyzer` config documentation
- `specs/database.md` — Remove `workspace_path` and `discord_control_channel_id` documentation; update table count; remove legacy migration entries for dropped columns; remove `chat_analyzer_suggestions` table docs
- `specs/orchestrator.md` — Remove §12a-legacy entirely; update §12a to be the sole plan discovery path (via Supervisor, no fallback)

**Validation:** Every spec file references only current code. No mentions of `ChatAgent` (except in git history). `grep -r "ChatAgent\|chat_agent\|chat-agent\|chat_analyzer\|ChatAnalyzer\|chat-analyzer" specs/` returns zero results.

## Phase 4: Root Documentation & Naming Consistency

Update the three root-level docs to reflect the cleaned-up architecture.

**Files to update:**
- `CLAUDE.md` — Replace `chat_agent.py` with `supervisor.py` in core files list; add newer subsystems (tokens, memory, prompt_builder, tool_registry, rule_manager); verify all referenced files still exist; remove any ChatAnalyzer mentions
- `profile.md` — Update codebase map: add supervisor.py, tool_registry.py, prompt_builder.py, rule_manager.py, reflection.py, chat_observer.py, llm_logger.py, schedule.py; remove chat_agent.py entirely (no longer exists); update architecture diagram; update database schema table count; review design decisions section
- `README.md` — Verify feature list matches current capabilities; remove any references to ChatAgent or ChatAnalyzer; ensure Discord example commands still work

**Validation:** Each file is internally consistent and references only files that exist.

## Phase 5: Sync docs/specs/ with specs/ and Update mkdocs.yml Nav

The `docs/specs/` directory is a partial copy of `specs/` that has drifted. Sync it after the spec cleanup.

**Tasks:**
- Copy all specs from `specs/` to `docs/specs/` to ensure they match (or set up a build step to do this automatically)
- Remove `docs/specs/chat-agent.md` and `docs/specs/chat-analyzer.md` (they were deleted from `specs/` in Phase 3)
- Check if `docs/specs/adapters/development-guide.md` exists only in docs/specs — if so, move it to `specs/adapters/` as the canonical location
- Add the missing specs to `mkdocs.yml` nav: supervisor.md, agent-profiles.md, chat-observer.md, llm-logging.md, prompt-builder.md, reflection.md, rule-system.md, setup-wizard.md, tiered-tools.md
- Remove the nav entry for "Chat Agent" — replace with "Supervisor"
- Remove any nav entry for "Chat Analyzer"
- Verify all nav entries point to files that exist

## Phase 6: User Guide Content Audit

Review each user-facing guide for accuracy against current specs and source code.

**Files to audit:**
- `docs/index.md` — Verify feature claims match implementation; remove any ChatAgent/ChatAnalyzer references
- `docs/getting-started.md` — Verify setup steps still work; verify config file paths and example commands
- `docs/architecture.md` — Update mermaid diagram to include Supervisor, PromptBuilder, RuleManager, Reflection, ChatObserver; remove ChatAgent/ChatAnalyzer from diagrams
- `docs/discord-commands.md` — Cross-reference every command against `specs/command-handler.md` and `src/discord/commands.py`; remove deprecated analyzer commands
- `docs/hook-pipeline.md` — Cross-reference against `specs/hooks.md`; replace ChatAgent references with Supervisor
- `docs/migrations.md` — Check which migrations have been completed and remove them; verify remaining migration plan
- `docs/git-sync-current-state.md` and `docs/git-sync-gaps.md` — Verify which gaps have been resolved; update status

## Phase 7: Add Missing API Reference Docs & Standardize Docstrings

Create mkdocstrings stub files for source modules missing from `docs/api/`, and ensure all modules have good docstrings.

**Modules needing API docs:**
- `supervisor.py`, `command_handler.py`, `memory.py`, `tool_registry.py`, `prompt_builder.py`, `prompt_manager.py`
- `rule_manager.py`, `reflection.py`, `chat_observer.py`, `llm_logger.py`, `schedule.py`
- `setup_wizard.py`, `health.py`, `file_watcher.py`, `logging_config.py`
- `agent_names.py`, `task_names.py`, `known_tools.py`
- Do NOT create an API doc for `chat_agent.py` (deleted in Phase 1)

**For each module:**
1. Create `docs/api/<module>.md` with standard mkdocstrings directive (`::: src.<module>`)
2. Add to `mkdocs.yml` nav under the appropriate API Reference subsection
3. Verify the source module has adequate Google-style docstrings — if a module docstring is missing or minimal, add one

**Docstring standards (based on orchestrator.py, models.py, database.py):**
- Module-level: 1-line summary, blank line, purpose/responsibilities, key design decisions, reference to spec
- Class-level: purpose, key attributes, usage pattern
- Method-level: Google-style with Args/Returns/Raises for public methods
- All async methods should note they are coroutines

**Validation:** Run `mkdocs build` to verify all API docs render without errors.
