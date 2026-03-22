---
auto_tasks: true
---

# Supervisor Response Quality Improvements

## Background & Root Cause Analysis

### What Happened (Screenshot Analysis)

The user (ElectricJack) sent a complex multi-part request to the agent-queue bot:

1. **Check for recent code changes** since last run
2. **If changes exist**, create a task to run the entire test suite and fix issues
3. **If more than a couple issues**, create a plan.md in .claude folder for fixing failures
4. **Don't check the plan in**

The bot responded with: `"Done. Actions taken: browse_tools, list_tasks, load_tools, git_log(agent-queue)"`

This is a **terrible response** — it just listed tool names without:
- Reporting whether there were changes
- Saying whether a task was created
- Addressing any of the 4 parts of the request
- Providing any actionable information whatsoever

### Why the Supervisor Architecture Failed to Catch This

**Root Cause 1: The "Done. Actions taken: ..." fallback is a quality black hole (supervisor.py lines 323-324)**

When the LLM stops calling tools but returns no text response, the code falls through to a hardcoded fallback: `return f"Done. Actions taken: {', '.join(tool_actions)}"`. This bypasses ANY quality evaluation. The LLM essentially "gave up" after 4 tool calls without synthesizing results or taking the requested actions, and the code treated that as success.

**Root Cause 2: Reflection runs but its output is discarded (supervisor.py lines 307-319)**

The reflection pass happens AFTER the response is composed but BEFORE it's returned. However:
- `action_results` is always passed as `[]` (empty list) — reflection never sees what the tools actually returned
- The reflection's output/verdict is completely ignored — it doesn't modify the response
- Even if reflection detected a poor response, there's no mechanism to retry or improve it

**Root Cause 3: No response-quality gate before returning to user**

There is no check between generating a response and sending it to the user that evaluates whether the response actually addresses the original query. The system has plan quality scoring (`_score_parse_quality`, `validate_plan_quality`) but zero equivalent for chat responses.

**Root Cause 4: Tool results are not accumulated for synthesis**

The chat loop collects `tool_actions` (labels) but not `tool_results` (actual data returned). When the LLM stops without a text response, there's no accumulated context to fall back on or to feed into a retry prompt.

---

## Phase 1: Accumulate tool results and feed them to reflection

**Files:** `src/supervisor.py`

Currently `action_results=[]` is always passed to `reflect()`. Instead, accumulate tool results during the chat loop and pass them to reflection so it can evaluate whether the actions actually succeeded.

Changes:
- Add a `tool_result_pairs: list[dict]` accumulator in the chat loop (alongside `tool_actions`)
- After each `_execute_tool` call, append `{"tool": label, "result": json.dumps(result)[:500]}` to the accumulator
- Pass this accumulator as `action_results` to `reflect()` instead of `[]`

## Phase 2: Replace the "Done. Actions taken: ..." fallback with a synthesis step

**Files:** `src/supervisor.py`

The lazy fallback at lines 323-324 should never be the final response for a user request. When the LLM stops calling tools without providing text, force a synthesis turn.

Changes:
- When `not resp.tool_uses` and `not response` and `tool_actions` is non-empty:
  - Instead of returning the fallback string immediately, append a synthesis prompt to messages:
    ```
    "You used several tools but didn't provide a response to the user. Please summarize what you found and what actions you took. Address each part of the user's original request. If you didn't complete something, explain why."
    ```
  - Make one more LLM call with this prompt (no tools, just text generation)
  - Use the LLM's response (or fall back to the action list only if this also fails)
- Keep the fallback as absolute last resort (synthesis attempt also returns empty)
- Also apply this synthesis step when the `max_rounds` loop exhausts (lines 354-356)

## Phase 3: Make reflection output actionable — retry on poor quality

**Files:** `src/supervisor.py`, `src/reflection.py`

Currently reflection's output is completely discarded. Change `reflect()` to return a verdict that the chat loop can act on.

Changes to `reflection.py`:
- Add a `ReflectionVerdict` dataclass with fields: `passed: bool`, `reason: str`, `suggested_followup: str | None`
- Update the standard and deep reflection prompts to request a structured JSON verdict at the end: `{"passed": true/false, "reason": "...", "followup": "..."}`
- Add a `parse_verdict(text: str) -> ReflectionVerdict` method that extracts the JSON from the LLM response

Changes to `supervisor.py`:
- Change `reflect()` return type from `None` to `ReflectionVerdict | None`
- In the chat loop after reflection: if verdict is `passed=False`, re-enter the tool loop with a retry prompt that includes the reflection's reason and the original user request
- Limit retries to 1 additional attempt (avoid infinite loops)
- On retry, include the original user text and the reflection feedback as context

## Phase 4: Add a lightweight response adequacy heuristic

**Files:** `src/supervisor.py`

Add a cheap deterministic check that catches obviously inadequate responses before they reach the user, without needing an LLM call.

Changes:
- Add `_check_response_adequacy(user_text: str, response: str, tool_actions: list[str]) -> bool`
- Returns `False` (inadequate) when:
  - Response matches the generic fallback pattern `"Done. Actions taken: ..."`
  - User message has 3+ numbered items or bullet points AND response is under 100 chars
  - Response contains no information beyond tool names (no nouns/verbs from the domain)
- When inadequate, trigger the synthesis step from Phase 2 before returning
- This is a safety net that catches cases even when reflection is disabled or circuit-breaker is tripped
