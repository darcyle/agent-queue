---
auto_tasks: true
---

# Spec Updates Needed for Recent Code Changes

Analysis of 11 recent commits against existing specification files. Below are the background findings, followed by implementation phases for each spec that needs updating.

## Background: Changes Analyzed

1. **Hook simplification** (be30dce): Removed context steps, removed short-circuit checks, removed `NAMED_QUERIES`/`db_query`, removed `skip_llm` flag. Pipeline simplified from 4-phase to 2-phase (render prompt → invoke LLM).
2. **Hook response summary limit** (8dd84be): Truncation increased from 200 to 4000 chars.
3. **Task notification embeds** (56c4111): `format_task_completed_embed` and `format_task_failed_embed` now place summary/error in embed `description` (4096 char limit) instead of embed fields (1024 char limit).
4. **Commands.py truncation fix** (7b9bffb): New `_send_long_interaction` helper replaces bare `[:1997]` truncations in slash command responses. Same split/attach logic as `_send_long_message` but for interaction responses.
5. **Rule reconciliation timing** (aa206c6): Moved to `on_ready` so supervisor is available.
6. **Global rules fix** (0f76036): Global rules now generate hooks for all projects.

---

## Phase 1: Update hooks spec — remove context steps and short-circuit references

**File:** `specs/hooks.md`

Changes needed:
- **§2 Hook Data Model (line 50):** Remove `context_steps` from the Hook field table. The field still exists in the model but is vestigial (`'[]'` default, never read by the engine). Add a note that it's deprecated/unused.
- **§3.1 Trigger-Level Flags (line 154):** Remove the `skip_llm` flag row — it no longer exists in the codebase.
- **§6 Hook Run Recording (lines 343, 357, 362, 366):** Remove references to "short-circuit" from the status transitions. The `skipped` status and `context_results` field references to step execution should be removed or updated. The `running → skipped` transition is no longer reachable.
- **§11 Discord Notifications (line 477):** Update the hook completion notification format — response summary is now truncated to **4000 chars** (not 200).

## Phase 2: Update discord spec — embed description for task summaries and new long-message helper

**File:** `specs/discord/discord.md`

Changes needed:
- **§2.11 Long Message Handling (line 206):** Add documentation for the new `_send_long_interaction` helper function in `commands.py`. It provides the same split/attach behavior as `_send_long_message` but works with Discord interaction responses (`interaction.response.send_message` and `interaction.followup.send`). Thresholds: ≤2000 as-is, 2001–6000 split at line boundaries, >6000 preview + file attachment.
- **§3.4 Task Commands — `/task-result` (line 475):** Update the description to note that summary is now in the embed description (4096 char limit) rather than an embed field (1024 char limit).
- **§3.4 Task Commands — `/agent-error` (line 485):** Same update — error detail now uses embed description.
- **§4.2 Notification Types:** Add documentation for the embed-based notification functions `format_task_completed_embed` and `format_task_failed_embed`, noting that summary/error is placed in embed `description` (4096 chars) rather than fields (1024 chars).

## Phase 3: Update hooks spec — rule-generated hooks improvements

**File:** `specs/hooks.md`

Changes needed:
- **§13 Rule-Generated Hooks (line 511):** Add note that rule reconciliation now runs during `on_ready` (not at an arbitrary later point), ensuring the supervisor is available for LLM-powered prompt expansion. Also document that global rules now generate hooks for all projects (not just the rule's owning project).
