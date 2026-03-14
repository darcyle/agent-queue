# Test Suite Results & Fix Plan — 2026-03-14

## Test Run Summary

- **Total:** 2,122 tests
- **Passed:** 1,727
- **Skipped:** 393
- **Failed:** 2
- **Warnings:** 4
- **Duration:** ~4m 51s

---

## Failing Tests

### 1. `test_all_tools_have_test_cases` (Medium Priority)

**File:** `tests/chat_eval/test_tool_coverage.py:32`

**Error:**
```
AssertionError: 2 tool(s) have no test cases:
  - remove_workspace
  - set_default_branch
```

**Root Cause:** The `remove_workspace` and `set_default_branch` tools were added to the `TOOLS` list in `src/chat_agent.py`, but no corresponding chat-eval test cases exist in `tests/chat_eval/test_cases/`.

**Fix:**
- Add at least 2 test cases for `remove_workspace` to `tests/chat_eval/test_cases/workspaces.py`
- Add at least 2 test cases for `set_default_branch` to `tests/chat_eval/test_cases/projects.py`
- Each test case should follow the existing `TestCase` dataclass format with realistic user prompts and expected tool invocations

---

### 2. `test_arun_timeout` (High Priority)

**File:** `tests/test_git_manager_async.py:92`

**Error:**
```
ProcessLookupError
```

**Root Cause:** Race condition in `src/git/manager.py:_arun` (line 134). When a timeout fires and the subprocess has already exited before `proc.kill()` is called, `proc.kill()` raises `ProcessLookupError` instead of the expected `GitError`. The current code at lines 133-139 is:

```python
except asyncio.TimeoutError:
    proc.kill()           # <-- raises ProcessLookupError if process already exited
    await proc.wait()
    raise GitError(...)
```

**Fix:** Wrap `proc.kill()` in a try/except to handle `ProcessLookupError`:

```python
except asyncio.TimeoutError:
    try:
        proc.kill()
    except ProcessLookupError:
        pass  # Process already exited
    await proc.wait()
    raise GitError(
        f"git {' '.join(args)} timed out after "
        f"{effective_timeout}s (possible credential prompt)"
    )
```

---

## Prioritized Fix List

| # | Test | Priority | Effort | Fix Type |
|---|------|----------|--------|----------|
| 1 | `test_arun_timeout` | High | Small | Bug fix in `src/git/manager.py` — add try/except around `proc.kill()` |
| 2 | `test_all_tools_have_test_cases` | Medium | Small | Add chat-eval test cases for `remove_workspace` and `set_default_branch` |

Both fixes are small, isolated changes with no risk of side effects.
