# Infrastructure & Observability — Implementation Plan

This plan covers all items from the Infrastructure & Observability notes, organized
into actionable implementation phases. Each phase is scoped to be a single task.

## Background & Architecture

The agent-queue system is a Python asyncio daemon that orchestrates AI coding agents
via Discord. Key architectural facts relevant to this plan:

- **Config**: `src/config.py` — YAML-based config with `AppConfig` dataclass tree,
  loaded once at startup via `load_config()`. No validation beyond env-var resolution.
- **Startup**: `src/main.py` — calls `load_config()`, creates `Orchestrator`, starts
  Discord bot, runs scheduler loop.
- **Orchestrator**: `src/orchestrator.py` — central ~5s loop, deterministic scheduling,
  no LLM calls for coordination.
- **State machine**: `src/state_machine.py` + `src/models.py` — `TaskStatus` enum with
  `VALID_TASK_TRANSITIONS` dict mapping `(status, event) -> status`.
- **Logging**: Uses stdlib `logging.getLogger(__name__)` in ~5 modules. No structured
  logging, no correlation IDs.
- **LLM Logger**: `src/llm_logger.py` — JSONL append logger for ChatProvider and agent
  sessions. Already has retention cleanup.
- **Adapters**: `src/adapters/base.py` (ABC), `src/adapters/claude.py` (Claude Code).
  Minimal interface: start/wait/stop/is_alive.
- **Hooks**: `src/hooks.py` — event-driven + periodic hook engine with context pipeline
  and LLM invocation.
- **Docs**: `docs/` (MkDocs), `specs/` (detailed specs per module).

---

## Phase 1: Config validation on startup

**Goal**: Fail fast on misconfiguration by validating the loaded `AppConfig` before
the orchestrator or Discord bot are initialized.

**Files to modify**:
- `src/config.py` — Add a `validate_config(config: AppConfig) -> list[str]` function
- `src/main.py` — Call `validate_config()` after `load_config()`, exit on errors

**Implementation approach**:
1. Add `validate_config()` that checks:
   - `discord.bot_token` is non-empty
   - `discord.guild_id` is non-empty
   - `database_path` parent directory is writable
   - `workspace_dir` exists or is creatable
   - `agents_config.heartbeat_interval_seconds > 0`
   - `scheduling.rolling_window_hours > 0`
   - `pause_retry` values are positive
   - `archive.after_hours > 0`
   - `auto_task.max_plan_depth >= 1` and `max_steps_per_plan >= 1`
   - `memory` config: if enabled, embedding_provider is valid
   - `rate_limits` keys match expected format
   - Agent profile IDs are unique
2. Return list of error strings; empty = valid
3. In `main.py`, log errors and `sys.exit(1)` if validation fails
4. Add unit tests in `tests/test_config_validation.py`

**Dependencies**: None
**Complexity**: Low

---

## Phase 2: Config hot-reloading for non-critical settings

**Goal**: Allow changing non-critical config values without restarting the daemon.
Non-critical = settings that don't require re-establishing connections (e.g.,
scheduling params, timeouts, archive settings, monitoring thresholds).

**Files to modify**:
- `src/config.py` — Add `reload_config()` and `HOT_RELOADABLE_SECTIONS` constant
- `src/orchestrator.py` — Add `reload_config()` method, wire to scheduler loop
- `src/discord/commands.py` — Add `/reload-config` command

**Implementation approach**:
1. Define `HOT_RELOADABLE_SECTIONS` — sections safe to reload without reconnection:
   - `scheduling`, `pause_retry`, `monitoring`, `archive`, `auto_task`,
     `agents_config` (timeouts only), `llm_logging`, `hook_engine`
2. Add `reload_config(path, current_config) -> AppConfig` that loads fresh YAML,
   validates it, then selectively replaces only hot-reloadable sections
3. Orchestrator gets `async def reload_config()` method called via command or
   periodic check (e.g., file mtime watch)
4. Add `/reload-config` Discord command (admin-only) that triggers reload and
   reports which sections changed
5. Add tests verifying only safe sections are updated

**Dependencies**: Phase 1 (validation is needed before accepting reloaded config)
**Complexity**: Medium

---

## Phase 3: Environment-specific config profiles

**Goal**: Support dev/staging/production profiles so the same config file can have
environment-specific overrides.

**Files to modify**:
- `src/config.py` — Add profile merging logic to `load_config()`
- `src/main.py` — Accept `--profile` CLI arg or `AGENT_QUEUE_PROFILE` env var

**Implementation approach**:
1. Support a `profiles:` top-level key in config.yaml:
   ```yaml
   workspace_dir: ~/agent-queue-workspaces
   discord:
     bot_token: ${DISCORD_TOKEN}
   profiles:
     dev:
       llm_logging:
         enabled: true
       monitoring:
         stuck_task_threshold_seconds: 300
     production:
       archive:
         after_hours: 48
   ```
