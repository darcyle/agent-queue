# Infrastructure & Observability Implementation Plan

Source: `/home/jkern/agent-queue-workspaces/notes/agent-queue/infrastructure.md`

All items are P3 priority. This plan covers Configuration Improvements, Observability, and Documentation — 10 items across 3 categories, organized into 10 implementation phases.

---

## Architecture Context

**Key files referenced throughout this plan:**

| File | Lines | Role |
|------|-------|------|
| `src/config.py` | 463 | YAML config loading, 14 typed dataclasses (`AppConfig`, `DiscordConfig`, etc.), `${ENV_VAR}` substitution, `load_config()` |
| `src/main.py` | 103 | Entry point: `run()` async function, signal handling, `os.execv()` restart |
| `src/orchestrator.py` | 2,295 | Task lifecycle engine: `run_one_cycle()` 10-step loop, agent execution, plan parsing, workspace prep |
| `src/state_machine.py` | 162 | `VALID_TASK_TRANSITIONS` dict (34 entries across 11 states), `InvalidTransition`, DAG validation |
| `src/hooks.py` | 625 | `HookEngine` class: periodic/event triggers, 6 context step types, short-circuit, LLM invocation |
| `src/adapters/base.py` | 51 | `AgentAdapter` ABC: `start()`, `wait()`, `stop()`, `is_alive()`, `MessageCallback` type |
| `src/adapters/claude.py` | 600+ | Claude Code SDK adapter: subprocess management, message streaming, env vars |
| `src/models.py` | 397 | `TaskStatus` (11 states), `TaskEvent` (17 events), `Task`, `AgentOutput`, `TaskContext` dataclasses |
| `src/llm_logger.py` | 205 | `LLMLogger` class: JSONL append-only logging, `log_chat_provider_call()`, `log_agent_session()` |
| `src/chat_providers/logged.py` | 88 | `LoggedChatProvider` decorator: timing + logging wrapper around any `ChatProvider` |
| `src/chat_providers/anthropic.py` | 4,500+ | Anthropic API integration with streaming |
| `src/database.py` | 2,083 | SQLite persistence (WAL mode), 14 tables, CRUD, migrations |
| `src/command_handler.py` | 4,448 | 50+ commands via `execute(command_name, **kwargs)` dispatch |
| `src/event_bus.py` | 45 | `EventBus` class: async pub/sub with wildcard support |

