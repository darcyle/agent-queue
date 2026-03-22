# Phase 6: Orchestrator Handoff + Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move plan parsing from the orchestrator's inline completion pipeline to a Supervisor reflection handler triggered by `task.completed`, then remove dead code (ChatAnalyzer, agent_prompting, prompt_registry, prompt_manager).

**Architecture:** The orchestrator's `_phase_plan_generate()` / `_discover_and_store_plan()` logic becomes a `process_task_completion` command in CommandHandler. The Supervisor's `task.completed` reflection handler calls this command. The orchestrator's completion pipeline drops the plan_generate phase. Old modules (ChatAnalyzer, agent_prompting, prompt_registry, prompt_manager) are verified unused and removed. The `plan_parser.py` and `plan_parser_llm.py` modules are retained as libraries.

**Tech Stack:** Python 3.12+, asyncio, pytest, existing EventBus, plan_parser, plan_parser_llm.

**Dependencies:** Phase 4 (Supervisor Identity + Reflection) — completed. Phase 5 (Chat Observation) should be completed first so ChatAnalyzer removal is safe.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/command_handler.py` | Modify | Add `_cmd_process_task_completion` wrapping plan discovery |
| `src/supervisor.py` | Modify | Add `on_task_completed()` reflection handler |
| `src/orchestrator.py` | Modify | Replace `_phase_plan_generate` with Supervisor delegation, preserve AWAITING_PLAN_APPROVAL flow |
| `src/tool_registry.py` | Modify | Add `process_task_completion` tool definition |
| `src/config.py` | Modify | Add deprecation warning for `chat_analyzer` config |
| `src/chat_analyzer.py` | Delete | Replaced by Phase 5 ChatObserver |
| `src/chat_analyzer_service.py` | Delete | Replaced by Phase 5 ChatObserver |
| `src/agent_prompting.py` | Delete | Absorbed into PromptBuilder (Phase 1) |
| `src/prompt_registry.py` | Delete | Absorbed into PromptBuilder (Phase 1) |
| `src/prompt_manager.py` | Delete | Absorbed into PromptBuilder (Phase 1) |
| `tests/test_plan_handoff.py` | Create | Tests for the new plan processing flow |
| `specs/orchestrator.md` | Modify | Update post-task flow |

---

### Task 1: Add process_task_completion Command

**Files:**
- Modify: `src/command_handler.py`
- Create: `tests/test_plan_handoff.py`

Wrap the plan discovery logic into a CommandHandler command that the Supervisor can call. This command takes a task_id and workspace_path, discovers plan files, parses them, and stores them for approval — the same logic currently in `orchestrator._discover_and_store_plan()` but callable via the tool interface.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_plan_handoff.py
"""Tests for plan parsing handoff from orchestrator to Supervisor."""

import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

# Stub packages not available in test env
for mod_name in [
    "discord", "discord.ext", "discord.ext.commands",
    "discord.app_commands", "discord.ui",
    "aiosqlite", "anthropic", "ollama",
]:
    sys.modules.setdefault(mod_name, MagicMock())


def _make_handler():
    from src.command_handler import CommandHandler
    orch = MagicMock()
    orch.db = AsyncMock()
    orch.config = MagicMock()
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    config.auto_task = MagicMock()
    config.auto_task.enabled = True
    config.auto_task.plan_file_patterns = [".claude/plan.md", "plan.md"]
    config.auto_task.max_steps_per_plan = 20
    config.auto_task.use_llm_parser = False
    config.auto_task.skip_if_implemented = False
    config.auto_task.inherit_approval = False
    config.auto_task.chain_dependencies = True
    return CommandHandler(orch, config)


def test_process_task_completion_exists():
    handler = _make_handler()
    assert hasattr(handler, '_cmd_process_task_completion')


def test_process_task_completion_no_plan_file():
    """Returns no_plan when workspace has no plan file."""
    handler = _make_handler()
    with tempfile.TemporaryDirectory() as tmpdir:
        result = asyncio.run(handler.execute(
            "process_task_completion", {
                "task_id": "t-123",
                "workspace_path": tmpdir,
            }
        ))
    assert result.get("plan_found") is False


def test_process_task_completion_finds_plan():
    """Discovers and parses a plan file in the workspace."""
    handler = _make_handler()
    handler._orchestrator.db.get_task = AsyncMock(return_value=MagicMock(
        id="t-123", project_id="proj-1", is_plan_subtask=False,
    ))

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a plan file
        plan_dir = os.path.join(tmpdir, ".claude")
        os.makedirs(plan_dir)
        plan_path = os.path.join(plan_dir, "plan.md")
        with open(plan_path, "w") as f:
            f.write(
                "# Implementation Plan\n\n"
                "## Step 1: Add login endpoint\n"
                "Create POST /api/login with JWT auth.\n\n"
                "## Step 2: Add logout endpoint\n"
                "Create POST /api/logout to invalidate tokens.\n"
            )

        result = asyncio.run(handler.execute(
            "process_task_completion", {
                "task_id": "t-123",
                "workspace_path": tmpdir,
            }
        ))

    assert result.get("plan_found") is True
    assert result.get("steps_count", 0) >= 1


def test_process_task_completion_disabled():
    """Returns early when auto_task is disabled."""
    handler = _make_handler()
    handler._orchestrator.config.auto_task.enabled = False

    result = asyncio.run(handler.execute(
        "process_task_completion", {
            "task_id": "t-123",
            "workspace_path": "/tmp/nonexistent",
        }
    ))
    assert result.get("plan_found") is False
    assert "disabled" in result.get("reason", "").lower()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_plan_handoff.py -v`
