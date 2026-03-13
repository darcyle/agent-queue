# Explore if it's possible to get usage information from claude code (percentage used per session, whe

**Task ID:** `steady-delta`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `steady-delta/explore-if-its-possible-to-get-usage-information-from-claude-code-percentage-used-per-session-whe`
**Archived:** 2026-03-13 16:38:10

## Description

Explore if it's possible to get usage information from claude code (percentage used per session, when it resets, percentage used per week) and expose it through a discord command so we can track our usage remotely as well.

---
**Reopen Feedback:**
Now that you have done the research and it appears possible, please implement the functionality.

## Result

**Summary:** The working tree is clean. The implementation is already fully committed and working. Here's a summary of what's implemented:

## Implementation Summary

The `/claude-usage` Discord command is fully implemented across 3 files:

### `src/command_handler.py`
- **`_cmd_claude_usage()`** — Reads from 3 data sources:
  1. **`~/.claude/stats-cache.json`** — Total sessions, messages, model token usage (input/output/cache), daily activity for weekly summaries
  2. **`~/.claude/.credentials.json`** — Subscription type and rate limit tier
  3. **API rate-limit probe** — Real-time utilization percentages and reset times

- **`_probe_claude_rate_limit()`** — Sends a minimal 1-token API request using OAuth token from credentials, parses `anthropic-ratelimit-unified-*` response headers for status, utilization percentages, and reset timestamps

### `src/discord/commands.py`
- **`/claude-usage` slash command** — Formats the data with:
  - Plan & tier info
  - Rate limit status with color-coded emoji (🟢/🟡/🔴) and progress bars
  - Reset time with countdown
  - Weekly activity summary (sessions, messages, tool calls, tokens by model)
  - All-time token usage by model

### `tests/test_claude_usage.py`
- 5 tests covering: stats parsing, missing files, error handling, OAuth flow, and no-credentials fallback — all passing ✅

No additional changes needed — the feature was already implemented and committed in commit `336b15d`.

**Tokens Used:** 4,477
