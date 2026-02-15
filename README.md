# Agent Queue

**A lightweight orchestration system for AI coding agents.**

Agent Queue keeps your AI agents working while you sleep, eat, or live your life. It manages Claude Code, Codex, Cursor, and Aider sessions across multiple projects — feeding them tasks, handling rate limits, and letting you direct everything from Discord on your phone.

## Why Agent Queue?

AI coding agents are powerful but babysitting them is a full-time job. You launch Claude Code on a task, it hits a rate limit, and work stops until you notice. You want three agents working on different features in parallel, but there's no way to coordinate them. You step away from your desk and lose visibility into what's happening.

Platforms like [OpenClaw](https://github.com/openclaw/openclaw) solve the general-purpose AI agent problem — connecting to your email, calendar, messaging apps, browser, and everything else. OpenClaw is impressive, but it's a Swiss Army knife when you need a scalpel. Its 145k-star ecosystem covers voice assistants, personal automation, cross-platform messaging, and enterprise governance. That's a lot of surface area when all you want is: **keep my coding agents productive on my projects.**

Agent Queue is that scalpel. It does one thing well:

- **Development-specific.** Task management with git branches, test verification, and merge conflict handling. Not email automation or calendar management.
- **Deterministic orchestration.** The scheduler is a pure function — no LLM calls to decide what to work on next. When tokens run out, the system pauses and self-recovers. No wasted credits on orchestration overhead.
- **Lightweight deployment.** A single Python process with SQLite. Runs on a Raspberry Pi. No Redis, no Kubernetes, no Docker required.
- **Agent-agnostic.** First-class support for Claude Code (via the Python SDK), Codex CLI, Cursor CLI, and Aider. Add new agent types by implementing a four-method interface.
- **Remote-first control.** Full project management from Discord. Create tasks, check status, approve work, and grant budgets from your phone while waiting in line at the grocery store.

## Features

### Deterministic Task State Machine

Every task follows a strict state machine with explicit transitions. No ambiguity, no stuck states, no deadlocks.

```
DEFINED → READY → ASSIGNED → IN_PROGRESS → VERIFYING → COMPLETED
                                    │
                          ┌─────────┼──────────┐
                          ▼         ▼          ▼
                       PAUSED   WAITING    FAILED
                       (auto-   _INPUT     (retry or
                       resume)  (Discord)   escalate)
```

**Deadlock prevention is guaranteed by design:**
- PAUSED tasks always have a resume timer — they never stall permanently
- Circular dependencies are rejected at creation time (DAG validation)
- Failed tasks retry up to a configurable limit, then escalate to you
- Subtask completion rolls up automatically to parent tasks

### Multi-Agent, Multi-Project

Run multiple agents across multiple projects simultaneously. Each agent gets its own git checkout — just like a real team of developers, each working in their own copy of the repo.

```
~/agent-queue-workspaces/
  ├── project-alpha/
  │   ├── agent-1/repo/     ← claude working on auth
  │   └── agent-2/repo/     ← codex working on API
  └── project-beta/
      └── agent-3/repo/     ← aider working on tests
```

### Proportional Effort Allocation

Assign weights to projects instead of hard token budgets. Agent Queue automatically distributes work proportionally:

```
Project Alpha:  weight 3  →  75% of agent time
Project Beta:   weight 1  →  25% of agent time
```

The scheduler tracks a rolling usage window and favors projects below their target ratio. A **minimum task guarantee** ensures even a 5%-weight project gets at least one task completed per window — no starvation.

Optional hard budgets are available at global and per-project levels if you want spending caps.

### Smart Token & Rate Limit Management

Agent Queue handles the most frustrating part of working with AI agents — running out of tokens:

- **Rate limit detection**: When an agent hits a rate limit, work pauses automatically. A timer fires when the window resets, and work resumes. Zero manual intervention.
- **Token tracking**: Usage is recorded per project, per agent, per task. You always know where your credits are going.
- **Graceful degradation**: Token exhaustion pauses the task and frees the agent for other work. When tokens are replenished, the task picks up where it left off.
- **No wasted tokens on orchestration**: The scheduler is pure arithmetic. Deciding what to work on next costs zero tokens.

### Discord Control Plane

Manage everything from Discord — designed to work from your phone.

**Slash commands:**
```
/project create my-app --weight 3
/task create my-app "Implement user authentication"
  --description "Add JWT-based auth with login/signup endpoints"
  --test-command "pytest tests/test_auth.py"
  --priority 50
/agent add claude-1 claude --model claude-opus-4-6
/status
/budget status
```

**Natural language fallback:**
```
You:  "add a high priority task to my-app for rate limiting,
       it depends on the API refactor"

Bot:  Interpreted as:
      /task create my-app "Implement rate limiting"
        --depends-on task-47 --priority 10
      Execute? (👍 to confirm)
```

**Agent questions forwarded to you:**
```
❓ [my-app] agent-1 asks (task-52 "Implement rate limiting"):
   "Should rate limiting be per-user or per-API-key?"

Reply in this thread to answer.
```

**Task results posted automatically:**
```
✓ Task-52 "Implement rate limiting" — COMPLETED
  Agent: agent-1 (claude) | Branch: task-52/implement-rate-limiting
  Tokens: 18,420 | Files: 3 changed
  Tests: 14 passed, 0 failed
```

### Git Workflow

Agent Queue manages the full git lifecycle for each task:

1. **Assignment**: Fetches latest, creates a task branch (`task-52/implement-rate-limiting`)
2. **Work**: Agent commits on the branch
3. **Verification**: Pushes branch, runs tests
4. **Completion**: Merges to main (or escalates conflicts as a new task)

Merge conflicts are handled deterministically — if auto-merge fails, the orchestrator creates a high-priority conflict resolution task and notifies you on Discord.

### Flexible Verification

Each task specifies how completion is verified:

- **auto_test**: Run test commands. All pass = done.
- **qa_agent**: A separate agent reviews the work and approves or rejects.
- **human**: Results posted to Discord. You approve or reject from your phone.

### Crash Recovery

Agent Queue survives restarts without losing state:

- All state is persisted to SQLite with atomic transactions
- On startup, reconciles database state with actual running processes
- Dead agent processes are detected and their tasks rescheduled
- Paused tasks with expired timers are automatically resumed
- Graceful shutdown preserves partial work on task branches

### Agent Adapters

Four agent types supported out of the box:

| Agent | Interface | Structured Output | Session Resume |
|-------|-----------|-------------------|----------------|
| **Claude Code** | Python SDK (`claude-agent-sdk`) | Typed message stream | Yes |
| **Codex** | CLI (`codex exec --json`) | NDJSON events | Yes |
| **Cursor** | CLI (`cursor --print`) | JSON | No |
| **Aider** | CLI (`aider --message`) | Text | No |

Adding a new agent type means implementing four async methods:

```python
class MyAdapter(AgentAdapter):
    async def start(self, task: TaskContext) -> None: ...
    async def wait(self) -> AgentOutput: ...
    async def stop(self) -> None: ...
    async def is_alive(self) -> bool: ...
```

## Getting Started

### Prerequisites

- Python 3.12+
- A Discord bot token ([create one here](https://discord.com/developers/applications))
- At least one AI agent CLI installed (Claude Code, Codex, Cursor, or Aider)

### Installation

```bash
git clone https://github.com/yourusername/agent-queue.git
cd agent-queue
pip install -e ".[dev]"
```

### Configuration

Create `~/.agent-queue/config.yaml`:

```yaml
workspace_dir: ~/agent-queue-workspaces

discord:
  bot_token: ${DISCORD_BOT_TOKEN}
  guild_id: "your-guild-id"
  authorized_users:
    - "your-discord-user-id"

scheduling:
  rolling_window_hours: 24
  min_task_guarantee: true
```

Set your environment variables:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
export ANTHROPIC_API_KEY="your-api-key"     # for Claude agents
export OPENAI_API_KEY="your-api-key"        # for Codex agents
```

### Register Agents

Create `~/.agent-queue/agents.yaml`:

```yaml
agents:
  - name: claude-1
    type: claude
    config:
      model: claude-sonnet-4-20250514
      permission_mode: acceptEdits
      allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]

  - name: codex-1
    type: codex
    config:
      model: gpt-5-codex
      sandbox: workspace-write
```

### Run

```bash
agent-queue
# or with a custom config path:
agent-queue /path/to/config.yaml
```

### Quick Start via Discord

Once the bot is running and connected to your server:

```
/project create my-app --weight 1
/agent add claude-1 claude
/task create my-app "Set up project scaffolding with FastAPI"
  --description "Create a basic FastAPI app with health check endpoint"
  --test-command "pytest tests/ -v"
/status
```

Agent Queue will assign the task to `claude-1`, create a git branch, run the agent, verify tests pass, and post the results back to Discord.

## Architecture

```
┌─────────────────────────────────────────────┐
│              asyncio event loop             │
│                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │ Discord  │  │Scheduler │  │Heartbeat │  │
│  │  Bot     │  │          │  │ Monitor  │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
│       └──────────────┼──────────────┘        │
│                      ▼                       │
│              ┌──────────────┐                │
│              │  Event Bus   │                │
│              └──────┬───────┘                │
│       ┌─────────────┼─────────────┐          │
│       ▼             ▼             ▼          │
│  ┌─────────┐  ┌──────────┐  ┌─────────┐    │
│  │ Agent 1 │  │ Agent 2  │  │ Agent 3 │    │
│  │ (Claude)│  │ (Codex)  │  │ (Aider) │    │
│  └─────────┘  └──────────┘  └─────────┘    │
│                                             │
│                 SQLite DB                    │
└─────────────────────────────────────────────┘
```

Single process. No external dependencies beyond SQLite. Runs on a Raspberry Pi 5.

## Project Structure

```
agent-queue/
  ├── src/
  │   ├── main.py              — entry point
  │   ├── config.py            — YAML config loading
  │   ├── database.py          — SQLite persistence
  │   ├── models.py            — data models & enums
  │   ├── state_machine.py     — task & agent state machines
  │   ├── scheduler.py         — deterministic scheduling
  │   ├── orchestrator.py      — core orchestration loop
  │   ├── event_bus.py         — async event system
  │   ├── adapters/            — agent type implementations
  │   ├── discord/             — bot, commands, notifications
  │   ├── git/                 — checkout & branch management
  │   └── tokens/              — budget & rate limit tracking
  └── tests/                   — exhaustive state machine + integration tests
```

## License

MIT
