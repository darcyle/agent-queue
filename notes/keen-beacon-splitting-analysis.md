# Analysis: Why `keen-beacon` Was Not Split

## Summary

Three root causes prevented the task `keen-beacon` ("1 Discord Configuration (`src/config.py`)") from being automatically split into subtasks. Together they form a compounding failure: the task could never execute, and even if it could, the system would explicitly refuse to split it.

---

## Task Context

| Field            | Value |
|------------------|-------|
| **Task ID**      | `keen-beacon` |
| **Title**        | 1 Discord Configuration (`src/config.py`) |
| **Status**       | `DEFINED` (never promoted) |
| **Parent**       | `bold-ridge` ("Verify Command Parity Between Discord and Chat Agent") |
| **Dependency**   | `eager-horizon` ("Current Architecture Review") — status: `READY` |
| **Downstream**   | `bold-nexus` ("2 Bot Channel Resolution") — status: `DEFINED` |
| **Chain Position** | 3rd of 33 subtasks in the `bold-ridge` dependency chain |

Dependency chain:
```
bold-torrent (COMPLETED, pri=100)
  └→ eager-horizon (READY, pri=101)
       └→ keen-beacon (DEFINED, pri=102)  ← stuck here
            └→ bold-nexus (DEFINED, pri=103)
                 └→ ... 29 more DEFINED tasks
```

---

## Root Cause 1: Stuck in DEFINED — Never Executed

`keen-beacon` depends on `eager-horizon`, which is in `READY` status — meaning it has been promoted from `DEFINED` but has not yet been assigned to an agent or executed.

The `_check_defined_tasks()` method in `orchestrator.py` (line 424) only promotes a task from `DEFINED` → `READY` when **all** of its dependencies are in `COMPLETED` status:

```python
async def _check_defined_tasks(self) -> None:
    defined = await self.db.list_tasks(status=TaskStatus.DEFINED)
    for task in defined:
        deps = await self.db.get_dependencies(task.id)
        if not deps:
            await self.db.transition_task(task.id, TaskStatus.READY, ...)
        else:
            deps_met = await self.db.are_dependencies_met(task.id)
            if deps_met:
                await self.db.transition_task(task.id, TaskStatus.READY, ...)
```

Since `eager-horizon` is `READY` (not `COMPLETED`), `keen-beacon` remains in `DEFINED` indefinitely. A task that never executes can never produce output — including a plan file that would trigger auto-splitting.

**Impact:** No execution → no plan file → no auto-splitting possible.

---

## Root Cause 2: Recursive Plan Guard (`is_plan_subtask`)

Even if `keen-beacon` were somehow promoted, assigned, executed, and produced a plan file, the auto-task generation pipeline would **explicitly skip it**.

`keen-beacon` was created by `_generate_tasks_from_plan()` when it parsed `bold-ridge`'s plan file. During creation, it was flagged with `is_plan_subtask=True` (line 999):

```python
new_task = Task(
    ...
    is_plan_subtask=True,
)
```

The `_generate_tasks_from_plan()` method (line 865) has a guard that prevents plan subtasks from generating further sub-plans:

```python
async def _generate_tasks_from_plan(self, task: Task, workspace: str) -> list[Task]:
    ...
    # Prevent recursive plan explosion: subtasks must not generate
    # further sub-plans.
    if task.is_plan_subtask:
        return []
    ...
```

This is an intentional design decision to prevent unbounded recursive task generation (a plan producing subtasks that each produce their own plans, etc.). However, it means **no plan subtask can ever be split**, regardless of its content.

**Impact:** The `is_plan_subtask=True` flag is a hard block on auto-splitting, even if all other conditions were met.

---

## Root Cause 3: Over-Parsed Design Document Created 33-Step Dependency Chain

The parent task `bold-ridge` produced a comprehensive design document rather than a focused implementation plan. The plan parser extracted **33 sections** as individual tasks — well beyond the configured `max_steps_per_plan` limit of 20 — including many non-actionable sections:

