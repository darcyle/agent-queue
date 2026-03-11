# Hook Pipeline Guide

Hooks enable automated workflows that react to task lifecycle events or run
on schedules. Each hook follows a five-stage pipeline:

```
trigger → gather context → short-circuit check → render prompt → invoke LLM
```

The LLM invocation uses a full ChatAgent with all tools available, so hooks
can create tasks, check status, send notifications — anything a human user
can do via Discord chat, a hook can do autonomously.

## Trigger Types

### Periodic

Fires on a timer, checked every orchestrator cycle (~5 seconds):

```json
{"type": "periodic", "interval_seconds": 7200}
```

### Event-Driven

Fires when a matching EventBus event arrives:

```json
{"type": "event", "event_type": "task_completed"}
```

Available events: `task_completed`, `task_failed`, `task_blocked`,
`agent_question`, `pr_created`, `task_skipped`, `budget_warning`

## Context Steps

Steps gather information before the LLM is invoked. They run **sequentially**,
and each step's output is available to subsequent steps via template
placeholders (`{{step_0}}`, `{{step_1}}`, etc.).

### Shell Step

Execute a shell command and capture stdout/stderr/exit_code:

```json
{"type": "shell", "command": "git log --oneline -5", "timeout": 60}
```

Output fields: `stdout`, `stderr`, `exit_code`

### Read File Step

Read a file's contents (with optional line limit):

```json
{"type": "read_file", "path": "/path/to/file.txt", "max_lines": 500}
```

Output fields: `content`

### HTTP Step

Make an HTTP GET request:

```json
{"type": "http", "url": "https://api.example.com/status", "timeout": 30}
```

Output fields: `body`, `status_code`

### Database Query Step

Run a pre-defined named query (raw SQL is not allowed for safety):

```json
{"type": "db_query", "query": "recent_task_results", "params": {}}
```

Available named queries:
- `recent_task_results` — last 20 task results with status and token usage
- `task_detail` — detailed info for a specific task (params: `{"task_id": "..."}`)
- `recent_events` — last 50 system events
- `hook_runs` — last 10 runs for a specific hook (params: `{"hook_id": "..."}`)

Parameters support template placeholders from event data:
```json
{"type": "db_query", "query": "task_detail", "params": {"task_id": "{{event.task_id}}"}}
```

### Git Diff Step

Get git diff output between a branch and HEAD:

```json
{"type": "git_diff", "workspace": "/path/to/repo", "base_branch": "main"}
```

Output fields: `diff`, `exit_code`

### Memory Search Step

Semantic search against a project's memory index:

```json
{"type": "memory_search", "project_id": "my-project", "query": "error handling", "top_k": 3}
```

Output fields: `content` (formatted results), `count`

Supports template placeholders in `project_id` and `query`:
```json
{"type": "memory_search", "project_id": "{{event.project_id}}", "query": "{{event.task_title}}"}
```

## Short-Circuit Conditions

Short-circuit conditions let you skip the LLM invocation entirely when a
context step indicates "nothing to do". This saves tokens when the hook
determines there's no action needed.

Add these flags to any context step:

| Condition | Triggers When | Use Case |
|-----------|--------------|----------|
| `skip_llm_if_exit_zero` | Shell command exits with code 0 | Skip when health check passes |
| `skip_llm_if_empty` | stdout or content is empty | Skip when no errors found |
| `skip_llm_if_status_ok` | HTTP status is 2xx | Skip when service is healthy |

Example — only invoke the LLM when tests fail:
```json
{
    "type": "shell",
    "command": "cd /path/to/project && npm test 2>&1",
    "timeout": 120,
    "skip_llm_if_exit_zero": true
}
```

## Prompt Templates

Use `{{placeholder}}` syntax in the `prompt_template` field. Placeholders
are resolved after context steps complete but before LLM invocation.

| Placeholder | Resolves To |
|-------------|-------------|
| `{{step_0}}` | First step's primary output (auto-selects stdout/content/body/diff) |
| `{{step_N.field}}` | Specific field from step N (e.g., `{{step_0.exit_code}}`) |
| `{{event}}` | Full event data as JSON |
| `{{event.field}}` | Specific event field (e.g., `{{event.task_id}}`) |

Primary output auto-detection order: `stdout` → `content` → `body` → `diff`

## LLM Configuration

Hooks use the system's default chat provider by default, but can override
the provider and model:

