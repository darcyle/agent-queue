# [Action Verb] [Broad Area of Work]

**Task ID:** `smart-delta`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 101
**Branch:** `grand-falcon/add-a-clear-command-that-clears-the-channel-messages`
**Plan Subtask:** Yes
**Plan Source:** `/mnt/d/Dev/agent-queue2/.claude/plans/grand-falcon-plan.md`
**Archived:** 2026-03-13 16:38:10

## Description

**[Action Verb] [Broad Area of Work]**

## Background Context
---
name: plan-structure-guide
description: Instructions for agents producing well-structured implementation plans
category: task
variables:
  - name: max_steps
    description: Maximum number of implementation phases allowed
    required: true
tags: [planning, agent, structure]
version: 1
---

## Plan Structure Requirements

When producing an implementation plan, follow these formatting rules
to ensure the plan can be automatically parsed into subtasks.

IMPORTANT: Each phase becomes a separate subtask executed by an agent.
Phases should be COARSE — each one should represent a substantial chunk
of work containing MANY concrete steps. Do NOT create a separate
phase for every small action (e.g. "create file X", "add import Y").
Instead, group related work into 2-4 broad phases. Fewer, larger
phases are ALWAYS preferred.

### DO:
- Use action-verb headings: "## Implement X", "## Create Y", "## Add Z"
- Use numbered phases: "## Phase 1: Database Layer and Migrations"
- Group related work AGGRESSIVELY: a phase like "Build API Endpoints"
  should include creating routes, adding validation, writing handlers,
  AND writing tests for those endpoints — all in one phase
- Include a numbered outline of ALL concrete steps within each phase
  (so the agent can see the full scope of work at a glance)
- Include detailed descriptions for each step after the outline
- Include estimated effort for each phase
- Put implementation phases under a "## Implementation Plan" container heading
- Keep the total number of phases between 2 and {{max_steps}} (aim for 2-4)
- Each phase should be independently executable and represent several
  hours of focused work

### DON'T:
- Don't create more than 4 phases unless the work is truly enormous
- Don't create a separate phase for each individual file change or function
- Don't include overview/summary sections as separate headings
- Don't include design discussion, architecture review, or rationale sections
- Don't include reference material (file change summaries, API specs, examples)
- Don't include sections labeled "Future Work", "Out of Scope", or "Background"
- Don't produce more than {{max_steps}} implementation phases
- Don't write a design document when asked to implement something

### Plan File Lifecycle

When your task completes, any plan file left in the workspace (`.claude/plan.md`
or `plan.md`) will be **automatically parsed and converted into follow-up subtasks**.

- **If you only wrote the plan** (did NOT implement it): leave the plan file in place
  so the system creates subtasks to execute each phase.
- **If you implemented the plan yourself** (both planned AND executed the work in a
  single task): **DELETE the plan file** before completing, or add `auto_tasks: false`
  to the plan's YAML frontmatter. Leaving a completed plan file behind creates
  duplicate/unnecessary follow-up tasks.

### Ideal Plan Structure:

```markdown

## Implementation Plan

### Phase 1:

This task is part of the implementation plan from: **Add a /clear command that clears the channel messages**

## High-Level Steps
- [Step]
- [Step]
- [Step]
- [Step]

## Task Details
[Description covering multiple related changes]

Steps in this phase:
1. [Step]
2. [Step]
3. [Step]
4. [Step]

[Detailed descriptions for each step]

**Estimated effort:** ~N hours
```

## Result

**Summary:** The branch is fully up to date with main — commit `a2ae0ca` has already been merged into main. There's no plan file, no uncommitted changes, and no commits ahead of main. The task is fully complete with nothing remaining to do.

**Tokens Used:** 596
