# Agent Queue

**Put your AI agents to work. Go touch grass.**

You've got Claude Code. It's incredible. But you're still babysitting it — watching the terminal, manually kicking off the next task, noticing when it stalls. Agent Queue fixes that. It runs your agents autonomously, in parallel, across all your projects, while you manage everything from Discord on your phone.

No more sitting at your desk waiting for a task to finish so you can start the next one. Set your agents loose, get a notification when the work's done, and review the diff — from anywhere.

## How it works

Agent Queue runs as a background daemon. You talk to it through a dedicated Discord channel — just type naturally, like you're texting a dev lead who has root access to your machine. It understands what you want and makes it happen.

```
You:     hey can you add a task to fix the login bug in my-app?
         the error is "JWT expired" in auth.py line 47

Bot:     Created task `task-89` — "Fix JWT expiry bug in auth.py"
         Assigned to claude-1. Branch: task-89/fix-jwt-expiry-bug
         I'll post updates in the task thread.

[5 minutes later, in the task thread]

Bot:     ✅ Task Complete — auth.py updated, tests passing
         18,420 tokens · 2 files changed
```

The bot is powered by Claude. It understands context, remembers what you were working on, and acts — it doesn't just give you instructions on what to do yourself.

## What it does

**Runs agents in parallel.** Multiple Claude Code agents work simultaneously across your projects. Each agent gets its own workspace — linked to your existing directories, complete with your `.env`, your `node_modules`, your `venv`. No setup overhead per agent.

**Manages the full task lifecycle.** Task created → agent assigned → branch created → work done → tests run → results posted. Retries on failure. Escalates to you when it's stuck. Never silently drops work.

**Streams agent output to Discord threads.** Each task gets its own thread. Watch your agents work in real time. Reply to unblock them if they have a question.

**Tracks token usage.** Per-project, per-task breakdowns. Know exactly where your API budget is going.

**Persists everything.** SQLite-backed. Survives restarts. Pick up exactly where you left off.

**Notes per project.** Keep a running markdown knowledge base for each project — architecture decisions, context, specs. Your agents can read and write notes too.

## Why Agent Queue?

