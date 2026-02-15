# Agent Queue — Design Document

**Date**: 2026-02-15
**Status**: Approved

## Overview

Agent Queue is a lightweight agent orchestration system for remote administration of AI coding agents. It keeps agents fed with work, manages token budgets, handles rate limits, and provides full control via Discord chat from anywhere in the world.

**Key principles:**
- Orchestration requires zero LLM calls — all scheduling is deterministic
- Token exhaustion pauses work gracefully; the system self-recovers
- Multiple agents work in parallel on different tasks across multiple projects
- Supports heterogeneous agent types: Claude (via SDK), Codex, Cursor, Aider

**Architecture**: Event-driven state machine in a single Python asyncio process. SQLite for persistence. Discord bot for the control plane. Agent adapters abstract away tool differences.

**Deployment**: Local machine or Raspberry Pi 5, with future cloud deployment possible.

---

## 1. Task State Machine

Every task follows a deterministic state machine with explicit transitions. No LLM is needed to determine the next state.

### States

| State | Description |
|-------|-------------|
| DEFINED | Task created, may be missing info or have unmet dependencies |
| READY | All prerequisites met, eligible for scheduling |
| ASSIGNED | Scheduler has picked an agent, agent is being launched |
| IN_PROGRESS | Agent is actively working |
| WAITING_INPUT | Agent asked a question, waiting for human reply via Discord |
| PAUSED | Tokens exhausted or rate limited, will auto-resume via timer |
| VERIFYING | Work done, running verification (auto-test, QA agent, or human review) |
| COMPLETED | Verified and done |
| FAILED | Verification failed, returned to READY for retry |
| BLOCKED | Max retries exhausted, requires human intervention |

### Transition Table

```
(DEFINED,       DEPS_MET)          → READY
(READY,         ASSIGNED)          → ASSIGNED
(ASSIGNED,      AGENT_STARTED)     → IN_PROGRESS
(IN_PROGRESS,   AGENT_COMPLETED)   → VERIFYING
(IN_PROGRESS,   AGENT_FAILED)      → FAILED
(IN_PROGRESS,   TOKENS_EXHAUSTED)  → PAUSED
(IN_PROGRESS,   AGENT_QUESTION)    → WAITING_INPUT
(WAITING_INPUT, HUMAN_REPLIED)     → IN_PROGRESS
(WAITING_INPUT, INPUT_TIMEOUT)     → PAUSED
(PAUSED,        RESUME_TIMER)      → READY
(VERIFYING,     VERIFY_PASSED)     → COMPLETED
(VERIFYING,     VERIFY_FAILED)     → FAILED
(FAILED,        RETRY)             → READY
(FAILED,        MAX_RETRIES)       → BLOCKED
```

All other (state, event) pairs are invalid and rejected.

### Deadlock Prevention

1. Tasks with incomplete subtasks cannot transition to VERIFYING — subtasks must all be COMPLETED first
2. PAUSED tasks have a mandatory retry timer — they always transition back to READY
3. Circular dependencies are rejected at task creation time (DAG validation via DFS)
4. FAILED tasks retry up to a configurable limit, then enter BLOCKED requiring human intervention

### Verification Types (per task)

- **auto_test**: Run defined test commands. Pass = COMPLETED.
- **qa_agent**: A separate agent reviews the work and approves/rejects.
- **human**: Post results to Discord, wait for human `/task approve` or `/task reject`.

---

## 2. Agent State Machine & Adapter Interface

### Agent States

| State | Description |
|-------|-------------|
| IDLE | Available for work |
| STARTING | Launching process, setting up checkout |
| BUSY | Actively working on a task |
| PAUSED | Rate limited or token exhaustion, waiting |
| ERROR | Crashed, pending cleanup then return to IDLE |

### Agent Record

- `id`, `name`, `agent_type` (claude, codex, cursor, aider)
- `state`, `current_task_id`, `checkout_path`, `pid`
- `last_heartbeat`, `total_tokens_used`, `session_tokens_used`

