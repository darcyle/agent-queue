---
auto_tasks: true
---

# Spec Updates Needed for Recent Code Changes

The following spec files need updates based on recent commits to main. Each phase
is a self-contained spec update that can be implemented independently.

## Background

Recent commits on main introduced several behavioral changes that are not yet
reflected in the specification documents:

1. **Plan approval workflow** (ae61118, c6780e4, b507422) — The orchestrator's
   `_generate_tasks_from_plan` was refactored into a two-step plan approval flow:
   `_discover_and_store_plan` discovers/parses the plan and stores it in
   `task_context` for user review, then `_create_subtasks_from_stored_plan`
   creates the subtasks after the user approves. Tasks now transition through
   `AWAITING_PLAN_APPROVAL` status instead of auto-creating subtasks.

2. **Plan file cleanup after approval** (07fcc7d) — A new
   `_cleanup_plan_files_after_approval` method on `CommandHandler` deletes plan
   files from the workspace after approval or deletion to prevent stale plans from
   being re-discovered.

3. **Workspace sync fix for default branch** (ab0da75) — `sync_workspaces` no
   longer stashes uncommitted changes on default-branch workspaces. Instead it
   auto-commits them and pushes any unpushed local commits before hard-resetting.

4. **Legacy table drop** (0f67cff) — The `agent_workspaces` table was dropped
   from the schema. The migration code that populated it was removed and replaced
   with a `_drop_legacy_agent_workspaces` migration. References in `delete_agent`
   and `delete_project` cascades were removed.

5. **Discord @mention requirement** (c15e034) — Messages in per-project channels
   now require an @mention to trigger the bot. Users can chat freely without every
   message being processed.

## Phase 1: Update models-and-state-machine.md for plan approval workflow

File: `specs/models-and-state-machine.md`

Changes needed:

1. **Add `AWAITING_PLAN_APPROVAL` to the `TaskStatus` enum table** (after
   `AWAITING_APPROVAL`):
   - Value: `AWAITING_PLAN_APPROVAL`
   - Meaning: "The agent completed its work and produced a plan file. The plan has
     been parsed and stored for user review. Subtasks will be created only after
     explicit approval via the `approve_plan` command."

2. **Add three new `TaskEvent` values** to the enum table:
   - `PLAN_FOUND` — "The orchestrator discovered a plan file in the workspace after
     task completion. The plan was parsed and stored for approval."
   - `PLAN_APPROVED` — "A user approved the discovered plan via the `approve_plan`
     command. Subtasks are created and the task completes."
   - `PLAN_REJECTED` — "A user rejected the plan via the `reject_plan` command.
     The task returns to READY for the agent to retry with feedback."
   - `PLAN_DELETED` — "A user dismissed the plan via the `delete_plan` command.
     The task completes without creating subtasks."

3. **Add state machine transitions** to the transition table and mermaid diagram:
   - `(VERIFYING, PLAN_FOUND) → AWAITING_PLAN_APPROVAL` (happy path)
   - `(AWAITING_PLAN_APPROVAL, PLAN_APPROVED) → COMPLETED`
   - `(AWAITING_PLAN_APPROVAL, PLAN_REJECTED) → READY`
   - `(AWAITING_PLAN_APPROVAL, PLAN_DELETED) → COMPLETED`
   - `(AWAITING_PLAN_APPROVAL, ADMIN_RESTART) → READY` (admin override)

## Phase 2: Update orchestrator.md for plan approval workflow

File: `specs/orchestrator.md`

Changes needed:

1. **Refactor Section 12 ("Plan-Generated Tasks")** to describe the two-step
   approval workflow instead of the old single-step `_generate_tasks_from_plan`:

   - Rename the section to "Plan Discovery and Approval Workflow" or similar.
   - Document `_discover_and_store_plan(task, workspace)`:
     - Same guards as before (auto_task.enabled, is_plan_subtask).
     - Discovers plan file, reads it, parses it (LLM or regex).
     - Archives the plan file to `.claude/plans/{task.id}-plan.md`.
     - Stores parsed plan data in `task_context` (type: `plan_data`, containing
       JSON with steps, source file, raw content, and plan context preamble).
     - Also stores the archived path as `task_context` (type: `plan_archived_path`).
     - Transitions task from VERIFYING → AWAITING_PLAN_APPROVAL.
     - Posts a plan approval embed to Discord with plan details and
       approve/reject/delete buttons.
     - Returns `True` if a plan was found and stored, `False` otherwise.
   - Document `_create_subtasks_from_stored_plan(task) -> list[Task]`:
     - Retrieves stored plan data from `task_context`.
     - Creates subtasks using the same logic as the old method (dependency
       chaining, approval inheritance, etc.).
     - Returns the created tasks.

