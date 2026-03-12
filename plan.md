# Test Suite Fix Plan

**Date:** 2026-03-12
**Branch:** stark-rapids/run-full-test-suite-and-generate-fix-plan
**Test Results:** 25 failed, 1 collection error, 1639 passed, 374 skipped, 4 warnings

---

## Background / Root Cause Summary

The 25 test failures + 1 collection error fall into **6 distinct root causes**:

| # | Root Cause | Tests Affected | Severity |
|---|-----------|---------------|----------|
| 1 | `_merge_and_push()` calls `has_remote()` but tests don't mock it | 14 tests | High |
| 2 | `_create_pr_for_task()` doesn't gate on `has_remote()` for LINK repos | 1 test | Medium |
| 3 | `test_setup_wizard_channels.py` uses wrong import path | 1 collection error | Low |
| 4 | `load_config()` validation rejects configs without Discord settings | 2 tests | Medium |
| 5 | `sentence-transformers` not installed; memory tests can't create MemSearch | 6 tests | Medium |
| 6 | Chat eval tool coverage: 13 tools missing test cases + 1 wrong tool name | 2 tests | Low |

---

## Phase 1: Fix `_merge_and_push()` test mocks for `has_remote()` (14 tests)

**Problem:** The orchestrator's `_merge_and_push()` method (src/orchestrator.py:1718) calls `self.git.has_remote(workspace)` to decide whether to use `sync_and_merge` (remote repos) or `merge_branch` (local repos). The tests use `MagicMock(spec=GitManager)` which returns a MagicMock for `has_remote()` — truthy but not a proper boolean. This causes:
- LINK/local tests to incorrectly enter the remote code path → `sync_and_merge` is called instead of `merge_branch` → ValueError on tuple unpacking
- CLONE/remote tests to pass the wrong type to `delete_remote=has_remote` parameter

**Failing tests:**
- `tests/test_merge_and_push.py::TestMergeAndPushClone::test_successful_merge_and_push`
- `tests/test_merge_and_push.py::TestMergeAndPushClone::test_sync_and_merge_not_called_for_link`
- `tests/test_merge_and_push.py::TestMergeAndPushLink::test_link_repo_merges_locally`
- `tests/test_merge_and_push.py::TestMergeAndPushLink::test_link_repo_merge_conflict_notifies`
- `tests/test_merge_and_push.py::TestMergeAndPushLink::test_link_repo_delete_branch_failure_ignored`
- `tests/test_merge_and_push.py::TestMergeAndPushLink::test_link_repo_conflict_recovery_checks_out_default`
- `tests/test_merge_and_push.py::TestMergeAndPushLink::test_link_repo_conflict_recovery_failure_ignored`
- `tests/test_merge_and_push.py::TestMergeAndPushLink::test_link_repo_success_does_not_trigger_recovery`
- `tests/test_rebase_before_merge.py::TestLinkRepoRebaseFallback::test_link_rebase_tried_on_merge_conflict`
- `tests/test_rebase_before_merge.py::TestLinkRepoRebaseFallback::test_link_rebase_fails_still_notifies`
- `tests/test_rebase_before_merge.py::TestLinkRepoRebaseFallback::test_link_no_rebase_when_merge_succeeds`
- `tests/test_rebase_before_merge.py::TestLinkRepoRebaseFallback::test_link_rebase_succeeds_but_retry_merge_fails`
- `tests/test_workspace_sync_comprehensive.py::TestOrchestratorMergeAndPushIntegration::test_success_cleans_up_branch`
- `tests/test_workspace_sync_comprehensive.py::TestOrchestratorMergeAndPushIntegration::test_link_repo_rebase_fallback_on_conflict`

**Fix:** Add `git.has_remote.return_value = True` or `False` to each test's git mock fixture, matching the intended repo type:

### File: `tests/test_merge_and_push.py`
- In the `git` fixture used by `TestMergeAndPushClone`, add: `git.has_remote.return_value = True`
- In the `git` fixture used by `TestMergeAndPushLink`, add: `git.has_remote.return_value = False`

### File: `tests/test_rebase_before_merge.py`
- In the `git` fixture used by `TestLinkRepoRebaseFallback`, add: `git.has_remote.return_value = False`

### File: `tests/test_workspace_sync_comprehensive.py`
- In the `git` fixture used by `TestOrchestratorMergeAndPushIntegration`:
  - For `test_success_cleans_up_branch`: `git.has_remote.return_value = True`
  - For `test_link_repo_rebase_fallback_on_conflict`: `git.has_remote.return_value = False`

---

## Phase 2: Fix `_create_pr_for_task()` for LINK repos (1 test)

**Problem:** `_create_pr_for_task()` (src/orchestrator.py ~line 1820) calls `self.git.has_remote(workspace)` to decide whether to push, but the MagicMock returns a truthy MagicMock instead of `False` for LINK repos, so `push_branch` is called when it shouldn't be.

