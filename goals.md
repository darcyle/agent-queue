# Agent Queue - Project Goals

## Primary Goal

Enable developers on throttled AI plans to queue coding work, walk away, and come back to completed pull requests — managed entirely from Discord on their phone.

## User-Facing Goals

### G1: Maximize Agent Utilization
- Agents should always be working when tokens are available — no idle time between tasks
- Rate limit pauses and resumes must be automatic and invisible to the user
- Task queues should drain continuously overnight and while the user is offline

### G2: Multi-Project Orchestration
- Users can run multiple projects simultaneously with fair, weighted scheduling
- Each project gets proportional agent time based on its configured priority
- No project should be starved of agent time indefinitely

### G3: Discord-First Control Plane
- Every operation must be available from Discord — no SSH or terminal required
- Natural language chat and slash commands both work with full feature parity
- Task progress streams live in Discord threads; users reply to unblock agents
- Notifications surface completions, failures, and stuck chains proactively

### G4: Zero Orchestration Overhead
- Scheduling, dependency resolution, and state transitions consume zero LLM tokens
- Every token spent should be on actual agent work, hooks, or memory — never bookkeeping
- The system must run efficiently on a Raspberry Pi or low-end hardware

### G5: Reliable Task Lifecycle
- No work is silently dropped — every task is tracked in persistent storage across restarts
- Failed tasks retry automatically up to a configurable limit before escalating
- Dependency chains execute in correct order; cycles are detected and rejected
- Stuck chains are detected and surfaced with root cause and blast radius

### G6: Safe Git Workflows
- Each task works on an isolated branch in its own workspace
- Nothing merges to main without explicit user approval
- Git operations (branch, commit, push, PR) are atomic and retry-resilient
- Merge conflicts are detected and reported, never silently corrupted

### G7: Autonomous Workflows via Hooks
- Users can define event-driven and scheduled automations that run without intervention
- Hooks have full tool access — they can create tasks, check status, send notifications
- Safeguards (cooldowns, concurrency caps, token limits) prevent runaway costs

### G8: Evolving Project Memory
- The system learns from completed tasks and refines project knowledge over time
- Agents receive relevant context (architecture, conventions, past decisions) automatically
- Memory grows intelligently — old memories compact into digests, not unbounded logs

### G9: Extensible Architecture
- New agent types (beyond Claude Code) can be added via the adapter interface
- New LLM providers can be plugged in without changing orchestration logic
- The MCP server exposes all commands as tools for external integration
- A plugin system allows third-party extensions

### G10: Simple Setup, Low Maintenance
- First-time setup should be a single guided wizard — working system in under 10 minutes
- Configuration lives in one YAML file with sensible defaults
- The system self-heals: dead agents are detected, workspaces are recovered, timers resume after restarts
