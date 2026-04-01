# Agent Queue - Requirements

## Task Management

- **R1.1:** Tasks must transition through a defined state machine: DEFINED > READY > ASSIGNED > IN_PROGRESS > VERIFYING > COMPLETED, with PAUSED, FAILED, BLOCKED, and AWAITING_APPROVAL as side states
- **R1.2:** Tasks in DEFINED must auto-promote to READY only when all declared dependencies are COMPLETED
- **R1.3:** Dependency graphs must be validated for cycles at creation time; circular dependencies must be rejected
- **R1.4:** Failed tasks must retry automatically up to `max_retries` (default 3) before transitioning to BLOCKED
- **R1.5:** PAUSED tasks must always carry a `resume_after` timestamp and auto-resume — no task may stall permanently
- **R1.6:** Users must be able to create, edit, cancel, restart, skip, archive, and restore tasks
- **R1.7:** Plan files (`.claude/plan.md`) produced by agents must be parsed into chained subtasks with correct dependency ordering
- **R1.8:** Task priority must be respected within each project — lower number executes first

## Scheduling & Rate Limits

- **R2.1:** The scheduler must use proportional credit-weight allocation across projects within a configurable rolling window
- **R2.2:** Per-project `max_concurrent_agents` must be enforced — never exceed the configured cap
- **R2.3:** Rate limit detection must auto-pause affected agents and auto-resume when the token window resets
- **R2.4:** Scheduling decisions must use zero LLM calls — purely deterministic logic
- **R2.5:** Global and per-project token budgets must be enforced when configured

## Agent & Workspace Management

- **R3.1:** Each task must execute in an isolated workspace (separate clone or git worktree)
- **R3.2:** Workspace locks must prevent two agents from working in the same directory simultaneously
- **R3.3:** Dead agents must be detected via heartbeat timeout and their tasks rescheduled
- **R3.4:** The adapter interface must support pluggable agent types (Claude Code, Codex, Cursor, Aider)
- **R3.5:** Agent profiles must allow per-task configuration of model, tools, MCP servers, and system prompt

## Git Integration

- **R4.1:** Task branches must be created from the latest remote main before work begins
- **R4.2:** Commits, pushes, and PR creation must be automatic on task completion
- **R4.3:** Merging to main must require explicit user approval (AWAITING_APPROVAL state)
- **R4.4:** Push operations must use `--force-with-lease` for safe idempotent retries
- **R4.5:** Merge conflicts must be detected and reported to the user, never silently resolved
- **R4.6:** Workspace sync operations must be available to bring all workspaces to latest main

## Discord Interface

- **R5.1:** All system operations must be available via both slash commands and natural language chat
- **R5.2:** Each task must get a dedicated Discord thread with live-streamed agent output
- **R5.3:** Users must be able to reply in task threads to provide input to waiting agents
- **R5.4:** Project notifications must route to the correct per-project Discord channel
- **R5.5:** An authorization whitelist must restrict who can issue commands
- **R5.6:** Status dashboards must show task counts, agent states, and queue depth at a glance

## Supervisor (Chat Agent)

- **R6.1:** The Supervisor must translate natural language into CommandHandler tool calls via multi-turn LLM conversation
- **R6.2:** All Supervisor operations must delegate to CommandHandler — no direct database or git access
- **R6.3:** The tiered tool system must present only core tools by default and load additional categories on demand
- **R6.4:** The ChatObserver must passively monitor project channels and surface suggestions without interrupting workflow

## Hooks & Automation

- **R7.1:** Hooks must support three trigger types: periodic (interval/cron), event-driven (task.completed, etc.), and scheduled (one-shot)
- **R7.2:** Hook execution must invoke a full Supervisor with all tool access
- **R7.3:** Cooldown enforcement must prevent the same hook from firing more frequently than its configured minimum interval
- **R7.4:** A global concurrency cap (default 2) must limit simultaneous hook executions
- **R7.5:** Every hook run must be recorded with trigger reason, status, prompt, response, and token usage
- **R7.6:** Users must be able to manually fire hooks, bypassing cooldown but respecting concurrency

## Memory System

- **R8.1:** Each project must maintain a living `profile.md` that captures architecture, conventions, and decisions
- **R8.2:** After each completed task, an LLM call must revise the project profile based on what was learned
- **R8.3:** Context delivery must follow priority tiers: profile > project docs > notes > recent tasks > semantic search
- **R8.4:** Old task memories must compact into weekly digests to prevent unbounded growth
- **R8.5:** Memory system failures must never block task execution — all memory operations must be fault-tolerant

## Configuration & Setup

- **R9.1:** Configuration must live in a single YAML file (`~/.agent-queue/config.yaml`) with environment variable substitution
- **R9.2:** The setup wizard must guide first-time users through Discord token, API keys, and agent creation
- **R9.3:** The setup wizard must be idempotent — re-running must pre-fill existing values
- **R9.4:** Sensible defaults must be provided for all optional configuration

## Persistence & Reliability

- **R10.1:** All state must persist in SQLite (WAL mode) and survive process restarts
- **R10.2:** The system must run as a single Python asyncio process — no threads or multiprocessing required
- **R10.3:** Graceful shutdown must allow in-progress tasks to complete before exit
- **R10.4:** The system must support restart via `os.execv` while preserving PID for daemon managers

## Extensibility

- **R11.1:** The MCP server must auto-expose all CommandHandler commands as MCP tools
- **R11.2:** New agent adapters must be addable by subclassing `AgentAdapter` and registering in the factory
- **R11.3:** New LLM providers must be addable without modifying orchestration logic
- **R11.4:** The plugin system must allow third-party extensions to register tools and event handlers