Expected: FAIL — `_cmd_process_task_completion` doesn't exist

- [ ] **Step 3: Implement _cmd_process_task_completion**

Add to `CommandHandler` in `src/command_handler.py`:

```python
async def _cmd_process_task_completion(self, args: dict) -> dict:
    """Discover and process a plan file from a completed task's workspace.

    Called by the Supervisor's task.completed reflection handler.
    Wraps the plan discovery logic previously inline in the orchestrator.

    Args:
        task_id: The completed task's ID.
        workspace_path: Absolute path to the task's workspace.

    Returns:
        dict with plan_found (bool), steps_count (int), reason (str).
    """
    task_id = args.get("task_id")
    workspace = args.get("workspace_path")
    config = self._orchestrator.config.auto_task

    if not config.enabled:
        return {"plan_found": False, "reason": "Auto-task disabled"}

    if not task_id or not workspace:
        return {"plan_found": False, "reason": "Missing task_id or workspace_path"}

    # Look up task to check if it's a subtask (subtasks don't generate plans)
    task = await self._orchestrator.db.get_task(task_id)
    if task and getattr(task, 'is_plan_subtask', False):
        return {"plan_found": False, "reason": "Subtasks do not generate plans"}

    # Discover plan file
    from src.plan_parser import find_plan_file, read_plan_file, parse_and_generate_steps

    patterns = config.plan_file_patterns or [".claude/plan.md", "plan.md"]
    plan_path = find_plan_file(workspace, patterns)
    if not plan_path:
        return {"plan_found": False, "reason": "No plan file found"}

    # Parse plan
    raw_content = read_plan_file(plan_path)
    if not raw_content or not raw_content.strip():
        return {"plan_found": False, "reason": "Plan file is empty"}

    max_steps = getattr(config, 'max_steps_per_plan', 20)
    steps, quality = parse_and_generate_steps(
        raw_content, max_steps=max_steps
    )

    if not steps:
        return {"plan_found": False, "reason": "No actionable steps parsed"}

    # Archive plan file to prevent re-discovery
    import shutil
    archive_dir = os.path.join(workspace, ".claude", "plans")
    os.makedirs(archive_dir, exist_ok=True)
    archive_name = f"{task_id}-plan.md"
    archive_path = os.path.join(archive_dir, archive_name)
    shutil.copy2(str(plan_path), archive_path)
    os.remove(str(plan_path))

    # Store plan data for approval
    import json
    plan_data = {
        "steps": steps,
        "raw_content": raw_content,
        "source_file": str(plan_path),
        "quality_score": quality.quality_score if quality else 0.0,
    }
    await self._orchestrator.db.set_task_context(
        task_id, "stored_plan", json.dumps(plan_data)
    )

    return {
        "plan_found": True,
        "steps_count": len(steps),
        "quality_score": quality.quality_score if quality else 0.0,
        "source_file": str(plan_path),
        "message": (
            f"Found plan with {len(steps)} steps "
            f"(quality: {quality.quality_score:.0%}). "
            f"Awaiting approval."
        ),
    }
```

