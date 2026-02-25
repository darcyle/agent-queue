# Architecture

## Overview

Agent Queue is a single-process Python daemon that orchestrates AI coding agents through a Discord interface. The system is designed around the constraint of throttled AI API plans — maximizing token utilization by keeping agents busy and automatically recovering from rate limits.

## System Components

```
┌─────────────────────────────────────────────────────────┐
│                    Discord Interface                     │
│              (Bot + Commands + Notifications)            │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                     Chat Agent                          │
│            (Natural language → commands)                 │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    Orchestrator                          │
│           (Task lifecycle + agent management)            │
├─────────┬───────────┬──────────────┬────────────────────┤
│Scheduler│State Mach.│  Event Bus   │   Plan Parser      │
│         │           │              │                     │
└─────────┴───────────┴──────────────┴────────────────────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         ┌────────┐ ┌────────┐ ┌────────┐
         │Adapter │ │  Git   │ │Database│
         │(Claude)│ │Manager │ │(SQLite)│
         └────────┘ └────────┘ └────────┘
```

## Key Design Decisions

### Zero LLM Overhead for Orchestration

The scheduler and task routing use no LLM calls. Every token the system spends is a token an agent spends on actual work. Scheduling decisions are made via proportional credit-weight allocation.

### Spec-Driven Development

Each module has a corresponding specification in the `specs/` directory. These specs serve as the source of truth for behavior and are written in plain English describing *what* the module should do, not *how*.

### Async-First

All I/O operations use `asyncio`. The main event loop runs the Discord bot, the scheduling cycle, and agent monitoring concurrently.

### SQLite Persistence

All state is persisted to SQLite via `aiosqlite`. The system survives restarts and picks up exactly where it left off.

## Module Reference

| Module | Purpose |
|--------|---------|
| `src/main.py` | Entry point, signal handling, restart support |
| `src/orchestrator.py` | Core task/agent lifecycle management |
| `src/models.py` | Data models (Task, Agent, Project, Hook, etc.) |
| `src/database.py` | SQLite persistence layer (14 tables) |
| `src/config.py` | YAML config loading with environment variable substitution |
| `src/scheduler.py` | Proportional credit-weight scheduling |
| `src/state_machine.py` | Task state transitions and DAG validation |
| `src/event_bus.py` | Async pub/sub with wildcard support |
| `src/plan_parser.py` | Plan file parsing (regex + LLM) |
| `src/hooks.py` | Hook engine for automation |
| `src/chat_agent.py` | LLM-powered Discord conversation interface |
| `src/adapters/` | Agent adapter interface and implementations |
| `src/chat_providers/` | LLM provider abstraction (Anthropic, Ollama) |
| `src/discord/` | Discord bot, commands, and notifications |
| `src/git/` | Git operations (branch management, worktrees) |
| `src/tokens/` | Token budget tracking |

For detailed API documentation, see the [API Reference](api/index.md).
