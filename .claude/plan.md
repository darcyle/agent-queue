---
auto_tasks: true
---

# Investigation: All Truncation Points in Agent Queue System

## Background & Root Cause Analysis

Task reports/responses are being truncated, making them unreadable. After a thorough investigation of the entire codebase, there are **multiple layers of truncation** — some are intentional Discord API constraints, some are overly aggressive internal limits, and some are missing proper handling for long content.

### The Main Culprit: Discord's 2000-Character Message Limit

Discord enforces a hard 2000-character limit per message. The system has a `_send_long_message()` method in `bot.py` (line 496) that handles this correctly by:
- Sending messages under 2000 chars normally
- Splitting medium messages (2000–6000 chars) at line boundaries
- Attaching very long messages (>6000 chars) as files with a short preview

**However, this handler is NOT used consistently.** Several notification paths bypass it and directly truncate to `[:1997] + "..."`, losing content permanently.

### Discord Embed Limits (separate from message limits)

Discord embeds have their own hard limits defined in `embeds.py`:
- Title: 256 chars
- Description: 4096 chars
- **Field value: 1024 chars** ← This is where task summaries get truncated in embeds
- Footer: 2048 chars
- Total embed: 6000 chars

The task completion embed (`format_task_completed_embed` in `notifications.py:335`) truncates the summary to `LIMIT_FIELD_VALUE` (1024 chars) — this is the most impactful truncation for task reports since summaries from Claude agents are often longer.

---

## Complete Truncation Inventory

### Category 1: Task Report Truncation (HIGH IMPACT — directly causes the user's issue)

| Location | What's Truncated | Limit | Fix Priority |
|----------|-----------------|-------|-------------|
| `notifications.py:348` | Task completion summary in embed field | 1024 chars | **HIGH** |
| `notifications.py:184,375` | Error messages in failure notifications | 300 chars | MEDIUM |
| `orchestrator.py:3770` | Error message in thread failure report | 400 chars | MEDIUM |
| `command_handler.py:3413` | Agent summary in task-info command | 1000 chars | **HIGH** |
| `command_handler.py:3409` | Error message in task-info command | 2000 chars | LOW |
| `hooks.py:481` | Hook response summary | 200 chars | **HIGH** |

### Category 2: Discord Message Truncation (MEDIUM IMPACT — loses content without fallback)

