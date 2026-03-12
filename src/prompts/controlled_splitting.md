---
name: controlled-splitting
description: Depth-aware instructions for subtasks that may optionally sub-plan
category: task
variables:
  - name: current_depth
    description: Current depth of this subtask in the plan hierarchy
    required: true
  - name: max_depth
    description: Maximum allowed plan depth
    required: true
tags: [planning, execution, agent, subtask]
version: 1
---

## Task Execution with Optional Sub-Planning

This task was generated from a parent plan (depth {{current_depth}}/{{max_depth}}).
You may either:

1. **Execute directly** — Implement the changes described below.
   This is strongly preferred for well-scoped tasks.

2. **Create a focused sub-plan** — Only if the task is genuinely too
   large for a single agent session. If you do, follow the plan
   structure requirements carefully.

Guidelines:
- Strongly prefer direct execution over sub-planning
- If sub-planning, limit to 2-3 coarse phases that each group many
  related steps together
- Do NOT create fine-grained phases for individual changes
- Do NOT produce design documents or architecture reviews
- Do NOT include background, overview, or rationale sections
- Every heading in your plan must start with an action verb