2. **Update the COMPLETED path in Section 9 (step 15)** — line ~480:
   - Change "call `_generate_tasks_from_plan(task, workspace)`" to
     "call `_discover_and_store_plan(task, workspace)`. If a plan is found,
     the task transitions to AWAITING_PLAN_APPROVAL and subtask creation is
     deferred until the user approves the plan."

## Phase 3: Update command-handler.md for plan approval commands

File: `specs/command-handler.md`

Changes needed:

1. **Add `approve_plan` command documentation:**
   - Parameters: `task_id` (required)
   - Validates task is in AWAITING_PLAN_APPROVAL status
   - Calls `orchestrator._create_subtasks_from_stored_plan(task)`
   - Calls `_cleanup_plan_files_after_approval(task)` to delete plan files
   - Transitions task to COMPLETED
   - Returns subtask list with IDs and titles

2. **Add `reject_plan` command documentation:**
   - Parameters: `task_id` (required), `feedback` (optional)
   - Validates task is in AWAITING_PLAN_APPROVAL status
   - Transitions task back to READY with retry
   - Appends feedback to task description if provided
   - Returns confirmation

3. **Add `delete_plan` command documentation:**
   - Parameters: `task_id` (required)
   - Validates task is in AWAITING_PLAN_APPROVAL status
   - Calls `_cleanup_plan_files_after_approval(task)` to delete plan files
   - Transitions task to COMPLETED (no subtasks created)
   - Returns confirmation

4. **Document `_cleanup_plan_files_after_approval(task)` helper:**
   - Gets workspace for the task
   - Deletes archived plan file from `.claude/plans/` (path from task_context)
   - Deletes any original plan files (`.claude/plan.md`, `plan.md`)
   - Commits deletions to git

5. **Update `sync_workspaces` command documentation** (line ~1119):
   - Change step 6 ("If on the default branch: stashes uncommitted changes...")
     to: "If on the default branch: auto-commits uncommitted changes via
     `git.acommit_all()`, pushes any unpushed local commits to origin (with
     `force_with_lease`), then hard-resets to `origin/<default_branch>`."
   - Remove mention of stashing behavior.

## Phase 4: Update discord.md for @mention requirement in project channels

File: `specs/discord/discord.md`

Changes needed:

1. **Update the `on_message` routing table** (Section 2.7, line ~122):
   - Change "Message in a per-project channel | Yes" to
     "Message in a per-project channel (with @mention) | Yes"
   - Add a new row: "Message in a per-project channel (without @mention) | No (silent)"

2. **Update the flowchart** to include the project-channel @mention check:
   - After the "Determine channel context" step, add a decision node:
     "Project channel without @mention?" → Yes → Ignore

3. **Add explanatory text** after the table:
   - "In per-project channels, the bot requires an explicit @mention to respond.
     This allows team members to have discussions in project channels without
     every message being processed by the bot. The global bot channel and notes
     threads continue to process all messages without requiring mentions."

## Phase 5: Update database.md for legacy table removal

File: `specs/database.md`

Changes needed:

1. **Update the table count** in Section 3 introduction — change "All 15 tables"
   to "All 14 tables" (agent_workspaces was dropped).

2. **Update `delete_agent` cascade** (Section 8 area):
   - Remove step 3 ("agent_workspaces – per-project workspace mappings (legacy)")
   - Renumber remaining steps

3. **Update `delete_project` cascade** (Section 4):
   - Remove the `agent_workspaces` deletion step
   - Verify the cascade order matches the current code

4. **Update Section 14 (Migration / Schema Evolution)**:
   - Add the `_drop_legacy_agent_workspaces` migration that runs
     `DROP TABLE IF EXISTS agent_workspaces`
   - Remove documentation of `_migrate_agent_workspaces` and
     `_migrate_agent_workspaces_to_workspaces` methods (they no longer exist)

## Phase 6: Update git spec for agent_workspaces reference removal

File: `specs/git/git.md`

Changes needed:

1. **Update P1 (Per-Agent Workspace Isolation)** in Section 11:
   - Change "Maintained by: `_prepare_workspace` in the orchestrator, via
     `_compute_workspace_path` and the `agent_workspaces` SQLite cache."
   - To: "Maintained by: `_prepare_workspace` in the orchestrator, via
     `db.acquire_workspace` and the `workspaces` table."
