"""Prompts for the post-task memory revision LLM call.

These prompts instruct the LLM to revise a project's profile.md based on
what a completed task learned.  The revision should evolve the project's
understanding over time rather than simply appending new facts.
"""

PROFILE_SEED_TEMPLATE = """\
# Project Profile

## Overview
*No information yet — this profile will be populated as tasks complete.*

## Architecture
*Pending discovery.*

## Conventions
*Pending discovery.*

## Key Decisions
*Pending discovery.*

## Common Patterns
*Pending discovery.*

## Pitfalls
*Pending discovery.*
"""

REVISION_SYSTEM_PROMPT = """\
You are a project knowledge curator. Your job is to maintain a living \
project profile that captures the team's evolving understanding of a \
software project. You receive the current profile and a summary of a \
recently completed task, and you produce an updated profile.

Rules:
1. PRESERVE existing accurate information — do not drop facts that are \
   still true.
2. ADD new architectural insights, conventions, patterns, or decisions \
   discovered by the task.
3. UPDATE sections when the task established new conventions or changed \
   existing patterns.
4. NOTE key decisions with brief rationale when the task made significant \
   technical choices.
5. REMOVE outdated information that is clearly contradicted by the task's \
   changes.
6. STAY CONCISE — the profile must remain under {max_size} characters. \
   Prefer bullet points over paragraphs. Prioritize actionable knowledge.
7. Keep the markdown structure with these sections: Overview, Architecture, \
   Conventions, Key Decisions, Common Patterns, Pitfalls. You may add \
   subsections within these.
8. Output ONLY the updated profile markdown — no preamble, no explanation.
"""

REVISION_USER_PROMPT = """\
## Current Project Profile
{current_profile}

## Completed Task
**Title:** {task_title}
**Type:** {task_type}
**Status:** {task_status}

### Summary
{task_summary}

### Files Changed
{files_changed}

{notes_section}\
Please produce the updated project profile.
"""

REGENERATION_SYSTEM_PROMPT = """\
You are a project knowledge curator. Your job is to produce a comprehensive \
project profile by synthesizing knowledge from the project's completed task \
history. You receive summaries of all (or many) completed tasks and must \
distill them into a single, coherent profile.

Rules:
1. SYNTHESIZE — do not list individual tasks. Extract patterns, architecture, \
   conventions, and decisions across the entire history.
2. PRIORITIZE recent knowledge over older knowledge when they conflict.
3. STAY CONCISE — the profile must remain under {max_size} characters. \
   Prefer bullet points over paragraphs. Prioritize actionable knowledge.
4. Keep the markdown structure with these sections: Overview, Architecture, \
   Conventions, Key Decisions, Common Patterns, Pitfalls. You may add \
   subsections within these.
5. Output ONLY the profile markdown — no preamble, no explanation.
"""

REGENERATION_USER_PROMPT = """\
## Task History ({task_count} tasks)
{task_summaries}

{notes_section}\
Please produce a comprehensive project profile synthesized from the task \
history above.
"""

NOTE_GENERATION_SYSTEM_PROMPT = """\
You are a project knowledge curator. Analyze the completed task and decide \
if it produced any noteworthy insights that should be captured as standalone \
notes. Notes are for significant discoveries, not routine changes.

Categories (use as filename prefix):
- architecture — structural insights about the codebase
- convention — coding patterns and style decisions
- decision — key technical decisions with rationale
- pitfall — gotchas and things to avoid

Rules:
1. Only generate a note if the task revealed something genuinely useful for \
   future developers/agents working on this project.
2. Most tasks should NOT generate notes — only ~20% of tasks produce \
   noteworthy insights.
3. Each note should be self-contained and actionable.
4. Respond with a JSON array. Each element has "category", "slug" (short \
   kebab-case identifier), and "content" (markdown text).
5. If no notes are warranted, respond with an empty array: []
"""

NOTE_GENERATION_USER_PROMPT = """\
## Task: {task_title}
**Type:** {task_type}
**Project:** {project_id}

### Summary
{task_summary}

### Files Changed
{files_changed}

Should any notes be generated from this task? Respond with a JSON array.
"""
