# Agent Queue

**Put your AI agents to work. Go touch grass.**

Agent Queue is a task queue and orchestrator built for subsidized AI coding plans. It keeps one or more Claude Code agents busy across all your projects, automatically recovers from rate limits, and queues the next task before the current one finishes.

## Quick Links

- **[Getting Started](getting-started.md)** — Installation and setup
- **[Architecture](architecture.md)** — How the system is designed
- **[Specifications](specs/models-and-state-machine.md)** — Detailed specs for each module
- **[API Reference](api/index.md)** — Auto-generated API documentation from source code

## How It Works

Agent Queue runs as a background daemon. You talk to it through a dedicated Discord channel — just type naturally, like you're texting a dev lead who has root access to your machine.

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

## Key Features

- **Parallel agent execution** — Multiple Claude Code agents work simultaneously across projects
- **Full task lifecycle management** — From creation through verification and PR
- **Discord integration** — Real-time streaming of agent output to threads
- **Token tracking** — Per-project, per-task usage breakdowns
- **Persistent state** — SQLite-backed, survives restarts
- **Rate limit aware** — Auto-pauses and resumes when throttle windows reset
