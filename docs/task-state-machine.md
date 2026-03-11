# Task State Machine

This document describes the full task state machine including all states,
events, transitions, and the rules governing task lifecycle progression.

## States

| State | Description | Terminal? |
|-------|-------------|-----------|
| `DEFINED` | Task created, waiting for dependencies to be satisfied | No |
| `READY` | All dependencies met, eligible for scheduling | No |
| `ASSIGNED` | Scheduler has assigned an agent, awaiting execution start | No |
| `IN_PROGRESS` | Agent is actively working on the task | No |
| `WAITING_INPUT` | Agent asked a question, waiting for human reply via Discord | No |
| `PAUSED` | Temporarily paused (rate limit or token exhaustion), has `resume_after` timestamp | No |
| `VERIFYING` | Agent completed work, verification in progress | No |
| `AWAITING_APPROVAL` | PR created, waiting for human approval/merge | No |
| `COMPLETED` | Task finished successfully | Yes |
| `FAILED` | Task execution failed (may retry) | Semi-terminal |
| `BLOCKED` | Max retries exhausted or admin-stopped | Yes |

## State Transition Diagram

```
                    ┌──────────────────────────────────────────────────┐
                    │              ADMIN_RESTART (from any state)      │
                    └──────────────────────────────────────────────────┘

    ┌─────────┐  DEPS_MET  ┌───────┐  ASSIGNED  ┌──────────┐  AGENT_STARTED  ┌─────────────┐
    │ DEFINED ├────────────►│ READY ├────────────►│ ASSIGNED ├───────────────►│ IN_PROGRESS │
    └─────────┘             └───┬───┘             └────┬─────┘                └──┬──┬──┬──┬─┘
                                ▲                      │                         │  │  │  │
                                │               TIMEOUT│                         │  │  │  │
                                │           EXEC_ERROR │                         │  │  │  │
                                │                      ▼                         │  │  │  │
                                │                 ┌─────────┐                    │  │  │  │
                                │                 │ BLOCKED │◄───MAX_RETRIES─────┘  │  │  │
                                │                 └─────────┘                       │  │  │
                                │                                                  │  │  │
                         RESUME_TIMER  ┌────────┐◄──TOKENS_EXHAUSTED───────────────┘  │  │
                                ├──────┤ PAUSED │                                      │  │
                                │      └────────┘◄──INPUT_TIMEOUT──┐                   │  │
                                │                                   │                   │  │
                                │      ┌────────────────┐          │                   │  │
                                │      │ WAITING_INPUT  ├──────────┘                   │  │
                                │      └───────┬────────┘◄──AGENT_QUESTION─────────────┘  │
                                │              │                                          │
                                │        HUMAN_REPLIED ──► back to IN_PROGRESS            │
                                │                                                         │
                           RETRY│      ┌───────────┐◄──AGENT_COMPLETED────────────────────┘
                                ├──────┤ VERIFYING  │
                                │      └──┬───┬─────┘
                                │         │   │
                                │  VERIFY_│   │PR_CREATED
                                │  PASSED │   │
                         ┌──────┤         ▼   ▼
                         │      │  ┌───────────────────────┐
                         │  ┌───┤  │ AWAITING_APPROVAL     │
                         │  │   │  └───────┬───────────────┘
                         │  │   │          │ PR_MERGED
                         │  │   │          ▼
                         │  │   │  ┌─────────────┐
                         │  │   └─►│ COMPLETED   │
                         │  │      └─────────────┘
                         │  │
                         │  ▼
                         │  ┌────────┐
                         └──┤ FAILED │──MAX_RETRIES──► BLOCKED
                            └────────┘
```

## Transition Table

### Core Lifecycle (Happy Path)

| From | Event | To | Description |
|------|-------|----|-------------|
| DEFINED | DEPS_MET | READY | All dependency tasks are COMPLETED |
| READY | ASSIGNED | ASSIGNED | Scheduler picks task for an idle agent |
| ASSIGNED | AGENT_STARTED | IN_PROGRESS | Agent process has launched |
| IN_PROGRESS | AGENT_COMPLETED | VERIFYING | Agent reports work done |
| VERIFYING | VERIFY_PASSED | COMPLETED | Tests pass, no approval needed |
| VERIFYING | PR_CREATED | AWAITING_APPROVAL | PR created, needs human review |
| AWAITING_APPROVAL | PR_MERGED | COMPLETED | PR merged, task done |

### Pause & Resume

| From | Event | To | Description |
|------|-------|----|-------------|
| IN_PROGRESS | TOKENS_EXHAUSTED | PAUSED | Token budget or rate limit hit |
| IN_PROGRESS | AGENT_QUESTION | WAITING_INPUT | Agent needs human input |
| WAITING_INPUT | HUMAN_REPLIED | IN_PROGRESS | Human answered, resume work |
| WAITING_INPUT | INPUT_TIMEOUT | PAUSED | No reply within timeout |
| PAUSED | RESUME_TIMER | READY | `resume_after` timestamp elapsed |

### Failure & Retry

| From | Event | To | Description |
|------|-------|----|-------------|
| IN_PROGRESS | AGENT_FAILED | FAILED | Agent reports failure |
| VERIFYING | VERIFY_FAILED | FAILED | Tests failed |
| FAILED | RETRY | READY | retry_count < max_retries, try again |
| FAILED | MAX_RETRIES | BLOCKED | No more retries, needs intervention |
| IN_PROGRESS | MAX_RETRIES | BLOCKED | Direct shortcut (skip FAILED) |
| IN_PROGRESS | RETRY | READY | Direct shortcut (skip FAILED) |

### Administrative Operations

| From | Event | To | Description |
|------|-------|----|-------------|
| BLOCKED | ADMIN_SKIP | COMPLETED | Mark as done despite failure |
| FAILED | ADMIN_SKIP | COMPLETED | Mark as done despite failure |
| IN_PROGRESS | ADMIN_STOP | BLOCKED | Force-stop a running task |
| Any non-running | ADMIN_RESTART | READY | Re-queue for execution |

### Error Recovery

| From | Event | To | Description |
|------|-------|----|-------------|
| IN_PROGRESS | TIMEOUT | BLOCKED | Agent stuck timeout |
| ASSIGNED | TIMEOUT | BLOCKED | Agent never started |
| ASSIGNED | EXECUTION_ERROR | READY | Launch failed, retry |
| IN_PROGRESS | RECOVERY | READY | Daemon restarted while task running |
| ASSIGNED | RECOVERY | READY | Daemon restarted while task assigned |
| AWAITING_APPROVAL | PR_CLOSED | BLOCKED | PR closed without merge |

## Promotion Rules

1. **DEFINED → READY**: Checked every orchestrator cycle (~5s). A task
   promotes when `db.are_dependencies_met(task_id)` returns True.

2. **READY → ASSIGNED**: The `Scheduler` selects which project gets the
   next agent slot based on proportional credit-weight.

3. **PAUSED → READY**: Tasks in PAUSED always have a `resume_after` Unix
   timestamp. The orchestrator checks this every cycle.

4. **FAILED → READY/BLOCKED**: On failure, if `retry_count < max_retries`,
   the task goes back to READY. Otherwise it becomes BLOCKED.

## Important Notes

- **State machine is advisory**: Transitions are NOT enforced in production.
  The orchestrator writes directly via `db.update_task()`.
- **PAUSED never stalls**: Every PAUSED task has a `resume_after` timestamp.
- **DAG validation**: Cycle detection prevents circular dependency chains.
