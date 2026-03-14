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
| `src/setup_wizard.py` | Interactive setup CLI — Discord, API keys, first-run configuration |
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

## Spec-Driven Development

This project follows a **spec-driven development workflow**. Specifications are the source of truth for all functionality.

### Flow

```
specs/ (source of truth)  →  src/ (implementation)  →  tests/ (verification)  →  docs/ (generated)
```

1. **Specs first:** All changes start by updating or creating a spec in `specs/`
2. **Code from specs:** Implementation in `src/` is written to satisfy the spec
3. **Tests from specs:** Tests in `tests/` verify the spec's behavioral contracts
4. **Docs from code:** Documentation in `docs/` is generated from the implemented code

### Specs Folder Structure

The `specs/` folder mirrors `src/` structure. Each spec covers one or more closely-related source files:

```
specs/
├── models-and-state-machine.md    — Data models, enums, state transitions, DAG validation
├── config.md                      — YAML config loading, env var substitution, all config fields
├── main.md                        — Entry point, process lifecycle, signal handling, restart
├── database.md                    — SQLite schema (14 tables), all CRUD operations, migrations
├── orchestrator.md                — Central loop, task lifecycle, workspace management, plan generation
├── scheduler-and-budget.md        — Credit-weight scheduling, token budgets, rate limit windows
├── command-handler.md             — All 50+ commands, input/output contracts, error handling
├── chat-agent.md                  — LLM tool definitions, conversation loop, history compaction
├── event-bus.md                   — Async pub/sub, wildcard subscriptions
├── plan-parser.md                 — Plan file discovery, regex + LLM parsing, step extraction
├── hooks.md                       — Hook engine, triggers, context steps, LLM invocation, cooldowns
├── adapters/
│   └── claude.md                  — AgentAdapter interface, Claude Code SDK integration
├── chat-providers/
│   └── providers.md               — ChatProvider interface, Anthropic + Ollama implementations
├── discord/
│   └── discord.md                 — Bot, slash commands, notifications, channel routing, auth
├── git/
│   └── git.md                     — GitManager operations, worktrees, PR management
└── setup-wizard.md                — Interactive setup CLI, connectivity tests, config generation
```

### Spec Conventions

- **Plain English:** Specs describe *what* the system does, not *how* the code is structured
- **Behavioral:** Focus on inputs, outputs, side effects, and invariants
- **Complete enough to reimplement:** A developer should be able to rewrite the code from the spec alone
- **High-level:** Don't mirror code line-by-line — describe functionality and contracts
- **Authoritative:** When spec and code disagree, the spec is correct and code should be updated

### When to Update Specs

- **Adding a feature:** Write the spec first, then implement
- **Fixing a bug:** If the bug reveals a missing spec detail, update the spec
- **Refactoring:** Update the spec only if the external behavior changes
- **Deleting code:** Remove the corresponding spec section

## Code Conventions

- All async functions use `async/await` (asyncio-based)
- **Async-first I/O:** All production code uses non-blocking I/O. Git operations use `GitManager`'s async API (`a`-prefixed methods) which use `asyncio.create_subprocess_exec()`. No production code calls synchronous `subprocess.run()` — sync methods are retained only for tests.
- Database operations return dicts or dataclass instances from `models.py`
- Commands return structured dicts with `success` boolean and data/error fields
- Notifications go through `_notify_channel()` for project-aware Discord routing
- Git operations are wrapped in `GitManager` with `GitError` exceptions
- Config values use dataclasses with sensible defaults
