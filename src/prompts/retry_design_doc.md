---
name: retry-design-doc
description: Retry prompt when an agent produced a design doc instead of an implementation plan
category: task
variables:
  - name: task_title
    description: Title of the task being retried
    required: true
  - name: signal_list
    description: Markdown bullet list of detected design-doc signals
    required: true
  - name: max_steps
    description: Maximum number of implementation phases allowed
    required: true
tags: [planning, agent, retry]
version: 1
---

# Task: {{task_title}} (RETRY — Implementation Plan Needed)

## Important: Previous Output Was a Design Document

Your previous response was a design document rather than an
implementation plan. It contained the following non-actionable
sections that cannot be converted to tasks:

{{signal_list}}

**Please produce a focused IMPLEMENTATION PLAN instead.**

An implementation plan contains ONLY actionable phases that
describe what code to write, not design discussions or
architecture reviews.
