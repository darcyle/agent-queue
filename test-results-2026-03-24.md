# Chat Evaluation Test Results — 2026-03-24

**Run timestamp:** 2026-03-24T04:46:15 (nightly hook, first run)
**Branch:** nimble-meadow/run-nightly-chat-evaluation-tests
**Rule ID:** rule-nightly-evaluation-tests

## Summary

| Metric | Count |
|--------|-------|
| **Passed** | 25 |
| **Failed** | 0 |
| **Skipped** | 401 |
| **Warnings** | 4 |
| **Duration** | 11.66s |

**Result: ALL TESTS PASSING**

## Breakdown by Test File

### `test_deterministic.py` — 14 passed
Deterministic tests using ScriptedProvider (no LLM calls needed):
- SingleToolCall (3 tests)
- MultiToolSequence (2 tests)
- MaxIterations (2 tests)
- ToolErrorPropagation (2 tests)
- ActiveProject (2 tests)
- HistoryThreading (3 tests)

### `test_eval_integration.py` — 7 passed, 401 skipped
- 7 deterministic eval cases passed (scripted provider)
- 401 eval cases skipped — these require `ANTHROPIC_API_KEY` for live LLM evaluation

### `test_tool_coverage.py` — 4 passed
- `test_all_tools_have_test_cases` — PASSED (all tools have eval coverage)
- `test_no_test_cases_reference_nonexistent_tools` — PASSED
- `test_no_duplicate_case_ids` — PASSED
- `test_minimum_case_count` — PASSED

## Warnings (non-blocking)
1. `audioop` deprecation in discord.py (Python 3.13 removal)
2. Unknown `pytest.mark.eval` mark (cosmetic)
3. `TestCaseResult` collection warning (has `__init__`)
4. `TestCase` collection warning (has `__init__`)

## Observations
- No failures or regressions detected
- 401 skipped tests are expected — they require a live API key for LLM-based evaluation
- Tool coverage is complete — all registered tools have test cases
- No follow-up bugfix tasks needed
