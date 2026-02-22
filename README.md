# Agent Queue

**Put your AI agents to work. Go touch grass.**

If you're on Claude Max (or any subsidized plan with a big token budget), you're probably leaving most of it on the table. The tokens reset every few hours whether you used them or not. The throttle lifts at 3am and there's nobody at the keyboard. You finish a task, alt-tab away for ten minutes, and the agent sits idle. That's the real cost — not the tokens you spend, but the ones you waste by not having work queued up.

Agent Queue is a task queue and orchestrator built specifically around this constraint. It keeps one or more Claude Code agents busy across all your projects, automatically recovers from rate limits, and queues the next task before the current one finishes. When the throttle window resets, work resumes immediately — whether you're awake or not.

You manage everything from Discord on your phone. Queue up a week's worth of tasks before you leave the house. Come back to a stack of completed PRs.

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

Subsidized Claude plans (Max, Team, etc.) give you a large rolling token budget that resets on a schedule. The catch: it's throttled. You hit the limit, you wait. The window resets, you're back.

Most people treat this as a frustrating constraint. Agent Queue treats it as a design target. The system is built around the assumption that **your agents will hit rate limits, and work must continue anyway.** When a task stalls, Agent Queue pauses it, frees the agent, and picks up another task that isn't throttled. When the window resets, the paused task automatically resumes. No intervention required — the only thing that stops your agents from working is running out of tasks to give them.

The scheduler is also token-aware by design: zero LLM calls for orchestration decisions. Every token the system spends is a token your agent spends on actual work.

Platforms like [OpenClaw](https://github.com/openclaw/openclaw) solve the general-purpose AI agent problem — connecting to email, calendars, browsers, and everything else. That's impressive, but it's a Swiss Army knife when you need a scalpel. Agent Queue does one thing: **keep your coding agents saturating their token budget on your projects.**

- **Built for throttled plans.** Auto-pauses on rate limits, auto-resumes when the window resets. Works overnight, works while you're out, works while you sleep.
- **Development-specific.** Git branches, test verification, merge conflict handling. Not calendar automation.
- **Zero orchestration overhead.** No LLM calls to decide what to work on next. Every token goes to your agents.
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

### Hooks — Automated Self-Improvement

Hooks let Agent Queue monitor itself and react automatically. A hook is: **trigger → gather context → send prompt to LLM with all existing tools**. No code changes needed for new use cases — just create a new hook config via Discord.

```
You:     create a hook that runs tests every 2 hours and creates
         tasks for any failures

Bot:     ✅ Hook "test-watcher" created for my-app
         Trigger: every 2 hours
         When tests pass, the hook short-circuits (zero tokens).
         When tests fail, the LLM creates fix tasks automatically.
```

**Trigger types:**
- **Periodic** — Run on a schedule (every N seconds)
- **Event-driven** — Fire when something happens (`task_completed`, `task_failed`, etc.)

**Context steps** gather data before prompting the LLM:

| Step type | What it does |
|-----------|-------------|
| `shell` | Run a command, capture stdout/stderr/exit code |
| `read_file` | Read file contents |
| `http` | Make an HTTP request |
| `db_query` | Run a named query (safe, not raw SQL) |
| `git_diff` | Get diff output |

**Short-circuit conditions** skip the LLM call (zero tokens) when everything is fine:
- `skip_llm_if_exit_zero` — Tests pass? No action needed.
- `skip_llm_if_empty` — No log output? Nothing to analyze.
- `skip_llm_if_status_ok` — Health check returns 200? All good.

**Example hooks:**

```
# Self-healing test suite — runs pytest, creates tasks for failures
You:     create a hook for my-app called "test-watcher" that runs
         "cd ~/code/my-app && python -m pytest 2>&1" every 2 hours,
         skips the LLM if tests pass, and creates tasks for failures

# Log analyzer — scans daemon logs for errors
You:     create a hook that tails the last 500 lines of the daemon
         log every hour and creates tasks for real errors

# Post-task reviewer — reviews every completed task
You:     create a hook that fires on task_completed events and
         reviews the results for regressions

# Health check — pings a service endpoint
You:     create a hook that checks http://localhost:8080/health
         every 5 minutes, skips if healthy, creates a task if down
```

**Managing hooks:**
```
You:     list hooks for my-app
Bot:     2 hooks:
         • test-watcher (periodic, every 2h) — enabled
         • log-analyzer (periodic, every 1h) — enabled

You:     show recent runs for test-watcher
Bot:     Last 5 runs:
         • 2h ago — skipped (tests passed)
         • 4h ago — skipped (tests passed)
         • 6h ago — completed, created task "Fix test_auth failure"

You:     fire test-watcher now
Bot:     ✅ Hook fired manually. Running now.

You:     disable test-watcher
Bot:     ✅ Hook "test-watcher" disabled.
```

The prompt template uses `{{step_0}}`, `{{step_1}}` for context step outputs and `{{event}}`, `{{event.task_id}}` for event data. The LLM has access to all existing tools (`create_task`, `list_tasks`, etc.), so it decides what action to take — no hard-coded monitor types.

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

- **Rate limit detection and recovery.** When an agent hits the throttle, the task pauses automatically. A timer fires when the window resets and work resumes — no manual intervention, no missed windows.
- **Saturate your budget across multiple agents.** While one agent is throttled, others keep working on different projects or tasks.
- Per-project and global hard limits for pay-as-you-go API keys
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

hook_engine:
  enabled: true
  max_concurrent_hooks: 2
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
  │   ├── hooks.py             — generic hook engine (self-improvement)
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