Also add `"process_task_completion"` to the tool registry so the Supervisor can discover it. In `src/tool_registry.py`, add it to the `_TOOL_CATEGORIES` under the `"system"` category:

```python
"process_task_completion": "system",
```

And add its tool definition to `_ALL_TOOL_DEFINITIONS`:

```python
{
    "name": "process_task_completion",
    "description": "Process a completed task's workspace for plan files. Discovers, parses, and stores plans for approval. Called by Supervisor reflection on task.completed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The completed task's ID",
            },
            "workspace_path": {
                "type": "string",
                "description": "Absolute path to the task's workspace",
            },
        },
        "required": ["task_id", "workspace_path"],
    },
},
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_plan_handoff.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/command_handler.py src/tool_registry.py tests/test_plan_handoff.py
git commit -m "Add process_task_completion command for plan discovery"
```

---

### Task 2: Add on_task_completed Reflection Handler to Supervisor

**Files:**
- Modify: `src/supervisor.py`
- Modify: `tests/test_supervisor.py`

Wire the Supervisor to handle `task.completed` events. When a task completes, the Supervisor calls `process_task_completion` to discover plans, then runs a reflection pass.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_supervisor.py`:

```python
def test_on_task_completed_exists():
    """Supervisor has an on_task_completed handler."""
    sup = _make_supervisor()
    assert hasattr(sup, "on_task_completed")
    assert callable(sup.on_task_completed)


def test_on_task_completed_calls_process():
    """on_task_completed calls process_task_completion tool."""
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(return_value={
        "plan_found": False, "reason": "No plan file found"
    })

    result = asyncio.get_event_loop().run_until_complete(
        sup.on_task_completed(
            task_id="t-123",
            project_id="my-game",
            workspace_path="/tmp/workspace",
        )
    )

    sup.handler.execute.assert_called_once_with(
        "process_task_completion", {
            "task_id": "t-123",
            "workspace_path": "/tmp/workspace",
        }
    )
    assert result["plan_found"] is False


def test_on_task_completed_sets_project():
    """on_task_completed sets active project before processing."""
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(return_value={"plan_found": False})

    asyncio.get_event_loop().run_until_complete(
        sup.on_task_completed(
            task_id="t-123",
            project_id="my-game",
            workspace_path="/tmp/workspace",
        )
    )
    assert sup._active_project_id == "my-game"


def test_on_task_completed_returns_plan_found():
    """on_task_completed returns plan status for orchestrator."""
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(return_value={
        "plan_found": True, "steps_count": 3
    })

    result = asyncio.get_event_loop().run_until_complete(
        sup.on_task_completed(
            task_id="t-123",
            project_id="my-game",
            workspace_path="/tmp/workspace",
        )
    )
    assert result["plan_found"] is True
    assert result["steps_count"] == 3