### Adapter Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

class AgentResult(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED_TOKENS = "paused_tokens"
    PAUSED_RATE_LIMIT = "paused_rate_limit"

@dataclass
class TaskContext:
    description: str
    acceptance_criteria: list[str]
    test_commands: list[str]
    checkout_path: str
    branch_name: str
    attached_context: list[str]
    mcp_servers: list[dict]
    tools: list[str]

@dataclass
class AgentOutput:
    result: AgentResult
    summary: str
    files_changed: list[str]
    tokens_used: int
    error_message: str | None

class AgentAdapter(ABC):
    @abstractmethod
    async def start(self, task: TaskContext) -> None: ...

    @abstractmethod
    async def wait(self) -> AgentOutput: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def is_alive(self) -> bool: ...
```

### Adapter Implementations

- **ClaudeAdapter**: Uses `claude-agent-sdk` async `query()` generator, streams messages, detects token exhaustion from exceptions, supports session resume
- **CodexAdapter**: Spawns `codex exec --json`, parses NDJSON stdout
- **CursorAdapter**: Spawns `cursor --print --output-format json`, parses output
- **AiderAdapter**: Spawns `aider --message --yes`, parses stdout text

---

## 3. Project & Scheduling Model

### Project Structure

```python
@dataclass
class Project:
    id: str
    name: str
    repos: list[RepoConfig]
    credit_weight: float          # relative share of tokens (default 1.0)
    max_concurrent_agents: int
    status: ProjectStatus         # ACTIVE, PAUSED, ARCHIVED
```

### Proportional Effort Allocation

Credits are divided by weight, not fixed amounts:

```
Project A: weight 3  →  3/(3+1) = 75%
Project B: weight 1  →  1/(3+1) = 25%
```

The scheduler tracks a rolling usage window (default 24h) and favors the project furthest below its target ratio.

### Minimum Task Guarantee

A project with READY tasks is guaranteed at least one task completion per window, regardless of its weight. This prevents small-allocation projects from being starved.

### Scheduler Algorithm

Runs on every event. Pure function, no LLM calls:

```python
def schedule(state):
    for agent in idle_agents:
        candidates = projects_with_ready_tasks
        candidates.sort(key=lambda p: actual_ratio / target_ratio)
        for project in candidates:
            if global_budget_exhausted: continue
            if project.budget_exceeded: continue    # optional per-project limit
            if project.at_max_agents: continue
            task = highest_priority_ready_task(project)
            if task: assign(agent, task); break
```

### Deadlock Freedom Properties

1. No circular waits — task dependency graph validated as DAG
2. No hold-and-wait — agents work on exactly one task
3. Preemption via PAUSE — token exhaustion pauses, doesn't hold resources
4. Bounded retry — failed tasks escalate to human after N retries
5. Timer-based recovery — PAUSED always has a resume timer

### Task Priority Within a Project

1. Dependencies met (blocked tasks are ineligible)
2. Numeric priority (lower = higher priority)
3. Creation order (FIFO tiebreaker)

---

## 4. Data Model (SQLite)

```sql
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credit_weight REAL NOT NULL DEFAULT 1.0,
    max_concurrent_agents INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    budget_limit INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE repos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    url TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    checkout_base_path TEXT NOT NULL
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    parent_task_id TEXT REFERENCES tasks(id),
    repo_id TEXT REFERENCES repos(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'DEFINED',
    verification_type TEXT NOT NULL DEFAULT 'auto_test',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    assigned_agent_id TEXT REFERENCES agents(id),
    branch_name TEXT,
    resume_after TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_criteria (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);

CREATE TABLE task_context (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    label TEXT,
    content TEXT NOT NULL
);

CREATE TABLE task_tools (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    config TEXT NOT NULL
);

CREATE TABLE agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'IDLE',
    current_task_id TEXT REFERENCES tasks(id),
    checkout_path TEXT,
    pid INTEGER,
    last_heartbeat TIMESTAMP,
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    session_tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE token_ledger (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    tokens_used INTEGER NOT NULL,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id),
    task_id TEXT REFERENCES tasks(id),
    agent_id TEXT REFERENCES agents(id),
    payload TEXT,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE rate_limits (
    id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    limit_type TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    current_tokens INTEGER NOT NULL DEFAULT 0,
    window_start TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

---

## 5. Discord Interface

### Channel Structure

```
#agent-queue/
  ├── #control         — commands, natural language, system responses
  ├── #notifications   — auto-posted status updates
  ├── #agent-questions — agent questions awaiting human response
  └── #project-{name}  — task results, project-specific updates
```

### Command Set

**Projects**: `/project create|list|status|pause|resume|set|archive`
**Tasks**: `/task create|list|show|edit|cancel|retry|approve|reject`
**Agents**: `/agent add|list|status|pause|resume|remove`
**Budget**: `/budget status|grant|set-rate-limit`
**System**: `/status`, `/pause`, `/resume`, `/logs`, `/system reload-config`

### Natural Language Fallback

Messages that don't match a slash command are routed to a lightweight LLM (claude-haiku) that maps natural language to system commands. The bot posts interpreted commands for confirmation before executing.

### Agent-to-Human Questions

When an agent needs human input:
1. Task moves to WAITING_INPUT
2. Bot posts the question to #agent-questions with project/task context
3. Human replies in the thread
4. Reply is forwarded to the agent, task resumes to IN_PROGRESS
5. Configurable timeout moves task to PAUSED if no reply

### Task Results

Completed tasks post structured results to the project channel: agent used, branch, tokens consumed, summary of changes, files changed, test results.

### Multi-line Task Creation

`/task create` opens a thread for adding description, criteria, and context in follow-up messages. Phone-friendly.

### Permission Model

Configured list of authorized Discord user IDs. No role hierarchy for MVP.

---

## 6. Git Workflow & Workspace Management

### Checkout Layout

Each agent gets a full git clone per repo:

```
~/agent-queue-workspaces/
  ├── project-alpha/
  │   ├── agent-1/my-repo/
  │   └── agent-2/my-repo/
  └── project-beta/
      └── agent-1/backend/
```

Workspace directory defaults to `~/agent-queue-workspaces/`, configurable in system config.

### Task Git Lifecycle

1. **Assignment**: orchestrator fetches, checks out default branch, pulls, creates task branch (`<task-id>/<slugified-title>`)
2. **Work**: agent makes commits on the task branch
3. **Verification**: orchestrator pushes branch, runs verification
4. **Completion**: merge to default branch (or escalate conflicts), push, notify other agents to pull
5. **Failure/Retry**: branch preserved, next attempt continues on same branch with failure context

### Conflict Handling

- Fast-forward or auto-merge: proceed
- Conflict: abort merge, post details to Discord, create a conflict resolution task with high priority

### Multi-Repo Projects

Tasks specify which repo they target. The orchestrator sets the agent's working directory to the correct checkout.

---

## 7. Token & Rate Limit Management

### Proportional Effort (Default)

Weights control relative effort allocation. The scheduler tracks a rolling usage window and favors projects below their target ratio. No hard limits unless explicitly configured.

### Minimum Task Guarantee

Any project with READY tasks gets at least one task completed per window, regardless of its weight share.

### Three Optional Tiers

- **Tier 0 (default)**: Weights only. No hard limits. Proportional scheduling.
- **Tier 1**: Global daily token budget. All work pauses when hit.
- **Tier 2**: Per-project hard cap (opt-in per project).

### Rate Limit Handling

Per agent type, configurable windows (per-minute, per-hour, per-day). When hit:
1. Agent and task pause
2. Timer set for window reset
3. Auto-resume when window resets

### Token Tracking by Agent Type

| Agent | Method |
|-------|--------|
| Claude SDK | Usage metadata in message stream |
| Codex | JSON output events, fallback to estimation |
| Cursor | JSON output, fallback to estimation |
| Aider | `/tokens` command, fallback to estimation |

---

## 8. Process Lifecycle & Recovery

### Startup Sequence

1. Load/create SQLite database
2. Recovery: reconcile DB state with actual running processes (dead PIDs → reset agents/tasks, expired pauses → READY)
3. Validate agent checkouts
4. Start asyncio loop: Discord bot, scheduler, heartbeat monitor, timers

### Crash Detection

- PID monitoring every 30 seconds
- Adapter-level heartbeat (stream activity for Claude SDK, process.poll() for subprocesses)
- Configurable stuck timeout (default 10 minutes)

### Graceful Shutdown

SIGTERM/SIGINT: stop scheduling, signal agents to stop, wait 30s, force kill, persist state, exit. Tasks marked PAUSED (not READY) to preserve partial work.

### State Consistency

All state transitions are atomic SQLite transactions. Task + agent state always updated together. Crash mid-transaction → SQLite rolls back → recovery finds consistent state.

### Event Loop Architecture

Single asyncio process. Discord bot, scheduler, heartbeat monitor, and agent monitors all run as async tasks communicating via an in-process event bus (asyncio queues and callbacks).

---

## 9. Configuration

### System Config: `~/.agent-queue/config.yaml`

Covers: workspace directory, database path, Discord settings, NL parser model, agent defaults (heartbeat interval, stuck timeout, shutdown timeout), rate limits, scheduling parameters (rolling window, min task guarantee), optional global budget, pause retry intervals.

### Agent Definitions: `~/.agent-queue/agents.yaml`

Registers agents with type-specific configuration (model, permissions, tools, sandbox settings).

### Project Definitions: `~/.agent-queue/projects.yaml`

Seeds projects with repos, weights, and concurrency limits.

### Config Hierarchy

YAML files are seed data, loaded on first startup. SQLite is the runtime source of truth. Discord commands modify the database. `/system reload-config` re-reads YAML files.

### Secrets

Referenced via `${ENV_VAR}` syntax in YAML. Never stored in config files.

### Project Source Layout

```
agent-queue/
  ├── src/
  │   ├── main.py
  │   ├── config.py
  │   ├── database.py
  │   ├── models.py
  │   ├── state_machine.py
  │   ├── scheduler.py
  │   ├── event_bus.py
  │   ├── adapters/
  │   │   ├── base.py
  │   │   ├── claude.py
  │   │   ├── codex.py
  │   │   ├── cursor.py
  │   │   └── aider.py
  │   ├── discord/
  │   │   ├── bot.py
  │   │   ├── commands.py
  │   │   ├── nl_parser.py
  │   │   └── notifications.py
  │   ├── git/
  │   │   └── manager.py
  │   └── tokens/
  │       ├── tracker.py
  │       └── budget.py
  ├── tests/
  │   ├── test_state_machine.py
  │   ├── test_scheduler.py
  │   ├── test_budget.py
  │   └── ...
  ├── docs/plans/
  ├── pyproject.toml
  └── README.md
```

---

## 10. Testing Strategy

### State Machine Tests (Critical)

Exhaustive transition matrix: parametrized test for every valid `(state, event) → new_state` pair, and every invalid pair asserts rejection. Covers deadlock freedom properties, subtask rollup, DAG validation.

### Scheduler Tests

Proportional allocation convergence, minimum task guarantee, global budget enforcement, dependency ordering.

### Recovery Tests

Crash mid-task (dead PID recovery), WAITING_INPUT persistence across restarts, expired PAUSED timer recovery.

### Integration Tests

Full task lifecycle with mock agent adapters. Pause/resume cycles. Multi-project scheduling. Budget exhaustion and grant flows.

### Manual Testing

Real agent adapter smoke tests, Discord bot interaction, git operations on real repos.