2. In `load_config()`, after loading base config, deep-merge the selected profile
3. Profile is selected via `AGENT_QUEUE_PROFILE` env var or `--profile` CLI arg
4. Add `_deep_merge(base: dict, overlay: dict) -> dict` utility
5. Add tests for profile merging and precedence

**Dependencies**: Phase 1 (validation applies to merged config)
**Complexity**: Low-Medium

---

## Phase 4: Structured logging with correlation IDs

**Goal**: Replace ad-hoc `logging.getLogger()` calls with structured JSON logging.
Add a correlation ID (task_id) that flows through all log entries for a given task
execution.

**Files to modify**:
- `src/logging_config.py` (new) — Structured logging setup with JSON formatter
- `src/main.py` — Initialize structured logging at startup
- `src/orchestrator.py` — Pass task_id context through execution flow
- `src/adapters/claude.py` — Include task_id in log context
- `src/hooks.py` — Include hook_id/run_id in log context
- `src/database.py` — Include query context in log entries
- All files using `logging.getLogger()` — minimal changes

**Implementation approach**:
1. Create `src/logging_config.py`:
   - JSON formatter that outputs `{"timestamp", "level", "logger", "message",
     "task_id", "project_id", "agent_id", ...}`
   - `setup_logging(level, json_output=True)` function
   - Context variable using `contextvars.ContextVar` for task_id correlation
2. Add `TaskCorrelation` context manager that sets `task_id`, `project_id` in
   the contextvar and a custom `logging.Filter` that injects them into records
3. Wrap orchestrator's `_execute_task()` and `_launch_task_execution()` with
   TaskCorrelation context
4. Update `src/main.py` to call `setup_logging()` before anything else
5. Add config option `logging: {format: "json"|"text", level: "INFO"}`
6. Add tests verifying correlation IDs propagate correctly

**Dependencies**: None (can be done independently)
**Complexity**: Medium

---

## Phase 5: Health check endpoint

**Goal**: Expose an HTTP health check endpoint for external monitoring (e.g.,
uptime monitors, container orchestrators, systemd watchdog).

**Files to modify**:
- `src/health.py` (new) — aiohttp-based health server
- `src/orchestrator.py` — Expose health state (last cycle time, agent counts, etc.)
- `src/main.py` — Start health server alongside bot
- `src/config.py` — Add `health_check` config section
- `pyproject.toml` — Add `aiohttp` dependency (if not present)

**Implementation approach**:
1. Create `src/health.py` with a minimal `aiohttp` server:
   - `GET /health` → 200 if orchestrator ran a cycle in the last 30s, 503 otherwise
   - `GET /health/detailed` → JSON with:
     - `orchestrator_ok`: bool (last cycle < 30s ago)
     - `discord_connected`: bool (bot gateway is connected)
     - `database_ok`: bool (last DB query succeeded)
     - `active_agents`: int
     - `pending_tasks`: int
     - `uptime_seconds`: float
     - `last_cycle_at`: ISO timestamp
2. Add `HealthCheckConfig` dataclass: `enabled: bool, port: int = 8080, bind: str = "127.0.0.1"`
3. Orchestrator exposes `get_health_state() -> dict` method
4. Health server queries orchestrator state on each request (no background polling)
5. Start/stop health server in `main.py` alongside bot lifecycle
6. Add tests with mock aiohttp client

**Dependencies**: None
**Complexity**: Medium

---

## Phase 6: Improve LLM interaction logging

**Goal**: Enhance the existing `LLMLogger` to capture more actionable data for
prompt optimization, including token counts, cost estimates, prompt templates,
and aggregated statistics.

**Files to modify**:
- `src/llm_logger.py` — Extend logging fields and add analytics methods
- `src/chat_providers/anthropic_provider.py` — Pass token usage from API response
- `src/chat_providers/ollama_provider.py` — Pass token usage if available
- `src/adapters/claude.py` — Log more detailed session metadata
- `src/discord/commands.py` — Add `/llm-stats` command for quick insights

**Implementation approach**:
1. Extend `log_chat_provider_call()` to capture:
   - `input_tokens`, `output_tokens` from API response usage field
   - `cost_estimate_usd` computed from model pricing table
   - `prompt_template_name` (caller-provided identifier for the prompt pattern)
   - `cache_read_tokens`, `cache_creation_tokens` (Anthropic prompt caching)
2. Extend `log_agent_session()` to capture:
   - `tool_uses_count`, `conversation_turns` from session output
   - `retry_count` and whether it was a continuation
3. Add `get_stats(days=7) -> dict` method that reads JSONL files and computes:
   - Total/avg tokens per caller, per model
   - Cost breakdown by caller
   - Top prompt templates by token usage
   - Success/failure rates