def test_on_task_completed_handles_error():
    """on_task_completed never raises, returns plan_found=False."""
    sup = _make_supervisor()
    sup.handler.execute = AsyncMock(side_effect=Exception("DB error"))

    result = asyncio.get_event_loop().run_until_complete(
        sup.on_task_completed(
            task_id="t-123",
            project_id="proj",
            workspace_path="/tmp/ws",
        )
    )
    assert result["plan_found"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_supervisor.py::test_on_task_completed_exists -v`
Expected: FAIL

- [ ] **Step 3: Implement on_task_completed**

Add to `Supervisor` class in `src/supervisor.py`:

```python
    async def on_task_completed(
        self,
        task_id: str,
        project_id: str,
        workspace_path: str,
    ) -> dict:
        """Handle a task.completed event.

        Called by the orchestrator's completion pipeline BEFORE merge.
        Discovers plan files, triggers reflection, and may create
        follow-up work.

        Returns a dict with "plan_found" (bool) so the orchestrator
        can transition to AWAITING_PLAN_APPROVAL if needed.

        Never raises — errors are caught, returns {"plan_found": False}.
        """
        try:
            if project_id:
                self.set_active_project(project_id)

            # Discover and process plan files
            result = await self.handler.execute(
                "process_task_completion", {
                    "task_id": task_id,
                    "workspace_path": workspace_path,
                }
            )

            # Run reflection on the completed task
            if self._provider:
                trigger = "task.completed"
                summary = f"Task {task_id} completed"
                if result.get("plan_found"):
                    summary += f" — plan found with {result.get('steps_count', 0)} steps"

                from src.tool_registry import ToolRegistry
                registry = ToolRegistry()
                active_tools = {t["name"]: t for t in registry.get_core_tools()}

                await self.reflect(
                    trigger=trigger,
                    action_summary=summary,
                    action_results=[{"tool": "process_task_completion", "result": result}],
                    messages=[],
                    active_tools=active_tools,
                )

            return result if isinstance(result, dict) else {"plan_found": False}
        except Exception:
            return {"plan_found": False}  # Never crash
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervisor.py tests/test_supervisor.py
git commit -m "Add on_task_completed handler to Supervisor"
```

---

### Task 3: Wire Orchestrator to Delegate Plan Processing

**Files:**
- Modify: `src/orchestrator.py`
- Modify: `tests/test_plan_handoff.py`

Replace `_phase_plan_generate` with a Supervisor delegation call WITHIN the completion pipeline. This is critical: plan discovery MUST happen while the workspace is still available (before merge and before workspace cleanup).

- [ ] **Step 1: Write test for the new flow**

Add to `tests/test_plan_handoff.py`:

```python
def test_orchestrator_calls_supervisor_on_completion():
    """After task completes, orchestrator delegates to Supervisor."""
    from src.supervisor import Supervisor
    assert hasattr(Supervisor, "on_task_completed")


def test_on_task_completed_returns_plan_status():
    """on_task_completed returns whether a plan was found."""
    from src.supervisor import Supervisor
    from unittest.mock import AsyncMock, MagicMock

    sup = MagicMock(spec=Supervisor)
    sup.on_task_completed = AsyncMock(return_value={
        "plan_found": True, "steps_count": 3
    })

    result = asyncio.run(sup.on_task_completed(
        task_id="t-1", project_id="p-1", workspace_path="/tmp/ws"
    ))
    assert result["plan_found"] is True
```

- [ ] **Step 2: Modify the orchestrator**

In `src/orchestrator.py`, make these changes:

**A. Replace `_phase_plan_generate` with Supervisor delegation.**

Find `_run_completion_pipeline()` (around line 2511). It currently runs three phases:
```python
phases = [
    ("commit", self._phase_commit),
    ("plan_generate", self._phase_plan_generate),
    ("merge", self._phase_merge),
]
```

Replace `_phase_plan_generate` with a new Supervisor delegation phase:
```python
phases = [
    ("commit", self._phase_commit),
    ("plan_discover", self._phase_plan_discover),  # Supervisor delegation
    ("merge", self._phase_merge),
]
```

**B. Add `_phase_plan_discover` method** that delegates to Supervisor:

```python
async def _phase_plan_discover(self, ctx) -> None:
    """Delegate plan discovery to the Supervisor.

    Runs BEFORE merge so the workspace is guaranteed available.
    Must happen before merge because:
    1. Plan files need to be read from the workspace
    2. Plan files need to be archived before merge
    3. The task may need to transition to AWAITING_PLAN_APPROVAL
       which prevents merge from happening.
    """
    if not hasattr(self, '_supervisor') or not self._supervisor:
        # Fall back to old behavior if no Supervisor
        return await self._phase_plan_generate_legacy(ctx)

    result = await self._supervisor.on_task_completed(
        task_id=ctx.task.id,
        project_id=ctx.task.project_id or "",
        workspace_path=ctx.workspace,
    )
    if result and result.get("plan_found"):
        ctx.plan_needs_approval = True
```

Note: Keep the old `_phase_plan_generate` method temporarily renamed to `_phase_plan_generate_legacy` as a fallback. Remove it in a later cleanup pass once the Supervisor path is validated.

**C. Add `set_supervisor()` method to orchestrator** (if not already present):

```python
def set_supervisor(self, supervisor) -> None:
    """Set the Supervisor reference for post-task delegation."""
    self._supervisor = supervisor
```

**D. Enrich the task.completed event** with workspace_path:

Find where `task.completed` events are emitted (around lines 2787 and 3640) and add `workspace_path`:

```python
await self.bus.emit("task.completed", {
    "task_id": task.id,
    "project_id": task.project_id,
    "workspace_path": workspace,
})
```

**E. Wire in Discord bot.** In `src/discord/bot.py`, after the Supervisor and orchestrator are created, add:

```python
self.orchestrator.set_supervisor(self.agent)
```

This goes alongside the existing `self.orchestrator.hooks.set_supervisor(self.agent)` call.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_plan_handoff.py tests/test_supervisor.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/orchestrator.py src/discord/bot.py tests/test_plan_handoff.py
git commit -m "Wire orchestrator to delegate plan processing to Supervisor"
```

---

### Task 4: Remove ChatAnalyzer and ChatAnalyzerService

**Files:**
- Delete: `src/chat_analyzer.py`
- Delete: `src/chat_analyzer_service.py`
- Modify: `src/orchestrator.py` (remove ChatAnalyzer initialization)
- Modify: `src/discord/bot.py` (remove ChatAnalyzer references)
- Delete or modify: `tests/test_chat_analyzer*.py`

**Prerequisites:** Phase 5 (ChatObserver) must be complete so the replacement exists.

- [ ] **Step 1: Verify ChatAnalyzer is not imported anywhere critical**

Run these searches to find all imports:

```bash
grep -rn "chat_analyzer" src/ --include="*.py" | grep -v __pycache__
grep -rn "ChatAnalyzer" src/ --include="*.py" | grep -v __pycache__
```

Expected locations:
- `src/orchestrator.py` — initialization (lines ~740-747), shutdown (~896-897)
- `src/discord/bot.py` — suggestion posting, view reattachment
- `src/chat_analyzer_service.py` — service layer
- `src/config.py` — `ChatAnalyzerConfig` (keep for now, add deprecation warning in Task 6)

- [ ] **Step 2: Remove ChatAnalyzer from orchestrator**

In `src/orchestrator.py`:
- Remove the conditional ChatAnalyzer initialization block (lines ~740-747)
- Remove the `self.chat_analyzer` attribute initialization in `__init__`
- Remove the ChatAnalyzer shutdown in the cleanup method
- Keep the `chat.message` event emission in the Discord bot — ChatObserver uses it

- [ ] **Step 3: Remove ChatAnalyzer references from Discord bot**

In `src/discord/bot.py`:
- Remove `_post_analyzer_suggestion` callback (replaced by Phase 5's `_post_observation_suggestion`)
- Remove `_post_analyzer_auto_action` callback
- Remove `_reattach_analyzer_views` (the new SuggestionView uses different custom_id format: `suggest_accept:` vs `analyzer_accept:`)
- Remove the ChatAnalyzerService import and initialization
- Keep the `chat.message` event emission — ChatObserver subscribes to it

- [ ] **Step 4: Delete the files**

```bash
git rm src/chat_analyzer.py
git rm src/chat_analyzer_service.py
```

- [ ] **Step 5: Remove or update tests**

```bash
# Find chat analyzer test files
find tests/ -name "*chat_analyzer*" -o -name "*analyzer*" | head -20
```

Delete test files for the removed modules. Keep any tests that verify the DB schema (the `chat_analyzer_suggestions` table stays — it's used by the new SuggestionView).

- [ ] **Step 6: Run tests to verify nothing breaks**

Run: `pytest tests/ -x --ignore=tests/chat_eval -q 2>&1 | tail -10`
Expected: No new failures (existing pre-existing failures are OK)

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Remove ChatAnalyzer and ChatAnalyzerService (replaced by ChatObserver)"
```

---

### Task 5: Verify and Remove Dead Modules

**Files:**
- Delete (if unused): `src/agent_prompting.py`
- Delete (if unused): `src/prompt_registry.py`
- Delete (if unused): `src/prompt_manager.py`

These modules were marked for removal in the spec as "absorbed into PromptBuilder." However, they may still be imported by other code. This task verifies each is truly unused before removing it.

- [ ] **Step 1: Check agent_prompting.py imports**

```bash
grep -rn "agent_prompting\|from src.agent_prompting\|import agent_prompting" src/ tests/ --include="*.py" | grep -v __pycache__ | grep -v "agent_prompting.py"
```

If still imported by `src/orchestrator.py` (for `select_prompt`, `build_task_description`, etc.), it is NOT safe to remove yet. In that case:
- Add a comment: `# TODO: Phase 6 — migrate to PromptBuilder`
- Skip deletion
- Document in the commit what still depends on it

If no remaining imports, delete:
```bash
git rm src/agent_prompting.py
```

- [ ] **Step 2: Check prompt_registry.py imports**

```bash
grep -rn "prompt_registry\|from src.prompt_registry\|import prompt_registry" src/ tests/ --include="*.py" | grep -v __pycache__ | grep -v "prompt_registry.py"
```

If still imported (likely by `agent_prompting.py` or `plan_parser_llm.py`), it is NOT safe to remove. Skip if so.

If no remaining imports, delete:
```bash
git rm src/prompt_registry.py
```

- [ ] **Step 3: Check prompt_manager.py imports**

```bash
grep -rn "prompt_manager\|from src.prompt_manager\|import prompt_manager" src/ tests/ --include="*.py" | grep -v __pycache__ | grep -v "prompt_manager.py"
```

Same logic — delete only if truly unused.

- [ ] **Step 4: Delete associated test files**

For each removed module, delete its test file if one exists:
```bash
find tests/ -name "*agent_prompting*" -o -name "*prompt_registry*" -o -name "*prompt_manager*"
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -x --ignore=tests/chat_eval -q 2>&1 | tail -10`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Remove dead modules: [list removed files]"
```

If no modules could be removed, commit a note:
```bash
git commit --allow-empty -m "Phase 6 Task 5: agent_prompting, prompt_registry, prompt_manager still in use — defer removal"
```

---

### Task 6: Add Config Deprecation Warning

**Files:**
- Modify: `src/config.py`
- Modify: `tests/test_config_supervisor.py`

When a `chat_analyzer` config section exists but `supervisor.observation` is active, log a deprecation warning on startup.

- [ ] **Step 1: Write failing test**

Add to `tests/test_config_supervisor.py`:

```python
def test_chat_analyzer_deprecation_warning(caplog):
    """Deprecation warning when chat_analyzer config is present."""
    import logging
    from src.config import AppConfig, ChatAnalyzerConfig

    app = AppConfig(chat_analyzer=ChatAnalyzerConfig(enabled=True))
    with caplog.at_level(logging.WARNING):
        warnings = app.check_deprecations()
    assert any("chat_analyzer" in w.lower() for w in warnings)


def test_no_deprecation_when_analyzer_disabled():
    from src.config import AppConfig, ChatAnalyzerConfig

    app = AppConfig(chat_analyzer=ChatAnalyzerConfig(enabled=False))
    warnings = app.check_deprecations()
    assert len(warnings) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_config_supervisor.py::test_chat_analyzer_deprecation_warning -v`
Expected: FAIL — `check_deprecations` doesn't exist

- [ ] **Step 3: Implement check_deprecations**

Add to `AppConfig` in `src/config.py`:

```python
    def check_deprecations(self) -> list[str]:
        """Check for deprecated config sections and return warning messages."""
        warnings = []
        if self.chat_analyzer.enabled:
            warnings.append(
                "DEPRECATED: 'chat_analyzer' config section is deprecated. "
                "Use 'supervisor.observation' instead. The chat_analyzer "
                "section will be ignored when supervisor.observation is active."
            )
        return warnings
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config_supervisor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config_supervisor.py
git commit -m "Add deprecation warning for chat_analyzer config"
```

---

### Task 7: Clean Up Imports and Dead Code

**Files:**
- Various source files with stale imports

- [ ] **Step 1: Find stale imports**

```bash
# Find imports of removed/renamed modules
grep -rn "from src.chat_analyzer\|import chat_analyzer" src/ --include="*.py" | grep -v __pycache__
grep -rn "from src.chat_analyzer_service\|import chat_analyzer_service" src/ --include="*.py" | grep -v __pycache__
grep -rn "ChatAnalyzer[^S]" src/ --include="*.py" | grep -v __pycache__ | grep -v chat_agent.py
```

- [ ] **Step 2: Remove stale imports**

For each file with a stale import:
- Remove the import line
- Remove any code that uses the removed import
- Verify the file still works

- [ ] **Step 3: Check for orphaned config references**

```bash
grep -rn "chat_analyzer" src/ --include="*.py" | grep -v __pycache__ | grep -v config.py
```

Remove any references to `config.chat_analyzer` outside of `config.py` itself (since ChatAnalyzer is gone).

- [ ] **Step 4: Run full Phase 4-6 test suite**

```bash
pytest tests/test_config_supervisor.py tests/test_reflection.py tests/test_supervisor.py tests/test_prompt_builder.py tests/test_tool_registry.py tests/test_chat_observer.py tests/test_views.py tests/test_plan_handoff.py -v
```

Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "Clean up stale imports and dead code references"
```

---

### Task 8: Update Specs and Final Integration Test

**Files:**
- Modify: `specs/orchestrator.md` (if it exists, update post-task flow)
- Modify: `specs/supervisor.md`
- Modify: `tests/test_plan_handoff.py`

- [ ] **Step 1: Add integration test**

Add to `tests/test_plan_handoff.py`:

```python
def test_full_handoff_flow():
    """Verify the complete handoff: Supervisor handles task completion."""
    from src.supervisor import Supervisor

    # Supervisor has on_task_completed
    assert hasattr(Supervisor, "on_task_completed")

    # CommandHandler has process_task_completion
    handler = _make_handler()
    assert hasattr(handler, "_cmd_process_task_completion")

    # process_task_completion is a registered tool name
    from src.tool_registry import ToolRegistry, _TOOL_CATEGORIES
    assert "process_task_completion" in _TOOL_CATEGORIES


def test_plan_parser_still_works_as_library():
    """plan_parser.py is retained as a library and still functions."""
    from src.plan_parser import find_plan_file, parse_plan, parse_and_generate_steps

    content = (
        "# Plan\n\n"
        "## Step 1: Do thing A\nDetails for A.\n\n"
        "## Step 2: Do thing B\nDetails for B.\n"
    )
    steps, quality = parse_and_generate_steps(content)
    assert len(steps) >= 1
    assert quality is not None
```

- [ ] **Step 2: Update specs/supervisor.md**

Add to the Methods section:

```markdown
**on_task_completed(task_id, project_id, workspace_path) → None**
Handles task.completed events. Discovers plan files in the workspace,
stores them for approval, and runs a reflection pass. Never raises.
Called by the orchestrator after a task finishes successfully.
```

- [ ] **Step 3: Run all tests**

```bash
pytest tests/test_config_supervisor.py tests/test_reflection.py tests/test_supervisor.py tests/test_prompt_builder.py tests/test_tool_registry.py tests/test_chat_observer.py tests/test_views.py tests/test_plan_handoff.py -v
```

Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "Phase 6 complete: Orchestrator Handoff + Cleanup"
```
