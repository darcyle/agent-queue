# CLAUDE.md — Agent Queue Project Overview

## What This Is

Agent Queue is a task queue and orchestrator for AI coding agents (primarily Claude Code) running on throttled/subsidized token plans. It keeps agents continuously busy across multiple projects, automatically handles rate limits, and provides full control via Discord chat.

**Key principle:** Zero LLM calls for orchestration — all scheduling is deterministic. Every token goes to agent work, not coordination overhead.

## Architecture

Single Python asyncio process. Event-driven state machine. SQLite for persistence. Discord bot for the control plane.

```
asyncio event loop
├── Discord Bot          — listens for commands/messages, delegates to ChatAgent
├── Orchestrator Loop    — runs every ~5 seconds, manages task lifecycle
│   ├── Check DEFINED tasks (promote to READY if deps met)
│   ├── Check agent heartbeats (detect dead agents)
│   ├── Assign READY tasks to idle agents (via Scheduler)
│   ├── Execute assigned tasks (streams to Discord threads)
│   ├── Check AWAITING_APPROVAL tasks
│   ├── Run hook engine tick
│   └── Re-check newly generated tasks
└── Shutdown Handler     — graceful cleanup on signals
```

All components communicate through an async EventBus. SQLite uses WAL journal mode for concurrent reads.

## Key Files

### Core

| File | Lines | Purpose |
|------|-------|---------|
| `src/main.py` | ~80 | Entry point — parses CLI args, starts async loop with Discord + orchestrator |
| `src/orchestrator.py` | ~1,250 | **Central brain** — task lifecycle, agent management, plan generation, rate limit recovery |
| `src/command_handler.py` | ~1,300 | **Unified command execution** — single entry point for all operations (50+ commands) |
| `src/chat_agent.py` | ~1,200 | LLM-powered Discord interface — tool definitions, message context, streaming |
| `src/database.py` | ~630 | SQLite persistence layer — 14 tables, CRUD, dependency checks, migrations |
| `src/models.py` | ~200 | Dataclasses and enums — Task, Agent, Project, Hook, AgentOutput, TaskContext |
| `src/config.py` | ~220 | YAML config loading with `${ENV_VAR}` substitution, dataclass models |
| `src/scheduler.py` | ~85 | Proportional credit-weight scheduling algorithm |
| `src/state_machine.py` | ~78 | Formal task state transitions and DAG cycle detection |
| `src/event_bus.py` | ~26 | Simple async pub/sub with wildcard support |

### Subsystems

| Directory | Purpose |
|-----------|---------|
| `src/adapters/` | Agent adapter interface (`base.py`) and Claude Code implementation (`claude.py` ~600 lines) |
| `src/discord/` | Discord bot (`bot.py`), slash commands (`commands.py`), notification formatting (`notifications.py`) |
| `src/git/` | Git operations — clone, branch, worktree, push/pull (`manager.py`) |
| `src/tokens/` | Token budget calculation (`budget.py`) and usage ledger (`tracker.py`) |
| `src/chat_providers/` | LLM provider abstraction — Anthropic (`anthropic.py`), Ollama (`ollama.py`) |

### Supporting

| File | Purpose |
|------|---------|
| `src/plan_parser.py` | Parses `.claude/plan.md` or `plan.md` into structured steps for task generation |
| `src/hooks.py` | Generic hook engine — trigger, gather context, optionally call LLM with tools |
| `src/task_names.py` | Generates human-readable task IDs (`adjective-noun` format, ~900 combinations) |

## Task Lifecycle (State Machine)

```
DEFINED → READY → ASSIGNED → IN_PROGRESS → VERIFYING → COMPLETED
                                   │
                         ┌─────────┼──────────┐
                         ▼         ▼          ▼
                      PAUSED   WAITING    FAILED
                      (auto-   _INPUT     (retry →
                      resume)  (Discord)   BLOCKED)

AWAITING_APPROVAL  (after work, before merge — requires manual approve)
```

**Promotion rules:**
- DEFINED → READY: all dependencies are COMPLETED
- READY → ASSIGNED: scheduler picks it for an idle agent
- PAUSED tasks always have `resume_after` timestamp — never stall permanently
- Failed tasks retry up to a configurable limit, then become BLOCKED

**Plan-generated tasks:** When an agent completes a task and produces a `.claude/plan.md` file, the orchestrator parses it and creates chained subtasks with dependencies.

## Database Schema (14 tables)

Core tables: `projects`, `repos`, `tasks`, `task_dependencies`, `agents`, `token_ledger`, `events`, `rate_limits`

Supporting: `task_criteria`, `task_context`, `task_tools`, `task_results`, `hooks`, `hook_runs`, `system_config`

Dependencies are checked via `are_dependencies_met(task_id)` which requires all upstream tasks to be `COMPLETED`.

## Design Patterns

- **Command Pattern:** `CommandHandler` is the single execution point for all operations. Both Discord slash commands and ChatAgent LLM tools delegate here. This ensures feature parity and consistent error handling.
- **Adapter Pattern:** `AgentAdapter` interface allows pluggable agent types. Currently only Claude Code implemented.
- **Event-Driven:** `EventBus` decouples components. Hook engine subscribes to task lifecycle events.
- **Repository Pattern:** `Database` class abstracts all SQLite operations.

## Configuration

Config file: `~/.agent-queue/config.yaml` (YAML with `${ENV_VAR}` substitution)

Key config sections:
- `discord` — bot token, guild ID, channel names, authorized users
- `scheduling` — rolling window, min task guarantee
- `auto_task` — plan file patterns, inheritance flags, chain dependencies
- `pause_retry` — rate limit backoff, token exhaustion retry intervals
- `hook_engine` — enable/disable, max concurrent hooks
- `chat_provider` — provider (anthropic/ollama), model name

## Development

```bash
# Install
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run specific test
pytest tests/test_orchestrator.py -v

# Start daemon
./run.sh start

# Check status
./run.sh status

# View logs
./run.sh logs
```

**Python version:** 3.12+
**Linter:** ruff (line-length 100, target py312)
**Test framework:** pytest with pytest-asyncio (`asyncio_mode = "auto"`)
**Dependencies:** aiosqlite, discord.py, claude-agent-sdk, pyyaml

## Known Issues

Documented in `plan.md` — five interrelated issues with plan-generated tasks getting stuck in DEFINED:

1. **P0** — Inherited approval blocks entire dependency chains (all intermediate tasks require manual approval)
2. **P0** — AWAITING_APPROVAL tasks without PR URLs are silently skipped forever
3. **P1** — BLOCKED tasks orphan all downstream dependents with zero visibility
4. **P2** — No monitoring/alerting for tasks stuck in DEFINED
5. **P3** — One-cycle delay before first plan-generated task gets promoted

Also: `state_machine.py` defines valid transitions but they're never enforced in production — all transitions use direct `db.update_task()`.

## Code Conventions

- All async functions use `async/await` (asyncio-based)
- Database operations return dicts or dataclass instances from `models.py`
- Commands return structured dicts with `success` boolean and data/error fields
- Notifications go through `_notify_channel()` for project-aware Discord routing
- Git operations are wrapped in `GitManager` with `GitError` exceptions
- Config values use dataclasses with sensible defaults
