# Run test suite and generate fix plan

**Task ID:** `clear-impact`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `clear-impact/run-test-suite-and-generate-fix-plan`
**Archived:** 2026-03-13 16:38:09

## Description

Run the complete test suite for the agent-queue project and analyze the results:

1. Locate and execute the project's test suite (e.g., `npm test`, `pytest`, or the project's test command)
2. Capture all test output, including any failed tests and error messages
3. Identify the root causes of any test failures
4. Generate a detailed plan outlining what code changes need to be made to fix the issues
5. Write the findings and proposed fixes to `/home/jkern/agent-queue-workspaces/agent-queue/notes/test-results-and-fix-plan.md`

Include specific file paths, error messages, and step-by-step fixes in the plan.

Workspace path: /home/jkern/agent-queue-workspaces/agent-queue

## Result

**Summary:** Done. Here's a summary of what I found:

## Test Suite Results: 2 failed, 1,680 passed, 393 skipped (4m 28s)

### Two Failures Found:

1. **`test_all_tools_have_test_cases`** — The `set_default_branch` tool was added to the TOOLS list but has no chat-eval test cases. **Fix:** Add 2+ test cases to `tests/chat_eval/test_cases/projects.py`.

2. **`test_uses_yes_flag`** — A previous commit correctly removed the invalid `--yes` flag from `create_github_repo()`, but the test still asserts it should be present. **Fix:** Update the test to assert `--yes` is *absent*.

Both fixes are test-only changes. The detailed plan with code snippets has been written to `notes/test-results-and-fix-plan.md` and committed.

**Tokens Used:** 4,710