Platforms like [OpenClaw](https://github.com/openclaw/openclaw) solve the general-purpose AI agent problem — connecting to email, calendars, browsers, and everything else. That's impressive, but it's a Swiss Army knife when you need a scalpel. Agent Queue does one thing: **keep your coding agents productive on your projects.**

- **Development-specific.** Git branches, test verification, merge conflict handling. Not calendar automation.
- **Deterministic scheduling.** No LLM calls to decide what to work on next. Zero tokens wasted on orchestration.
- **Lightweight.** One Python process, SQLite. Runs on a Raspberry Pi. No Redis, no Kubernetes.
- **You're in control.** Nothing merges, nothing deploys without you seeing it. Discord notifications keep you in the loop from your phone.

## Features

### Conversational Control from Discord

The primary interface is a Claude-powered bot in a dedicated Discord channel. Just talk to it.

```
You:     what's running?

Bot:     ## Status
         2 agents active:
         • claude-1 → task-89 "Fix JWT expiry bug"
         • claude-2 → task-91 "Add user settings page"
         3 tasks queued for my-app

You:     pause my-app while I'm on vacation

Bot:     Project my-app paused. 3 queued tasks will wait.

You:     actually show me what claude-1 finished yesterday

Bot:     [shows diff, test results, files changed for task-87]
```

The bot has tools to read files, run shell commands, search your codebase, and manage everything in the system — all accessible through natural conversation.

### Active Project Focus

Set a project as active and every command defaults to it — no need to specify the project every time.

```
You:     set my-app as active

Bot:     ✓ my-app is now the active project.

You:     add a task to add dark mode

Bot:     Created task in my-app: "Add dark mode support"
```

### Task Streaming into Threads

When an agent picks up a task, a Discord thread is created automatically. The agent's output streams into that thread in real time. If the agent has a question, it shows up in the thread and you can reply to unblock it.

```
[Thread: task-89 | Fix JWT expiry bug]

Agent is working...
→ Reading auth.py
→ Found the issue: token validation doesn't check exp claim
→ Writing fix...
→ Running tests: 14 passed ✓
```

### Repo Management — Link Your Existing Projects

Don't want to clone a fresh copy? Link your existing directory. The agent works right in your project, with all your environment already set up.

```
You:     link ~/code/my-app as the my-app repo

Bot:     ✓ Linked /home/jack/code/my-app as repo "my-app"
         Agents will work directly in this directory.
```

Three repo modes:
- **link** — Use an existing directory (your `.env`, `venv`, and `node_modules` included)
- **clone** — Clone a remote repo; agents get their own checkouts
- **init** — Start a new empty repo from scratch

### Agent Workspaces

Assign agents to repos as their permanent home base. Tasks go to agents automatically — no need to specify a repo per task.

```
You:     create agent claude-2 and assign it to the my-app repo

Bot:     ✓ Agent claude-2 created, workspace: my-app
```

For parallel work, link multiple checkouts of the same project and assign one agent to each. Each agent works in a fully-configured directory with its own environment.

### Project Notes

Keep a markdown knowledge base for each project, accessible from Discord. Great for specs, architecture notes, and context for your agents.

```
You:     write a note for my-app called "auth-plan" with the
         design for the new auth system

Bot:     ✓ Note "auth-plan" saved to my-app workspace.

You:     create tasks from the auth-plan note

Bot:     I read the note. Here's what I'd create:
         1. "Set up JWT validation middleware" — high priority
         2. "Add refresh token endpoint"
         3. "Update login flow to use new auth"
         Create all three? (yes/no)
```

Agents can also write notes — great for brainstorming tasks where you want the output saved.

### Deterministic Task State Machine

Every task follows a strict lifecycle. No ambiguity, no stuck states.

```
DEFINED → READY → ASSIGNED → IN_PROGRESS → VERIFYING → COMPLETED
                                   │
                         ┌─────────┼──────────┐
                         ▼         ▼          ▼
                      PAUSED   WAITING    FAILED
                      (auto-   _INPUT     (retry or
                      resume)  (Discord)  escalate)
```

- PAUSED tasks always have a resume timer — they never stall permanently
- Circular dependencies rejected at creation time
- Failed tasks retry up to a configurable limit, then escalate to you via Discord
- Subtask completion rolls up to parent tasks automatically

### Proportional Scheduling

Assign weights to projects. Agent Queue distributes work proportionally across them.

```
my-app:       weight 3  →  75% of agent time
side-project: weight 1  →  25% of agent time
```

The scheduler tracks a rolling window and favors projects below their target ratio. A minimum task guarantee ensures no project starves.

### Token Budget Management

- Per-project and global hard limits
- Rate limit detection: when an agent hits a limit, work pauses automatically and resumes when the window resets
- Token exhaustion pauses the task and frees the agent for other work
- All usage visible from Discord: `tell me the token breakdown for my-app`

### Crash Recovery

- All state persisted to SQLite with atomic transactions
- On restart: dead agents detected, their tasks rescheduled, timers resumed
- Graceful shutdown preserves partial work on task branches

### Multi-Provider Claude Support

Works with whatever Claude backend you have:

| Provider | How to enable |
|----------|---------------|
| **Anthropic (direct)** | Set `ANTHROPIC_API_KEY` |
| **AWS Bedrock** | Set `AWS_REGION` (uses existing AWS credentials) |
| **Google Vertex AI** | Set `GOOGLE_CLOUD_PROJECT` |

## Getting Started

### Prerequisites

- Python 3.12+
- A Discord bot token ([create one here](https://discord.com/developers/applications))
- Claude Code installed and configured

### Install

```bash
git clone https://github.com/yourusername/agent-queue.git
cd agent-queue
pip install -e ".[dev]"
```

### Setup Wizard

Run the interactive setup wizard — it walks you through Discord configuration, API keys, and getting your first agent running:

```bash
python setup_wizard.py
```

### Manual Configuration

Create `~/.agent-queue/config.yaml`:

```yaml
workspace_dir: ~/agent-queue-workspaces

discord:
  bot_token: ${DISCORD_BOT_TOKEN}
  guild_id: "your-guild-id"
  control_channel: "agent-queue"       # bot listens here
  notifications_channel: "agent-queue" # task updates posted here
  authorized_users:
    - "your-discord-user-id"

scheduling:
  rolling_window_hours: 24
  min_task_guarantee: true
```

Set your environment variables:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
export ANTHROPIC_API_KEY="your-api-key"
```

### Run

```bash
agent-queue
# or with a custom config path:
agent-queue /path/to/config.yaml
```

### First Steps in Discord

Once the bot is online in your server, everything happens through conversation in your control channel:

```
You:  link ~/code/my-app as my-app

Bot:  ✓ Linked. Repo "my-app" registered.

You:  create a project called my-app

Bot:  ✓ Project my-app created.

You:  create agent claude-1 and assign it to my-app

Bot:  ✓ Agent claude-1 created.

You:  add a task to add rate limiting to the API

Bot:  Created task `task-1` — "Add rate limiting to API"
      Assigned to claude-1. I'll post updates in the thread.
```

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
│  │ (Claude)│  │ (Claude) │  │ (Claude)│    │
│  └─────────┘  └──────────┘  └─────────┘    │
│                                             │
│                 SQLite DB                   │
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
  │   ├── adapters/            — Claude Code agent implementation
  │   ├── discord/             — bot, commands, notifications, NL tools
  │   ├── git/                 — checkout & branch management
  │   └── tokens/              — budget & rate limit tracking
  ├── setup_wizard.py          — interactive setup
  └── tests/                   — state machine + integration tests
```

## License

MIT