**Failing test:**
- `tests/test_force_with_lease.py::TestCreatePrForTaskForceWithLease::test_link_repo_skips_push`

**Fix:** In `tests/test_force_with_lease.py`, add `git.has_remote.return_value = False` in the test or the fixture for this test case. Alternatively, the test can set it per-test:
```python
async def test_link_repo_skips_push(self, orch, git):
    git.has_remote.return_value = False
    ...
```

---

## Phase 3: Fix `test_setup_wizard_channels.py` import (1 collection error)

**Problem:** The file imports `from setup_wizard import _step_per_project_channels` but the module is at `src/setup_wizard.py`. All other tests use `from src.setup_wizard import ...`.

**Failing:** Collection error prevents the entire file from loading.

**Fix:** In `tests/test_setup_wizard_channels.py` line 16, change:
```python
from setup_wizard import _step_per_project_channels
```
to:
```python
from src.setup_wizard import _step_per_project_channels
```

---

## Phase 4: Fix `test_agent_profiles.py` config validation (2 tests)

**Problem:** `load_config()` in `src/config.py` always calls `config.validate()` which requires `bot_token` and `guild_id` in the Discord config. The profile tests create minimal YAML configs without Discord settings, so validation raises `ConfigValidationError`.

**Failing tests:**
- `tests/test_agent_profiles.py::TestConfigProfileLoading::test_load_profiles_from_yaml`
- `tests/test_agent_profiles.py::TestConfigProfileLoading::test_no_profiles_section`

**Fix options (choose one):**
1. **Option A (recommended):** Add minimal Discord config to the test YAML fixtures:
   ```yaml
   discord:
     bot_token: "test-token"
     guild_id: "12345"
   ```
2. **Option B:** Mock `config.validate()` to return no errors in these tests
3. **Option C:** Change `load_config()` to make Discord validation non-fatal (warning instead of error) — but this may have wider implications

---

## Phase 5: Fix memory integration tests (6 tests)

**Problem:** All `TestMemoryEndToEnd` tests create a `MemoryManager` with `embedding_provider="local"`, which requires the `sentence-transformers` Python package. This package is not installed in the test environment, so `MemSearch` initialization fails and returns `None`.

**Failing tests:**
- `tests/test_memory_integration.py::TestMemoryEndToEnd::test_index_and_search_roundtrip`
- `tests/test_memory_integration.py::TestMemoryEndToEnd::test_remember_then_recall`
- `tests/test_memory_integration.py::TestMemoryEndToEnd::test_multiple_projects_isolated`
- `tests/test_memory_integration.py::TestMemoryEndToEnd::test_notes_directory_indexed`
- `tests/test_memory_integration.py::TestMemoryEndToEnd::test_reindex_after_file_deletion`
- `tests/test_memory_integration.py::TestMemoryEndToEnd::test_hook_memory_search_step_roundtrip`

**Fix options (choose one):**
1. **Option A (recommended):** Add `pytest.importorskip("sentence_transformers")` at the top of the test class or module, so tests are automatically skipped when the dependency is missing
2. **Option B:** Add `sentence-transformers` to dev dependencies in pyproject.toml
3. **Option C:** Mark the tests with `@pytest.mark.integration` and skip by default

---

## Phase 6: Fix chat eval tool coverage (2 tests)

**Problem 1:** 13 tools defined in `TOOLS` (in `src/chat_agent.py`) have no corresponding test cases in `tests/chat_eval/test_cases/`:
- `check_profile`, `create_profile`, `delete_profile`, `edit_profile`, `export_profile`
- `find_merge_conflict_workspaces`, `get_profile`, `git_pull`, `import_profile`
- `install_profile`, `list_available_tools`, `list_profiles`, `sync_workspaces`

**Problem 2:** One test case references `push_branch` which doesn't exist in TOOLS (the actual tool is `git_push`).

**Failing tests:**
- `tests/chat_eval/test_tool_coverage.py::test_all_tools_have_test_cases`
- `tests/chat_eval/test_tool_coverage.py::test_no_test_cases_reference_nonexistent_tools`

**Fix:**
1. Create test case entries in `tests/chat_eval/test_cases/` for each of the 13 missing tools (or add them to existing category files)
2. Find the test case that references `push_branch` and rename it to `git_push`

---

## Priority Order

1. **Phase 1** — Highest impact (14 tests), simple mock fix
2. **Phase 2** — Same root cause pattern as Phase 1 (1 test)
3. **Phase 3** — Trivial one-line import fix (1 collection error)
4. **Phase 4** — Simple test fixture fix (2 tests)
5. **Phase 5** — Dependency/skip issue (6 tests)
6. **Phase 6** — Requires writing new test case content (2 tests, most effort)