4. Add `/llm-stats` Discord command that calls `get_stats()` and formats results
5. Add tests for new logging fields and stats computation

**Dependencies**: None
**Complexity**: Medium

---

## Phase 7: Document task state machine with transition diagrams

**Goal**: Create comprehensive documentation of the task state machine including
a Mermaid diagram, transition table, and behavioral descriptions.

**Files to modify**:
- `docs/specs/models-and-state-machine.md` — Enhance existing spec
- `docs/architecture.md` — Add state machine overview section

**Implementation approach**:
1. Generate a Mermaid state diagram from `VALID_TASK_TRANSITIONS` in
   `src/state_machine.py` — auto-generate or hand-craft for readability
2. Add to `docs/specs/models-and-state-machine.md`:
   - Mermaid diagram showing all states and transitions
   - Table: `From State | Event | To State | Description`
   - Behavioral notes for each state (what the orchestrator does in each state)
   - Common paths: happy path, retry path, pause/resume path, admin override path
3. Add a simplified diagram to `docs/architecture.md` showing the happy path
4. Include notes about which transitions are enforced vs. advisory

**Dependencies**: None
**Complexity**: Low

---

## Phase 8: Write adapter development guide

**Goal**: Document how to add a new agent adapter so third-party agent backends
can be integrated.

**Files to modify**:
- `docs/specs/adapters/development-guide.md` (new) — Full adapter development guide
- `docs/specs/adapters/claude.md` — Reference as example implementation

**Implementation approach**:
1. Create `docs/specs/adapters/development-guide.md` covering:
   - Overview of the `AgentAdapter` ABC (`src/adapters/base.py`)
   - Step-by-step: subclass, implement `start/wait/stop/is_alive`
   - `TaskContext` fields explained (what the adapter receives)
   - `AgentOutput` fields explained (what the adapter must return)
   - `MessageCallback` usage for streaming output to Discord
   - Registration in `AdapterFactory` (`src/adapters/__init__.py`)
   - Testing strategy (mock task context, verify output)
   - Example: skeleton adapter with inline comments
2. Reference `src/adapters/claude.py` as the canonical example
3. Include common pitfalls (e.g., heartbeat, token counting, error handling)

**Dependencies**: None
**Complexity**: Low

---

## Phase 9: Document hook pipeline with examples

**Goal**: Create practical documentation for the hook system with real-world
examples users can copy and adapt.

**Files to modify**:
- `docs/specs/hooks.md` — Enhance existing spec with examples section
- `docs/hook-examples.md` (new) — Standalone examples document

**Implementation approach**:
1. Create `docs/hook-examples.md` with practical examples:
   - **Auto-triage**: Event hook on `task.failed` that analyzes error and creates
     a follow-up bugfix task
   - **Daily summary**: Periodic hook that summarizes completed tasks and posts
     to Discord
   - **PR reviewer**: Event hook on `task.completed` that reviews the PR diff
   - **Budget alert**: Periodic hook that checks token usage and warns on threshold
   - **Dependency unblocker**: Event hook on `task.completed` that checks if
     blocked tasks can now proceed
2. Each example includes:
   - Full JSON config for the hook (trigger, context_steps, prompt_template)
   - Explanation of each context step type (shell, file, http, db, git)
   - Expected LLM behavior and tool calls
   - Discord command to create the hook
3. Enhance `docs/specs/hooks.md` with a "Quick Start" section linking to examples

**Dependencies**: None
**Complexity**: Low

---

## Phase 10: Add inline code documentation for complex orchestrator logic

**Goal**: Improve maintainability by adding detailed inline documentation to the
most complex parts of the orchestrator.

**Files to modify**:
- `src/orchestrator.py` — Add/improve docstrings and inline comments
- `src/scheduler.py` — Add/improve docstrings for scheduling algorithm
- `src/hooks.py` — Add/improve docstrings for hook pipeline

**Implementation approach**:
1. In `src/orchestrator.py`, document these complex sections:
   - `run_one_cycle()` — annotate each phase of the cycle
   - `_execute_task()` — document the full task execution flow
   - `_launch_task_execution()` — workspace acquisition, git branching, adapter setup
   - `_handle_agent_output()` — state transitions, retry logic, PR creation
   - Plan parsing and subtask creation flow
   - Workspace locking and release logic
2. In `src/scheduler.py`, document:
   - Proportional credit algorithm
   - `min_task_guarantee` behavior
   - How `rolling_window_hours` affects scheduling fairness
3. In `src/hooks.py`, document:
   - Context step execution pipeline
   - Short-circuit check logic
   - Tool registration for hook LLM calls
   - Cooldown and concurrency limiting
4. Use consistent docstring format (already established: Google-style with
   behavioral descriptions)

**Dependencies**: None
**Complexity**: Low-Medium (requires reading and understanding ~300KB of code)
