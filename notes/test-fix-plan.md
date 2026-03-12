# Test Suite Results — 2026-03-12

## Summary

**All tests pass. No fixes are needed.**

- **Passed:** 1,672
- **Skipped:** 393
- **Failed:** 0
- **Errors:** 0
- **Duration:** 283.72s (4m 43s)

## Details

The full test suite was run with `pytest -v` on the `fleet-forge/run-full-test-suite-and-generate-fix-plan` branch (rebased on latest `origin/main`).

### Warnings (non-blocking)

1. **DeprecationWarning** — `audioop` module (used by `discord/player.py`) is deprecated and slated for removal in Python 3.13. This is a third-party dependency issue, not project code.
2. **PytestUnknownMarkWarning** — `pytest.mark.eval` is not registered as a custom mark in `tests/chat_eval/test_eval_integration.py:26`.
3. **PytestCollectionWarning** (×2) — `TestCaseResult` and `TestCase` dataclasses have `__init__` constructors so pytest cannot collect them as test classes. These are data classes, not test classes, so the warning is harmless.

### Skipped Tests (393)

The skipped tests are expected — they are gated behind optional dependencies (e.g., `sentence-transformers` for memory tests) or conditional markers for integration/chain tests that require specific environments.

## Conclusion

No action items. The test suite is fully green.
