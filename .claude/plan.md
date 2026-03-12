# Plan: Avoid Creating Superfluous Tasks from Already-Completed Plans

## Problem

When a task both creates a plan file (`.claude/plan.md`) AND implements all the work
described in that plan, the completion pipeline (`_phase_plan_generate()`) still discovers
the plan file and creates subtasks from it. These subtasks are superfluous — the work is
already done.

**Example:** The `wise-rapids` task ("Plan implementation: Prompt Centralization") created
a plan, implemented all 6 prompt centralizations, passed all 1666 tests, and committed.
On completion, the system found the plan file and created subtask `grand-vault`, which
immediately concluded "all work is complete — nothing to do."

## Root Cause

There is no mechanism to detect whether a plan's phases were already executed during the
same task. The `_generate_tasks_from_plan()` method in `orchestrator.py` simply checks:
1. Is auto-task enabled?
2. Is this a plan subtask? (prevents recursive explosion)
3. Does a plan file exist?

If all three pass, it creates subtasks — regardless of whether the task already did the work.

## Design Constraints

- The solution should work without requiring LLM calls (keeping it cheap/fast)
- It should be reliable — false negatives (missing a real plan) are worse than false
  positives (creating a few extra tasks), but the current rate of false positives is too high
- It should be backward-compatible with existing plan files

## Phase 1: Add Git-Diff Heuristic to Skip Plan Task Generation

**Location:** `src/orchestrator.py`, `_generate_tasks_from_plan()` method (lines 1892-2100)

After discovering and reading the plan file, but before parsing it into tasks, check
whether the task made substantial code changes beyond just the plan file itself:

1. Run `git diff --stat <merge-base>..HEAD -- . ':!.claude/plan.md' ':!plan.md' ':!.claude/plans/'`
   in the workspace to get a summary of non-plan file changes
2. If the diff shows significant changes (e.g., >0 files changed excluding the plan itself),
   this is strong evidence the plan was already executed
3. Log a message and skip task generation

**Implementation details:**
- Add a helper method `_task_has_code_changes(workspace: str) -> bool` that uses
  `self.git` (the `GitOps` helper) to check for non-plan-file changes on the branch
- Call it in `_generate_tasks_from_plan()` right after the `is_plan_subtask` guard
- Make this check configurable via a new `skip_if_implemented: bool = True` field on
  `AutoTaskConfig` so it can be disabled if needed
- Use the existing `GitOps` infrastructure rather than raw subprocess calls
- The check should compare the task branch against the merge-base with main, excluding
  plan file paths from the diff

**Edge cases to handle:**
- Plan-only tasks (no code changes) should still generate subtasks — this is the normal case
- Tasks that make minor changes (e.g., just updating a config file) alongside a plan —
  use a threshold (e.g., at least 3 files changed or 50+ lines changed) to avoid
  false positives from incidental changes
- Tasks where the workspace has no git history — fall through to normal behavior

## Phase 2: Support Plan Frontmatter Opt-Out

**Location:** `src/plan_parser.py` and `src/orchestrator.py`

Add support for a YAML frontmatter field in plan files that explicitly opts out of
auto-task generation:

1. In `plan_parser.py`, add frontmatter parsing to `parse_plan()`:
   - Check for `---` delimited YAML frontmatter at the top of the plan file
   - Extract an `auto_tasks` field (values: `true`/`false`/`skip`)
   - Add `auto_tasks_enabled: bool = True` to the `ParsedPlan` dataclass

2. In `orchestrator.py`, `_generate_tasks_from_plan()`:
   - After parsing, check `plan.auto_tasks_enabled`
   - If `False`, log and skip task generation

3. Update `src/prompts/plan_structure_guide.md` to document the frontmatter option:
   - Add example: `---\nauto_tasks: false\n---` at top of plan to skip task generation

**This gives agents an explicit escape hatch:** if an agent writes a plan as part of
its thinking process but also implements the work, it can add `auto_tasks: false` to
the plan frontmatter to prevent superfluous subtask creation.

## Phase 3: Update Agent Instructions to Prevent Plan File Residue

**Location:** `src/orchestrator.py` (the "CRITICAL: Writing Implementation Plans" section,
around line 2759) and `src/prompts/plan_structure_guide.md`

Update the system prompt instructions that agents receive to be clearer about when to
leave vs. remove plan files:

1. Add a new rule to the "CRITICAL: Writing Implementation Plans" section:
   ```
   6. If you implement the plan yourself (i.e., you both plan AND execute the work
      in a single task), DELETE the plan file before completing. Only leave a plan
      file in the workspace if you want the system to create follow-up tasks from it.
      Alternatively, add `auto_tasks: false` to the plan's YAML frontmatter.
   ```

2. Add a clarifying note:
   ```
   NOTE: Any plan file left in the workspace when your task completes will be
   automatically parsed and converted into follow-up subtasks. If you already
   did the work described in the plan, this creates duplicate/unnecessary tasks.
   ```

This is a belt-and-suspenders approach — even if the git-diff heuristic fails, agents
will be instructed to clean up after themselves.