```json
{
    "llm_config": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514"
    }
}
```

## Cooldown & Concurrency

- **`cooldown_seconds`** (per hook, default 3600): minimum time between runs
  for a single hook. Prevents rapid re-firing after events.
- **`max_concurrent_hooks`** (global, default 2): caps how many hooks can
  run simultaneously. Configured in `hook_engine.max_concurrent_hooks`.
- **Manual triggers** (`/fire-hook`): ignore cooldown, subject to concurrency cap.

## Hook Run Tracking

Every hook execution is recorded in the `hook_runs` database table with:
- `trigger_reason`: "periodic", "event:task_completed", or "manual"
- `status`: "running", "completed", "failed", or "skipped"
- `context_results`: JSON array of step outputs
- `skipped_reason`: why the LLM was skipped (if applicable)
- `prompt_sent`: the rendered prompt
- `llm_response`: the LLM's response
- `tokens_used`: estimated token count

## Examples

### Post-Completion Code Review

Automatically review code changes when a task completes and create follow-up
tasks if issues are found:

```json
{
    "name": "auto-review",
    "trigger": {"type": "event", "event_type": "task_completed"},
    "context_steps": [
        {"type": "db_query", "query": "task_detail", "params": {"task_id": "{{event.task_id}}"}},
        {"type": "git_diff", "workspace": "/path/to/project", "base_branch": "main"}
    ],
    "prompt_template": "Task {{event.task_id}} just completed.\n\nTask details:\n{{step_0}}\n\nCode changes:\n{{step_1}}\n\nReview the changes for bugs, security issues, or code quality problems. If you find issues, create a bugfix task describing the problem and fix.",
    "cooldown_seconds": 60
}
```

### Periodic Health Monitor

Check system health every 2 hours and create an alert task if services are degraded:

```json
{
    "name": "health-monitor",
    "trigger": {"type": "periodic", "interval_seconds": 7200},
    "context_steps": [
        {
            "type": "http",
            "url": "http://localhost:8080/health",
            "timeout": 10,
            "skip_llm_if_status_ok": true
        }
    ],
    "prompt_template": "Health check returned a non-OK status:\n{{step_0}}\n\nAnalyze the health check results and create a task to investigate any degraded components.",
    "cooldown_seconds": 3600
}
```

### Failure Pattern Detection

When a task fails, check if it's a recurring pattern and suggest fixes:

```json
{
    "name": "failure-analysis",
    "trigger": {"type": "event", "event_type": "task_failed"},
    "context_steps": [
        {"type": "db_query", "query": "task_detail", "params": {"task_id": "{{event.task_id}}"}},
        {"type": "db_query", "query": "recent_task_results"},
        {"type": "memory_search", "project_id": "{{event.project_id}}", "query": "task failure error", "top_k": 5}
    ],
    "prompt_template": "Task {{event.task_id}} in project {{event.project_id}} just failed.\n\nFailed task details:\n{{step_0}}\n\nRecent task history:\n{{step_1}}\n\nRelated memories:\n{{step_2}}\n\nAnalyze whether this is a recurring failure pattern. If so, create a task to address the root cause.",
    "cooldown_seconds": 600
}
```

### Daily Summary Report

Generate a daily project status summary:

```json
{
    "name": "daily-summary",
    "trigger": {"type": "periodic", "interval_seconds": 86400},
    "context_steps": [
        {"type": "db_query", "query": "recent_task_results"},
        {"type": "db_query", "query": "recent_events"}
    ],
    "prompt_template": "Generate a brief daily summary of project activity.\n\nRecent task results:\n{{step_0}}\n\nRecent events:\n{{step_1}}\n\nSummarize: tasks completed vs failed, token usage trends, and any issues that need attention.",
    "cooldown_seconds": 82800
}
```

### Test Gate — Skip LLM When Tests Pass

Only involve the LLM when tests fail, saving tokens on healthy runs:

```json
{
    "name": "test-gate",
    "trigger": {"type": "event", "event_type": "task_completed"},
    "context_steps": [
        {
            "type": "shell",
            "command": "cd /path/to/project && npm test 2>&1",
            "timeout": 120,
            "skip_llm_if_exit_zero": true
        }
    ],
    "prompt_template": "Tests failed after task {{event.task_id}} completed.\n\nTest output:\n{{step_0}}\n\nAnalyze the failures and create a bugfix task with the specific fixes needed.",
    "cooldown_seconds": 120
}
```
