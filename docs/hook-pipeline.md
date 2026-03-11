# Hook Pipeline Guide

Hooks enable automated workflows that react to task lifecycle events or run
on schedules. Pipeline: trigger → gather context → short-circuit → render prompt → invoke LLM.

## Trigger Types

### Periodic
```json
{"type": "periodic", "interval_seconds": 7200}
```

### Event
```json
{"type": "event", "event_type": "task_completed"}
```

Available events: `task_completed`, `task_failed`, `task_blocked`, `agent_question`, `pr_created`

## Context Steps

Steps gather information before the LLM is invoked. They run sequentially.

### Shell Step
```json
{"type": "shell", "command": "git log --oneline -5", "timeout": 60}
```

### Read File Step
```json
{"type": "read_file", "path": "/path/to/file.txt", "max_lines": 500}
```

### HTTP Step
```json
{"type": "http", "url": "https://api.example.com/status", "timeout": 30}
```

### Database Query Step
```json
{"type": "db_query", "query": "recent_task_results", "params": {}}
```
Named queries: `recent_task_results`, `task_detail`, `recent_events`, `hook_runs`

### Git Diff Step
```json
{"type": "git_diff", "workspace": "/path/to/repo", "base_branch": "main"}
```

### Memory Search Step
```json
{"type": "memory_search", "project_id": "my-project", "query": "error handling", "top_k": 3}
```

## Short-Circuit Conditions

| Condition | Triggers When |
|-----------|--------------|
| `skip_llm_if_exit_zero` | Shell exits with code 0 |
| `skip_llm_if_empty` | Output is empty |
| `skip_llm_if_status_ok` | HTTP status is 2xx |

## Prompt Templates

Use `{{placeholder}}` syntax:

| Placeholder | Resolves To |
|-------------|-------------|
| `{{step_0}}` | First step's primary output |
| `{{step_N.field}}` | Specific field from step N |
| `{{event}}` | Full event data as JSON |
| `{{event.field}}` | Specific event field |

## LLM Invocation

The hook's LLM call uses a full ChatAgent with access to all tools (create tasks, check status, etc.).

## Cooldown & Concurrency

- `cooldown_seconds` (default 3600): minimum time between runs per hook
- `max_concurrent_hooks` (default 2): global concurrency cap
- Manual triggers (`fire_hook`) ignore cooldown

## Example: Post-Completion Review

```json
{
    "name": "auto-review",
    "trigger": {"type": "event", "event_type": "task_completed"},
    "context_steps": [
        {"type": "git_diff", "workspace": "/path/to/project", "base_branch": "main"}
    ],
    "prompt_template": "Task {{event.task_id}} completed. Review:\n{{step_0}}\nCreate a bugfix task if issues found.",
    "cooldown_seconds": 300
}
```