| Location | What's Truncated | Limit | Fix Priority |
|----------|-----------------|-------|-------------|
| `commands.py:670-671` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:730-731` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:893-894` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:2287-2288` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:2314-2315` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:3507-3508` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:3644-3645` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:4488-4489` | Various command responses | `[:1997] + "…"` | MEDIUM |
| `commands.py:5272-5273` | Various command responses | `[:1997] + "…"` | LOW |

### Category 3: Display/Preview Truncation (LOW IMPACT — intentional UI limits)

| Location | What's Truncated | Limit |
|----------|-----------------|-------|
| `commands.py:539` | Task title in thread name | 100 chars |
| `commands.py:1595` | Task description preview | 800 chars |
| `commands.py:2800` | Task title | 100 chars |
| `orchestrator.py:602,3847` | Question payload in DB log | 500 chars |
| `orchestrator.py:2064` | PR body description | 500 chars |
| `orchestrator.py:3291` | Thread name | 100 chars |
| `notifications.py:219` | Agent question in notification | 500 chars |
| `plan_parser.py:549` | Plan step title | 120 chars |
| `adapters/claude.py:452,465,479` | Streaming previews (thinking/cmd/content) | 100-500 chars |

### Category 4: Context/Memory Truncation (MEDIUM IMPACT — affects agent context)

| Location | What's Truncated | Limit |
|----------|-----------------|-------|
| `memory.py:929-931` | Project docs injected as context | 3000 chars |
| `memory.py:989-990` | Recent task content in memory recall | 500 chars |
| `orchestrator.py:3127-3128` | Dependency summary for downstream tasks | 2000 chars |
| `config.py:270` | Agent profile content | 5000 chars |
| `command_handler.py:5467` | Profile preview | 500 chars |

### Category 5: Tool Output Truncation (LOW IMPACT — tool execution limits)

| Location | What's Truncated | Limit |
|----------|-----------------|-------|
| `command_handler.py:6017` | Shell stdout | 4000 chars |
| `command_handler.py:6018` | Shell stderr | 2000 chars |
| `command_handler.py:6051` | Find output | 4000 chars |
| `command_handler.py:6064` | File read (modal) | 4000 chars |
| `command_handler.py:6240` | Grep matches | 500 items |
| `command_handler.py:6293` | Grep output | 8000 chars |

### Category 6: Supervisor/Hook Truncation

| Location | What's Truncated | Limit |
|----------|-----------------|-------|
| `supervisor.py:112-113` | Detail in supervisor notices | 60 chars |
| `supervisor.py:756-757` | Supervisor result | 500 chars |
| `hooks.py:481` | Hook response summary | 200 chars |

---

## Recommended Fixes

## Phase 1: Fix task completion reports — move summary to embed description instead of field

The most impactful fix. Currently, task summaries go into an embed **field** (1024 char limit). Move the summary into the embed **description** (4096 char limit) for both completion and failure embeds.

**Files to change:**
- `src/discord/notifications.py` — `format_task_completed_embed()`: Move `output.summary` from a field to the embed description, giving it 4096 chars instead of 1024. Same for `format_task_failed_embed()`.
- `src/discord/embeds.py` — Ensure `success_embed()` and `error_embed()` support a `description` parameter that gets truncated to `LIMIT_DESCRIPTION`.

**Also fix:** The thread-posted completion message in `orchestrator.py:3657-3667` — it appends `output.summary` without any length handling. Since `thread_send` uses `_send_long_message`, this actually works correctly for threads already — but for the non-thread fallback path via `format_task_completed()` in `notifications.py:162`, the summary is included without truncation and then the whole message gets truncated to 2000 chars. Fix by using `_send_long_message` consistently.

## Phase 2: Fix hook response truncation (200 chars is too aggressive)

The hook response summary in `hooks.py:481` truncates to just 200 characters. This makes hook results nearly useless for task-spawning hooks and periodic reports.

**Files to change:**
- `src/hooks.py:481` — Increase from 200 to at least 2000 chars, or better yet, use `_send_long_message` pattern (split/attach for long content).

## Phase 3: Replace bare `[:1997]` truncations in commands.py with proper splitting

Multiple locations in `commands.py` do `msg[:1997] + "..."` which silently loses content. Replace these with calls to a shared utility that either splits the message or attaches it as a file.

**Files to change:**
- `src/discord/commands.py` — All ~11 locations listed in Category 2 above. Create or reuse a helper function that:
  1. If under 2000 chars, send normally
  2. If under 6000 chars, split at line boundaries
  3. If over 6000 chars, attach as file with preview

  Since `commands.py` uses `interaction.response` / `interaction.followup`, the helper needs to work with Discord interactions, not just channels.

## Phase 4: Increase agent summary limit in task-info command

`command_handler.py:3413` truncates `summary[:1000]` in the task-info response. This is a user-facing command where people go specifically to read the full summary.

**Files to change:**
- `src/command_handler.py:3413` — Remove or increase the 1000-char limit. Since the response goes through the LLM chat interface which has its own length handling, a higher limit (4000+) or no limit is fine.

## Phase 5: Increase dependency summary limit for downstream tasks

`orchestrator.py:3127-3128` truncates dependency summaries to 2000 chars. For complex tasks, this cuts off important context that downstream tasks need.

**Files to change:**
- `src/orchestrator.py:3127-3128` — Increase from 2000 to 4000 chars. The dependency summary goes into the agent's prompt, not Discord, so there's no Discord limit concern.