**Current config loading flow:**
1. `_load_env_file()` — loads `.env` from config directory (doesn't override existing env vars)
2. `yaml.safe_load()` — parse YAML
3. `_process_values()` — recursive `${ENV_VAR}` substitution across all strings
4. Manual mapping — each YAML section mapped to a typed dataclass with `.get()` defaults
5. No validation — invalid values caught only when that code path is hit at runtime

**Current logging:**
- Python stdlib `logging` module with default formatting (no custom setup in `main.py`)
- LLM-specific JSONL logging in `src/llm_logger.py` (opt-in via `llm_logging.enabled`)
- `LoggedChatProvider` wraps any `ChatProvider` with timing + JSONL logging
- No structured logging, no correlation IDs, no per-task log context

**Current state machine:**
- `VALID_TASK_TRANSITIONS` dict in `state_machine.py` with 34 entries mapping `(TaskStatus, TaskEvent) → TaskStatus`
- 11 states: DEFINED, READY, ASSIGNED, IN_PROGRESS, WAITING_INPUT, PAUSED, VERIFYING, AWAITING_APPROVAL, COMPLETED, FAILED, BLOCKED
- 17 events: DEPS_MET, ASSIGNED, AGENT_STARTED, AGENT_COMPLETED, AGENT_FAILED, etc.
- **Not enforced in production** — orchestrator uses direct `db.update_task()` calls
- Module already has good docstrings on classes and functions

**Current adapter interface:**
- `AgentAdapter` ABC in `src/adapters/base.py` with 4 abstract methods
- `MessageCallback = Callable[[str], Awaitable[None]]` for streaming output
- `TaskContext` and `AgentOutput` dataclasses defined in `src/models.py`
- Only one implementation: `ClaudeAdapter` in `src/adapters/claude.py`
- Factory: `AdapterFactory` in `src/adapters/__init__.py`

**Current hook system:**
- `HookEngine` class in `src/hooks.py` with `tick()` (periodic) and `_on_event()` (event-driven)
- 6 context step types: `shell`, `read_file`, `http`, `db_query`, `git_diff`, `memory_search`
- Short-circuit: `skip_llm_if_exit_zero`, `skip_llm_if_empty`, `skip_llm_if_status_ok`
- Prompt templates with `{{step_N}}`, `{{step_N.field}}`, `{{event.field}}` placeholders
- LLM invocation uses a temporary `ChatAgent` instance with full tool access
- Named DB queries: `recent_task_results`, `task_detail`, `recent_events`, `hook_runs`

---

## Phase 1: Config Validation on Startup

**Goal:** Fail fast on misconfiguration with clear, actionable error messages.

**Files to modify:**
- `src/config.py` — Add `ConfigError` dataclass, `validate()` method to `AppConfig`, per-section `validate()` methods
- `src/main.py` — Call `config.validate()` after `load_config()`, add `--validate-config` CLI flag
- `tests/test_config.py` — Unit tests for validation rules

**Implementation approach:**
1. Add `ConfigError` dataclass: `(section: str, field: str, message: str, severity: str)` where severity is `"error"` or `"warning"`
2. Add `validate() -> list[ConfigError]` method to `AppConfig` that delegates to per-section validators:
   - `DiscordConfig.validate()` — `bot_token` non-empty, `guild_id` non-empty (both required for Discord to connect)
   - `AgentsDefaultConfig.validate()` — `heartbeat_interval_seconds > 0`, `stuck_timeout_seconds >= 0`, `graceful_shutdown_timeout_seconds > 0`
   - `SchedulingConfig.validate()` — `rolling_window_hours > 0`
   - `PauseRetryConfig.validate()` — all seconds fields `> 0`, `rate_limit_max_retries >= 0`
   - `ChatProviderConfig.validate()` — `provider` in `["anthropic", "ollama"]`; if `ollama`, `base_url` required
   - `AutoTaskConfig.validate()` — `max_plan_depth >= 1`, `max_steps_per_plan >= 1`, `base_priority >= 0`
   - `ArchiveConfig.validate()` — `after_hours > 0`, each status in `statuses` is a valid `TaskStatus` name
   - `LLMLoggingConfig.validate()` — if `enabled`, `retention_days > 0`
   - `MemoryConfig.validate()` — if `enabled`, `embedding_provider` in known list, `max_chunk_size > 0`
   - `AppConfig.validate()` — cross-field: `workspace_dir` is writable or creatable, `database_path` parent dir exists or is creatable
3. Collect ALL errors before reporting (don't stop at first error) — return full list
4. In `main.py`, insert validation between `load_config()` (line 37) and `Orchestrator()` init (line 42):
   ```python
   config = load_config(config_path)
   errors = config.validate()
   if errors:
       for e in errors:
           print(f"Config {e.severity}: [{e.section}] {e.field}: {e.message}", file=sys.stderr)
       if any(e.severity == "error" for e in errors):
           sys.exit(1)
   ```
5. Add `--validate-config` CLI flag in `main()` that calls `load_config()` + `validate()` without starting services
6. Agent profiles validation: each profile has non-empty `id`, `permission_mode` in known values

**Dependencies:** None (standalone improvement)

**Estimated complexity:** Low-medium. ~150 lines of validation logic in config.py + ~15 lines in main.py + ~100 lines of tests.

---

## Phase 2: Config Hot-Reloading for Non-Critical Settings

**Goal:** Allow changing non-critical config values without restarting the application.

**Files to modify:**
- `src/config.py` — Add `ConfigWatcher` class, `HOT_RELOADABLE_SECTIONS` set, `diff_configs()` helper
- `src/orchestrator.py` — Start `ConfigWatcher`, subscribe to `config.reloaded` event, update `self.config`
- `src/hooks.py` — Subscribe to config changes, update `self.config.hook_engine`
- `src/event_bus.py` — No changes needed (already supports arbitrary event types via wildcard)
- `src/command_handler.py` — Add `reload_config` command for manual trigger
- `tests/test_config_watcher.py` — Tests for reload and diff logic

**Implementation approach:**
1. Define classification constants in `config.py`:
   ```python
   HOT_RELOADABLE_SECTIONS = {
       "scheduling", "monitoring", "hook_engine", "archive",
       "llm_logging", "pause_retry", "agents",
   }
   RESTART_REQUIRED_SECTIONS = {
       "discord", "workspace_dir", "database_path", "chat_provider",
   }
   ```
2. Add `ConfigWatcher` class:
   - Constructor takes `config_path: str`, `event_bus: EventBus`, `poll_interval: float = 30.0`
   - `start()` → launches `asyncio.create_task(self._poll_loop())`
   - `_poll_loop()` → checks `os.path.getmtime(config_path)` every `poll_interval` seconds
   - On mtime change: `reload()` → calls `load_config()`, `validate()`, `diff_configs(old, new)`
   - Emits `config.reloaded` event via EventBus with `{"changed_sections": [...], "config": new_config}`
   - If any `RESTART_REQUIRED_SECTIONS` changed: emit `config.restart_needed` event (warn, don't apply)
3. Add `diff_configs(old: AppConfig, new: AppConfig) -> set[str]`:
   - Compare each section by `dataclasses.asdict()` equality
   - Return set of section names that changed
4. In `orchestrator.py`:
   - `initialize()` → start `ConfigWatcher`, subscribe to `config.reloaded`
   - Handler: `self.config = event["config"]` for hot-reloadable sections
   - Update derived state: `self.scheduler` gets new scheduling config, `self._budget_manager` gets new budgets
5. Add `reload_config` command to `command_handler.py`:
   - Triggers `config_watcher.reload()` manually
   - Returns summary of what changed
6. In `hooks.py`:
   - Subscribe to `config.reloaded`, update `self.config` reference

**Dependencies:** Phase 1 (validation logic reused on reload to reject invalid config)

**Estimated complexity:** Medium. ~200 lines for ConfigWatcher + diff logic + ~50 lines across consumers + ~30 lines for command.

---

## Phase 3: Environment-Specific Config Profiles

**Goal:** Support dev/staging/production config overlays to simplify multi-environment deployments.

**Files to modify:**
- `src/config.py` — Add `_deep_merge()` helper, profile loading in `load_config()`, `profile` field on `AppConfig`
- `src/main.py` — Accept `--profile` CLI argument, pass to `load_config()`

**Implementation approach:**
1. Add `profile: str = ""` field to `AppConfig` dataclass (after `database_path`)
2. Add `_deep_merge(base: dict, overlay: dict) -> dict` helper:
   - Recursively merge dicts (overlay keys win)
   - Lists are **replaced** (not appended) to keep behavior predictable
   - `None` values in overlay remove the key from base
3. Modify `load_config(path: str, profile: str | None = None) -> AppConfig`:
   - After loading base YAML and before `_process_values()`:
   - If `profile` is provided (or `AGENT_QUEUE_PROFILE` env var is set):
     - Look for `{config_dir}/profiles/{profile}.yaml`
     - If found: `raw = _deep_merge(raw, profile_raw)`
     - If not found: raise `FileNotFoundError` with helpful message listing available profiles
   - Set `config.profile = profile or ""`
4. In `main.py`:
   - Parse `--profile` from `sys.argv` (before the config path arg)
   - Pass `profile` to `load_config()`
   - Log at startup: `f"Starting with profile: {config.profile}"` (or "no profile" if empty)
5. Profile precedence (highest to lowest):
   - `--profile` CLI argument
   - `AGENT_QUEUE_PROFILE` environment variable
   - No profile (current behavior, backward compatible)

**Dependencies:** Phase 1 (validation applies to the merged config, catches profile-introduced errors)

**Estimated complexity:** Low-medium. ~80 lines for deep-merge + profile loading + ~15 lines in main.py + ~60 lines tests.

---

## Phase 4: Structured Logging with Correlation IDs

**Goal:** Add JSON-structured logging with per-task correlation IDs for easier debugging and log aggregation.

**Files to modify:**
- New file: `src/logging_config.py` — `StructuredFormatter`, `CorrelationContext`, `setup_logging()`
- `src/config.py` — Add `LoggingConfig` dataclass (format, level)
- `src/main.py` — Call `setup_logging(config)` at startup before any `logger.info()` calls
- `src/orchestrator.py` — Wrap `_run_agent_for_task()` and key methods with `with_correlation()`
- `src/hooks.py` — Wrap `_execute_hook()` with `with_correlation(hook_id=...)`
- `src/command_handler.py` — Wrap `execute()` with `with_correlation(command=...)`
- `src/adapters/claude.py` — Access correlation context for adapter-level logging

**Implementation approach:**
1. Create `src/logging_config.py`:
   ```python
   import contextvars
   import json
   import logging
   from contextlib import contextmanager

   # Context variables for async-safe correlation
   _task_id: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="")
   _project_id: contextvars.ContextVar[str] = contextvars.ContextVar("project_id", default="")
   _agent_id: contextvars.ContextVar[str] = contextvars.ContextVar("agent_id", default="")
   _hook_id: contextvars.ContextVar[str] = contextvars.ContextVar("hook_id", default="")

   @contextmanager
   def with_correlation(*, task_id=None, project_id=None, agent_id=None, hook_id=None):
       """Set correlation context for the current async call chain."""
       tokens = []
       if task_id: tokens.append(_task_id.set(task_id))
       if project_id: tokens.append(_project_id.set(project_id))
       if agent_id: tokens.append(_agent_id.set(agent_id))
       if hook_id: tokens.append(_hook_id.set(hook_id))
       try:
           yield
       finally:
           for token in tokens:
               # Reset each context var

   class StructuredFormatter(logging.Formatter):
       """JSON-lines log formatter with correlation context."""
       def format(self, record):
           entry = {
               "timestamp": self.formatTime(record),
               "level": record.levelname,
               "logger": record.name,
               "message": record.getMessage(),
           }
           # Add correlation context
           for name, var in [("task_id", _task_id), ("project_id", _project_id),
                             ("agent_id", _agent_id), ("hook_id", _hook_id)]:
               val = var.get("")
               if val:
                   entry[name] = val
           if record.exc_info:
               entry["exception"] = self.formatException(record.exc_info)
           return json.dumps(entry)

   def setup_logging(config):
       """Configure root logger based on config."""
   ```
2. Add `LoggingConfig` dataclass to `src/config.py`:
   ```python
   @dataclass
   class LoggingConfig:
       format: str = "human"  # "human" or "structured"
       level: str = "INFO"
   ```
   Add `logging: LoggingConfig` to `AppConfig`, load from YAML `logging` section
3. In `main.py`, call `setup_logging(config.logging)` as first action after `load_config()`
4. In `orchestrator.py`, wrap key async paths:
   - `_run_agent_for_task(task, agent)` → `with with_correlation(task_id=task.id, project_id=task.project_id, agent_id=agent.id):`
   - `_promote_defined_to_ready()` → no context needed (operates on many tasks)
   - `_check_heartbeats()` → per-agent context
5. In `hooks.py`, wrap `_execute_hook()`:
   - `with with_correlation(hook_id=hook.id, project_id=hook.project_id):`
6. In `command_handler.py`, wrap `execute()` with command name context
7. Default to `"human"` format (current behavior) — `"structured"` opt-in for production

**Dependencies:** None (standalone, but pairs well with Phase 3 for per-env log format)

**Estimated complexity:** Medium. ~120 lines for `logging_config.py` + ~20 lines config + ~40 lines scattered context-setting.

---

## Phase 5: Health Check Endpoint

**Goal:** Expose an HTTP health check endpoint for external monitoring tools (uptime checks, container orchestration probes).

**Files to modify:**
- New file: `src/health.py` — `HealthCheckServer` class with aiohttp or stdlib `asyncio.start_server`
- `src/orchestrator.py` — Add `get_health_status() -> dict` method
- `src/config.py` — Add `HealthCheckConfig` dataclass
- `src/main.py` — Start health server alongside orchestrator and Discord bot

**Implementation approach:**
1. Add `HealthCheckConfig` to `src/config.py`:
   ```python
   @dataclass
   class HealthCheckConfig:
       enabled: bool = False
       port: int = 8080
       host: str = "0.0.0.0"
   ```
   Add to `AppConfig`, load from YAML `health_check` section
2. Create `src/health.py` using Python stdlib `asyncio.start_server` + manual HTTP parsing (avoids adding aiohttp dependency):
   - `HealthCheckServer(orchestrator, config)` class
   - Three endpoints via path dispatch:
     - `GET /health` — liveness: always 200 `{"status": "alive"}`
     - `GET /health/ready` — readiness: 200 if orchestrator loop active + DB accessible, 503 otherwise
     - `GET /health/detail` — full status JSON from `orchestrator.get_health_status()`
   - Simple HTTP/1.1 response formatting (Content-Type: application/json)
   - `start()` / `stop()` async methods
3. Add `get_health_status() -> dict` to `Orchestrator`:
   ```python
   async def get_health_status(self) -> dict:
       # Collect from internal state + DB queries
       agents = await self.db.list_agents()
       tasks_by_status = await self.db.count_tasks_by_status()
       db_size = os.path.getsize(self.config.database_path) / (1024*1024)
       return {
           "status": "healthy",
           "uptime_seconds": time.time() - self._start_time,
           "orchestrator": {"paused": self._paused, "cycle_count": self._cycle_count},
           "agents": {"total": len(agents), "idle": ..., "busy": ...},
           "tasks": tasks_by_status,
           "database": {"connected": True, "size_mb": round(db_size, 1)},
       }
   ```
   Track `self._start_time = time.time()` and `self._cycle_count` in orchestrator
4. In `main.py`, add health server to the async task group:
   ```python
   if config.health_check.enabled:
       health_server = HealthCheckServer(orch, config.health_check)
       health_task = asyncio.create_task(health_server.start())
   ```
   Clean up in `finally` block
5. No authentication — health endpoints are internal-only behind firewall/VPN

**Dependencies:** None (standalone)

**Estimated complexity:** Medium. ~130 lines for health.py + ~30 lines for orchestrator status method + ~20 lines config + ~15 lines main.py.

---

## Phase 6: Improved LLM Interaction Logging

**Goal:** Enhance LLM logging to support prompt optimization — track token costs, response quality signals, and provide queryable analytics.

**Files to modify:**
- `src/llm_logger.py` — Extend `log_chat_provider_call()` with token usage, cost, outcome fields
- `src/chat_providers/logged.py` — Extract and pass through token usage from `ChatResponse`
- `src/chat_providers/types.py` — Add `usage` field to `ChatResponse` if not present
- `src/chat_providers/anthropic.py` — Populate `usage` from Anthropic API response
- `src/hooks.py` — Pass hook outcome (action_taken/no_action/error) to logger
- New file: `src/llm_analytics.py` — JSONL aggregation queries
- `src/command_handler.py` — Add `llm_stats` command

**Implementation approach:**
1. Extend `LLMLogger.log_chat_provider_call()` with optional kwargs:
   - `input_tokens: int = 0`, `output_tokens: int = 0`
   - `cache_creation_tokens: int = 0`, `cache_read_tokens: int = 0`
   - `estimated_cost_usd: float = 0.0`
   - `prompt_template_id: str = ""` (for hooks: which hook generated this call)
   - `outcome: str = ""` (e.g., "action_taken", "no_action", "error", "completed", "failed")
   Add these to the JSONL entry dict (only if non-zero/non-empty to keep backward compat)
2. Add model pricing table to `llm_logger.py`:
   ```python
   MODEL_PRICING = {  # per million tokens
       "claude-sonnet-4": {"input": 3.0, "output": 15.0},
       "claude-haiku-3.5": {"input": 0.80, "output": 4.0},
       # ...
   }
   ```
   Calculate `estimated_cost_usd` from tokens + model name
3. Update `LoggedChatProvider` in `logged.py`:
   - After `create_message()` returns, extract `response.usage` (if available)
   - Pass `input_tokens`, `output_tokens`, etc. to `log_chat_provider_call()`
4. Ensure `ChatResponse` in `types.py` has a `usage: dict | None` field populated by the Anthropic provider
5. Update `hooks.py` `_execute_hook()`:
   - After LLM call, determine outcome: if hook took action (created task, sent message) vs. no action
   - Pass `outcome` and `prompt_template_id=hook.id` to logger
6. Create `src/llm_analytics.py`:
   - `daily_token_summary(base_dir, date) -> dict` — read JSONL, aggregate tokens/cost by model
   - `hook_effectiveness(base_dir, hook_id, days=7) -> dict` — action_taken_pct, avg_tokens
   - `prompt_cost_ranking(base_dir, days=7) -> list[dict]` — top callers by total cost
   - All functions read JSONL files directly (no database needed)
7. Add `llm_stats` command to `command_handler.py`:
   - Shows daily summary, top callers, hook effectiveness
   - Format as Discord embed

**Dependencies:** None (enhances existing `llm_logger.py`, backward compatible)

**Estimated complexity:** Medium. ~100 lines analytics + ~60 lines extending logger + ~40 lines logged provider + ~50 lines command.

---

## Phase 7: Document Task State Machine with Transition Diagrams

**Goal:** Create comprehensive documentation of the task lifecycle with visual state diagrams.

**Files to create/modify:**
- New file: `docs/architecture/task-state-machine.md` — Full state machine documentation with Mermaid diagrams
- `src/state_machine.py` — Enhance existing docstrings (module already has good ones; add per-group comments)

**Implementation approach:**
1. Create `docs/architecture/task-state-machine.md` with:
   - **Overview** — purpose of the state machine, its role in the system, enforcement status
   - **States reference** — all 11 `TaskStatus` values with descriptions:
     - DEFINED: created but dependencies not yet checked
     - READY: dependencies met, eligible for scheduling
     - ASSIGNED: scheduler picked this task, agent not yet started
     - IN_PROGRESS: agent is actively working
     - WAITING_INPUT: agent asked a question, waiting for human reply
     - PAUSED: rate-limited or token-exhausted, waiting for `resume_after`
     - VERIFYING: agent completed, checking results / PR status
     - AWAITING_APPROVAL: PR created, waiting for merge/approval
     - COMPLETED: terminal success
     - FAILED: agent failed, may be retried
     - BLOCKED: max retries exceeded or admin-stopped, terminal failure
   - **Events reference** — all 17 `TaskEvent` values
   - **Mermaid stateDiagram** showing all transitions (generated from `VALID_TASK_TRANSITIONS`)
   - **Grouped transition tables** with rationale:
     - Core lifecycle (happy path)
     - Error/retry path
     - Pause/resume path
     - PR workflow
     - Human input workflow
     - Administrative overrides
     - Daemon recovery
   - **Common scenarios** with step-by-step traces:
     - Successful task completion
     - Task with rate limiting
     - Task requiring PR approval
     - Task that fails and retries
     - Agent asking a question
   - **Enforcement note** — current status (validated but not enforced) and implications
2. Enhance `state_machine.py`:
   - The module already has a good module-level docstring — keep it
   - Add a docstring block above `VALID_TASK_TRANSITIONS` explaining the group organization
   - Add brief inline comments for the less obvious transitions (e.g., direct shortcuts)

**Dependencies:** None

**Estimated complexity:** Low. ~250 lines of documentation + ~20 lines of enhanced comments.

---

## Phase 8: Adapter Development Guide

**Goal:** Document how to create a new agent adapter so contributors can integrate new AI coding tools.

**Files to create/modify:**
- New file: `docs/guides/adapter-development.md` — Step-by-step tutorial and reference
- `src/adapters/base.py` — Enhance method docstrings with parameter details and behavioral contracts

**Implementation approach:**
1. Create `docs/guides/adapter-development.md` covering:
   - **Overview** — what an adapter is, the adapter pattern in this codebase
   - **Architecture** — how the orchestrator invokes adapters:
     ```
     Orchestrator._run_agent_for_task()
       → AdapterFactory.create(agent_type)
       → adapter.start(TaskContext)
       → adapter.wait(on_message=discord_thread_callback)
       → returns AgentOutput
       → orchestrator processes result
     ```
   - **Interface reference** — each `AgentAdapter` method:
     - `start(task: TaskContext)` — receives workspace path, task description, criteria, tools, system prompt
     - `wait(on_message: MessageCallback | None) -> AgentOutput` — blocks until done, streams progress
     - `stop()` — force-terminate, called on timeout/cancellation/shutdown
     - `is_alive() -> bool` — heartbeat check, called every `heartbeat_interval_seconds`
   - **Data types** — `TaskContext` fields (from `models.py`), `AgentOutput` fields, `AgentResult` enum
   - **Tutorial** — build a minimal "shell script" adapter:
     - Runs a bash script in the workspace directory
     - Captures stdout as output
     - Returns success/failure based on exit code
     - ~50 lines of example code
   - **Registration** — how `AdapterFactory` maps agent types to adapter classes
   - **Real-world walkthrough** — annotated highlights from `claude.py`:
     - Subprocess management with `asyncio.create_subprocess_exec`
     - Streaming JSONL messages from subprocess stdout
     - Environment variable setup (API keys, workspace path, tools)
     - Error handling for unknown message types
     - Graceful shutdown with `SIGTERM` → `SIGKILL` escalation
   - **Testing** — how to test an adapter locally (create a task, assign it, check output)
2. Enhance `src/adapters/base.py` docstrings:
   - Add parameter descriptions to each method
   - Add behavioral contracts (e.g., "start() must not block — launch the process and return")
   - Add example usage in module docstring

**Dependencies:** None

**Estimated complexity:** Low-medium. ~350 lines of documentation + ~30 lines of enhanced docstrings.

---

## Phase 9: Document Hook Pipeline with Examples

**Goal:** Comprehensive guide to the hook system — architecture, configuration, and practical examples.

**Files to create/modify:**
- New file: `docs/guides/hook-pipeline.md` — Hook system documentation with examples
- `src/hooks.py` — Add section-level docstrings to key methods

**Implementation approach:**
1. Create `docs/guides/hook-pipeline.md` covering:
   - **Architecture diagram** (Mermaid flowchart):
     ```
     Trigger (periodic/event) → Context Steps → Short-Circuit Check
       → Prompt Template Rendering → LLM Invocation → Tool Execution → Record Result
     ```
   - **Trigger types** with config examples:
     - `periodic` — `{"type": "periodic", "interval_seconds": 3600}`, fires on tick, respects cooldown
     - `event` — `{"type": "event", "event_type": "task.completed"}`, fires on EventBus event
   - **Context steps** — detailed reference for each of the 6 types:
     - `shell` — `{"type": "shell", "command": "...", "timeout": 60}`, captures stdout/stderr/exit_code
     - `read_file` — `{"type": "read_file", "path": "...", "max_lines": 500}`, returns content
     - `http` — `{"type": "http", "url": "...", "timeout": 30}`, returns body/status_code
     - `db_query` — `{"type": "db_query", "query": "recent_task_results"}`, uses named queries only
     - `git_diff` — `{"type": "git_diff", "workspace": ".", "base_branch": "main"}`, returns diff
     - `memory_search` — `{"type": "memory_search", "project_id": "...", "query": "...", "top_k": 3}`
   - **Short-circuit conditions** — save tokens by skipping LLM when context answers the question:
     - `skip_llm_if_exit_zero` — shell command succeeded, nothing to report
     - `skip_llm_if_empty` — no output from step, nothing to analyze
     - `skip_llm_if_status_ok` — HTTP 2xx, service is healthy
   - **Prompt templates** — placeholder syntax:
     - `{{step_0}}` — auto-selects stdout/content/body/diff from step 0
     - `{{step_0.stderr}}` — specific field from step result
     - `{{event.task_id}}` — field from triggering event data
     - `{{event}}` — full event data as JSON
   - **Tool access** — hooks get the same tools as Discord users (create_task, check_status, etc.)
   - **Concurrency & cooldown** — `max_concurrent_hooks` cap, per-hook `cooldown_seconds`
   - **Practical examples** (4 complete hook definitions):
     - Auto-reviewer: on `task.completed`, git_diff + LLM review → create follow-up task if issues found
     - Budget alert: periodic, db_query token usage → notify if over threshold
     - Dependency monitor: periodic, shell `pip list --outdated` → report if updates available
     - Deploy watcher: periodic, http check CI/CD status → notify on failure
   - **Debugging** — check `hook_runs` via `/hook-runs` command, common pitfalls (missing named query, placeholder typos)
   - **Creating hooks** — via Discord `/create-hook` command, required fields
2. Enhance `src/hooks.py` with docstrings:
   - `_run_context_steps()` — explain sequential execution, error isolation
   - `_should_skip_llm()` — explain each condition
   - `_render_prompt()` — explain placeholder resolution order
   - `_invoke_llm()` — explain ChatAgent reuse and tool access

**Dependencies:** None

**Estimated complexity:** Low-medium. ~400 lines of documentation + ~40 lines of enhanced docstrings.

---

## Phase 10: Inline Code Documentation for Complex Orchestrator Logic

**Goal:** Add comprehensive inline documentation to the orchestrator's most complex logic paths so future contributors can understand the system quickly.

**Files to modify:**
- `src/orchestrator.py` — Docstrings and inline comments for complex methods
- `src/database.py` — Document schema decisions, complex queries, concurrency model
- `src/command_handler.py` — Document command routing pattern and error handling

**Implementation approach:**
1. **`src/orchestrator.py`** — Focus areas (the module docstring is already good):
   - `run_one_cycle()` — Add numbered inline comments for each of the 10 steps explaining **why** this order matters:
     1. Sync agent profiles (must happen before scheduling)
     2. Promote DEFINED → READY (dependency check)
     3. Check heartbeats (detect dead agents before assigning new work)
     4. Assign READY → agents (scheduling)
     5. Execute assigned tasks (launch adapters)
     6. Check AWAITING_APPROVAL (PR status)
     7. Tick hook engine
     8. Check plan files (parse newly generated plans)
     9. Check stuck tasks (timeouts)
     10. Cleanup old logs (hourly, not every cycle)
   - `_run_agent_for_task()` — Document the full lifecycle:
     - Workspace preparation (clone/link/init strategies and when each is used)
     - Branch creation and rebase strategy
     - Adapter start → wait → result processing
     - Plan file discovery after agent completes
     - PR creation and notification flow
     - Error handling: rate limits, crashes, timeouts
   - `_prepare_workspace()` — Document clone vs link vs init:
     - Clone: full git clone for isolated work
     - Link: symlink to shared repo for read-heavy tasks
     - Init: fresh workspace for tasks without a repo
   - `_handle_plan_file()` — Document plan parsing pipeline:
     - Discovery → read → parse (regex or LLM) → create subtasks → wire dependencies
   - `_check_pr_status()` — Document PR approval workflow edge cases:
     - PR merged → COMPLETED
     - PR closed without merge → BLOCKED
     - No PR URL but AWAITING_APPROVAL → stuck (known issue P0)
   - Dead agent detection — heartbeat protocol and recovery actions
   - Budget monitoring — threshold logic and rate-limiting of notifications
2. **`src/database.py`** — Focus areas:
   - Module docstring — explain schema design decisions:
     - Why 14 tables (normalized for flexibility, denormalized where needed for performance)
     - WAL mode rationale (concurrent reads from Discord commands while orchestrator writes)
     - Migration strategy (idempotent ALTER TABLE ADD COLUMN)
   - Complex queries — add inline comments:
     - Task scheduling query (how priority and credit_weight interact)
     - Token ledger aggregation (rolling window calculation)
     - Dependency resolution (recursive WITH for transitive deps)
   - Transaction boundaries — document which operations need transactions
3. **`src/command_handler.py`** — Focus areas:
   - Module docstring — explain the command dispatch pattern:
     - `execute(command_name, **kwargs)` as single entry point
     - How command names map to handler methods
   - Permission model — how `authorized_users` is checked
   - Error handling — what exceptions propagate vs. are caught
   - Add docstrings to the 10 most complex command handlers

**Dependencies:** Phase 7 (state machine docs provide context that inline orchestrator docs will reference)

**Estimated complexity:** Medium. ~300 lines of docstrings and comments spread across 3 files. Requires careful reading of existing logic to write accurate docs.

---

## Summary

| Phase | Item | Category | Complexity | Dependencies | Parallelizable |
|-------|------|----------|------------|--------------|----------------|
| 1 | Config validation on startup | Config | Low-medium | None | Yes |
| 2 | Config hot-reloading | Config | Medium | Phase 1 | After Phase 1 |
| 3 | Environment-specific profiles | Config | Low-medium | Phase 1 | After Phase 1 |
| 4 | Structured logging + correlation IDs | Observability | Medium | None | Yes |
| 5 | Health check endpoint | Observability | Medium | None | Yes |
| 6 | Improved LLM logging | Observability | Medium | None | Yes |
| 7 | Task state machine docs | Documentation | Low | None | Yes |
| 8 | Adapter development guide | Documentation | Low-medium | None | Yes |
| 9 | Hook pipeline docs | Documentation | Low-medium | None | Yes |
| 10 | Inline orchestrator docs | Documentation | Medium | Phase 7 | After Phase 7 |

**Recommended execution order:**
- **Wave 1 (parallel):** Phases 1, 4, 5, 6, 7, 8, 9 — all independent, no dependencies
- **Wave 2 (after Phase 1):** Phases 2, 3 — both depend on Phase 1's validation logic
- **Wave 3 (after Phase 7):** Phase 10 — benefits from state machine docs being complete

**Total estimated effort:** ~2,500 lines of new code/documentation across all phases.