| Subtask Title | Actionable? |
|---------------|-------------|
| Overview | No — informational |
| Current Architecture Review | No — background reference |
| **1 Discord Configuration (`src/config.py`)** | **Yes — `keen-beacon`** |
| 2 Bot Channel Resolution (`src/discord/bot.py`) | Yes |
| Design Decisions | No — informational |
| 1 Channel Mapping Strategy | No — design discussion |
| 2 Control Channel Approach | No — design discussion |
| 3 Channel Discovery | No — design discussion |
| Implementation Steps | No — section header |
| File Change Summary | No — reference |
| User Workflow Examples | No — examples |
| Example 1: Set Up a Channel... | No — example |
| Example 2: Create a New Project... | No — example |
| Implementation Order | No — reference |
| Future Enhancements (Out of Scope) | No — explicitly out of scope |

Despite many of these headings matching entries in `NON_ACTIONABLE_HEADINGS` (e.g., "overview", "design decisions", "file change summary"), they were still extracted. This indicates that either:

1. The **LLM parser** was used (which does not apply the `NON_ACTIONABLE_HEADINGS` filter), or
2. The **`_parse_implementation_section()` path** was triggered (which extracts `###` sub-headings within an `## Implementation` container but still skips `NON_ACTIONABLE_HEADINGS`), and the document structure caused sub-headings to be misclassified, or
3. The regex parser's quality scoring triggered an LLM fallback that overrode the filtering.

With `chain_dependencies=True`, all 33 tasks were chained sequentially. `keen-beacon` sits at position 3, meaning it must wait for 2 preceding tasks to complete before it can even begin — and those preceding tasks include non-actionable items like "Overview" and "Current Architecture Review" that may themselves produce no useful output.

**Impact:** Over-parsing flooded the task queue with non-actionable work, created an excessively long serial dependency chain, and delayed execution of genuinely actionable tasks.

---

## How These Root Causes Compound

```
Root Cause 3 (over-parsing)
  → 33 subtasks chained sequentially instead of ~8 focused implementation steps
  → keen-beacon must wait for "Overview" and "Architecture Review" to complete first
  → excessively long queue of non-actionable tasks ahead of it

Root Cause 1 (stuck in DEFINED)
  → eager-horizon (Architecture Review) is READY but not yet completed
  → keen-beacon blocked waiting for this dependency
  → cannot execute, cannot produce output, cannot trigger plan file discovery

Root Cause 2 (is_plan_subtask guard)
  → even if root causes 1 and 3 were resolved and keen-beacon executed successfully
  → the system would still refuse to split it because is_plan_subtask=True
  → this is an absolute, unconditional block on recursive splitting
```

All three root causes must be addressed to enable keen-beacon (or any similarly-situated subtask) to be split:

1. **Resolve the dependency chain** so keen-beacon can actually execute.
2. **Reconsider the recursive plan guard** — either allow controlled depth (the `max_plan_depth` config exists but is not enforced) or provide a mechanism for subtasks to opt into further splitting.
3. **Improve plan parsing quality** so design documents don't produce 33 non-actionable tasks — apply stricter filtering, enforce `max_steps_per_plan`, and ensure the LLM parser respects the same quality heuristics as the regex parser.

---

## Recommended Fixes

### Fix 1: Enforce `max_steps_per_plan` Across All Parsers
The `max_steps_per_plan` config (default: 20) is passed to parsers but 33 tasks were created. Audit the LLM parser to ensure it respects the cap, and add a safety check in `_generate_tasks_from_plan()`:
```python
plan.steps = plan.steps[:config.max_steps_per_plan]  # enforce cap regardless of parser
```

### Fix 2: Implement `max_plan_depth` Enforcement
The `AutoTaskConfig` already has a `max_plan_depth` field (default: 1), but it is never checked in `_generate_tasks_from_plan()`. Replace the blanket `is_plan_subtask` guard with depth-aware logic:
```python
if task.is_plan_subtask:
    current_depth = await self._get_plan_depth(task)
    if current_depth >= config.max_plan_depth:
        return []
```

### Fix 3: Apply `NON_ACTIONABLE_HEADINGS` Filtering to LLM Parser Output
Ensure LLM-parsed steps are post-filtered against the same `NON_ACTIONABLE_HEADINGS` set used by the regex parser, preventing informational sections from becoming tasks regardless of which parser is used.
