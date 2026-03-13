"""Tests for the hook engine."""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, HookEngineConfig
from src.database import Database
from src.event_bus import EventBus
from src.hooks import HookEngine
from src.models import Hook, HookRun


@pytest.fixture
async def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def config():
    cfg = AppConfig()
    cfg.hook_engine = HookEngineConfig(enabled=True, max_concurrent_hooks=2)
    return cfg


@pytest.fixture
async def engine(db, bus, config):
    engine = HookEngine(db, bus, config)
    engine._orchestrator = MagicMock()
    engine._orchestrator._notify_channel = AsyncMock()
    engine._orchestrator.db = db
    engine._orchestrator.hooks = engine
    await engine.initialize()
    yield engine
    await engine.shutdown()


async def _create_project(db, project_id="test-project"):
    from src.models import Project
    project = Project(id=project_id, name="Test Project")
    await db.create_project(project)
    return project


async def _create_hook(db, **overrides) -> Hook:
    defaults = dict(
        id="test-hook",
        project_id="test-project",
        name="test-hook",
        enabled=True,
        trigger='{"type": "periodic", "interval_seconds": 3600}',
        context_steps='[]',
        prompt_template="Test prompt: {{step_0}}",
        cooldown_seconds=60,
    )
    defaults.update(overrides)
    hook = Hook(**defaults)
    await db.create_hook(hook)
    return hook


# --- Prompt rendering ---


class TestPromptRendering:
    def test_render_step_output(self, engine):
        result = engine._render_prompt(
            "Results: {{step_0}}",
            [{"stdout": "all tests pass", "exit_code": 0}],
        )
        assert result == "Results: all tests pass"

    def test_render_step_field(self, engine):
        result = engine._render_prompt(
            "Exit: {{step_0.exit_code}}",
            [{"stdout": "output", "exit_code": 1}],
        )
        assert result == "Exit: 1"

    def test_render_multiple_steps(self, engine):
        result = engine._render_prompt(
            "Step 0: {{step_0}}\nStep 1: {{step_1}}",
            [
                {"stdout": "first"},
                {"content": "second"},
            ],
        )
        assert "first" in result
        assert "second" in result

    def test_render_event_data(self, engine):
        result = engine._render_prompt(
            "Task: {{event.task_id}}",
            [],
            event_data={"task_id": "abc123"},
        )
        assert result == "Task: abc123"

    def test_render_event_full(self, engine):
        result = engine._render_prompt(
            "Event: {{event}}",
            [],
            event_data={"task_id": "abc", "status": "done"},
        )
        parsed = json.loads(result.replace("Event: ", ""))
        assert parsed["task_id"] == "abc"

    def test_render_missing_step(self, engine):
        result = engine._render_prompt("{{step_5}}", [])
        assert result == ""

    def test_render_no_event(self, engine):
        result = engine._render_prompt("{{event.task_id}}", [], event_data=None)
        assert result == ""


# --- Short-circuit logic ---


class TestShortCircuit:
    def test_skip_on_exit_zero(self, engine):
        steps = [{"type": "shell", "command": "echo hi", "skip_llm_if_exit_zero": True}]
        results = [{"stdout": "hi", "exit_code": 0}]
        reason = engine._should_skip_llm(steps, results)
        assert reason is not None
        assert "exit code 0" in reason

    def test_no_skip_on_exit_nonzero(self, engine):
        steps = [{"type": "shell", "command": "false", "skip_llm_if_exit_zero": True}]
        results = [{"stdout": "", "exit_code": 1}]
        reason = engine._should_skip_llm(steps, results)
        assert reason is None

    def test_skip_on_empty_output(self, engine):
        steps = [{"type": "shell", "command": "echo", "skip_llm_if_empty": True}]
        results = [{"stdout": "", "content": ""}]
        reason = engine._should_skip_llm(steps, results)
        assert reason is not None
        assert "empty" in reason

    def test_no_skip_on_nonempty(self, engine):
        steps = [{"type": "shell", "command": "echo x", "skip_llm_if_empty": True}]
        results = [{"stdout": "some output"}]
        reason = engine._should_skip_llm(steps, results)
        assert reason is None

    def test_skip_on_http_ok(self, engine):
        steps = [{"type": "http", "url": "http://example.com", "skip_llm_if_status_ok": True}]
        results = [{"body": "ok", "status_code": 200}]
        reason = engine._should_skip_llm(steps, results)
        assert reason is not None
        assert "HTTP 200" in reason

    def test_no_skip_on_http_error(self, engine):
        steps = [{"type": "http", "url": "http://example.com", "skip_llm_if_status_ok": True}]
        results = [{"body": "error", "status_code": 500}]
        reason = engine._should_skip_llm(steps, results)
        assert reason is None

    def test_no_skip_conditions(self, engine):
        steps = [{"type": "shell", "command": "echo hi"}]
        results = [{"stdout": "hi", "exit_code": 0}]
        reason = engine._should_skip_llm(steps, results)
        assert reason is None


# --- Context steps ---


class TestContextSteps:
    @pytest.mark.asyncio
    async def test_shell_step(self, engine):
        step = {"type": "shell", "command": "echo hello", "timeout": 10}
        results = await engine._run_context_steps([step])
        assert len(results) == 1
        assert "hello" in results[0]["stdout"]
        assert results[0]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_shell_step_failure(self, engine):
        step = {"type": "shell", "command": "exit 42", "timeout": 10}
        results = await engine._run_context_steps([step])
        assert results[0]["exit_code"] == 42

    @pytest.mark.asyncio
    async def test_shell_step_timeout(self, engine):
        step = {"type": "shell", "command": "sleep 60", "timeout": 1}
        results = await engine._run_context_steps([step])
        assert results[0]["exit_code"] == -1
        assert "timed out" in results[0]["stderr"]

    @pytest.mark.asyncio
    async def test_read_file_step(self, engine, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3")
        step = {"type": "read_file", "path": str(test_file)}
        results = await engine._run_context_steps([step])
        assert "line1" in results[0]["content"]
        assert "line3" in results[0]["content"]

    @pytest.mark.asyncio
    async def test_read_file_missing(self, engine):
        step = {"type": "read_file", "path": "/nonexistent/file.txt"}
        results = await engine._run_context_steps([step])
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_unknown_step_type(self, engine):
        step = {"type": "foobar"}
        results = await engine._run_context_steps([step])
        assert "error" in results[0]
        assert "Unknown" in results[0]["error"]


# --- Cooldown ---


class TestCooldown:
    @pytest.mark.asyncio
    async def test_cooldown_blocks_refire(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(db, cooldown_seconds=3600)
        now = time.time()

        # First check: should pass (no previous run)
        assert engine._check_cooldown(hook, now) is True

        # Record a recent run
        engine._last_run_time[hook.id] = now

        # Second check: should fail (within cooldown)
        assert engine._check_cooldown(hook, now + 10) is False

        # After cooldown expires
        assert engine._check_cooldown(hook, now + 3601) is True


# --- Concurrent dedup ---


class TestConcurrentDedup:
    @pytest.mark.asyncio
    async def test_same_hook_not_run_twice(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(db)

        # Simulate an in-flight hook
        engine._running[hook.id] = asyncio.create_task(asyncio.sleep(100))

        # tick should not launch another
        engine._last_run_time[hook.id] = 0  # Force it to be "due"
        await engine.tick()

        # Should still only have 1 task
        assert len(engine._running) == 1

        # Cleanup
        engine._running[hook.id].cancel()
        try:
            await engine._running[hook.id]
        except asyncio.CancelledError:
            pass


# --- Periodic scheduling ---


class TestPeriodicScheduling:
    @pytest.mark.asyncio
    async def test_periodic_hook_fires_when_due(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 10}',
            cooldown_seconds=5,
            context_steps='[{"type": "shell", "command": "echo test", "skip_llm_if_exit_zero": true}]',
        )

        # No previous run — should fire
        engine._last_run_time.pop(hook.id, None)
        await engine.tick()
        assert hook.id in engine._running

    @pytest.mark.asyncio
    async def test_periodic_hook_not_due(self, db, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "periodic", "interval_seconds": 3600}',
            cooldown_seconds=3600,
        )

        # Recent run
        engine._last_run_time[hook.id] = time.time()
        await engine.tick()
        assert hook.id not in engine._running


# --- Event-driven firing ---


class TestEventDriven:
    @pytest.mark.asyncio
    async def test_event_hook_fires(self, db, bus, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="event-hook",
            name="event-hook",
            trigger='{"type": "event", "event_type": "task_completed"}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "echo done", "skip_llm_if_exit_zero": true}]',
        )

        # Emit an event
        await bus.emit("task_completed", {"task_id": "t1"})

        # Give async task a moment
        await asyncio.sleep(0.1)

        # Hook should have been launched
        assert hook.id in engine._running or len(await db.list_hook_runs(hook.id)) > 0

    @pytest.mark.asyncio
    async def test_event_hook_ignores_wrong_type(self, db, bus, engine):
        await _create_project(db)
        hook = await _create_hook(
            db,
            id="event-hook2",
            name="event-hook2",
            trigger='{"type": "event", "event_type": "task_completed"}',
            cooldown_seconds=0,
        )

        await bus.emit("task_failed", {"task_id": "t1"})
        await asyncio.sleep(0.1)

        assert hook.id not in engine._running


# --- Full pipeline with mock LLM ---


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_execute_hook_skipped(self, db, engine):
        """Hook with skip_llm_if_exit_zero should skip LLM when command succeeds."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            context_steps='[{"type": "shell", "command": "echo pass", "skip_llm_if_exit_zero": true}]',
        )

        await engine._execute_hook(hook, "manual")

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert runs[0].status == "skipped"
        assert runs[0].skipped_reason is not None

    @pytest.mark.asyncio
    async def test_execute_hook_with_llm(self, db, engine):
        """Hook that doesn't skip should invoke LLM."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            context_steps='[{"type": "shell", "command": "exit 1"}]',
            prompt_template="Fix this: {{step_0}}",
        )

        # Mock the LLM invocation
        with patch.object(engine, '_invoke_llm', new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = ("I created a task to fix it.", 150)
            await engine._execute_hook(hook, "manual")

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert runs[0].status == "completed"
        assert runs[0].tokens_used == 150
        assert runs[0].prompt_sent is not None
        assert "Fix this:" in runs[0].prompt_sent

    @pytest.mark.asyncio
    async def test_execute_hook_failure(self, db, engine):
        """Hook execution failure should be recorded."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            context_steps='[{"type": "shell", "command": "exit 1"}]',
        )

        with patch.object(engine, '_invoke_llm', new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = RuntimeError("LLM provider down")
            await engine._execute_hook(hook, "manual")

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert runs[0].status == "failed"


# --- DB CRUD ---


class TestHookCRUD:
    @pytest.mark.asyncio
    async def test_create_and_get_hook(self, db):
        await _create_project(db)
        hook = await _create_hook(db)
        fetched = await db.get_hook(hook.id)
        assert fetched is not None
        assert fetched.name == "test-hook"
        assert fetched.enabled is True

    @pytest.mark.asyncio
    async def test_list_hooks(self, db):
        await _create_project(db)
        await _create_hook(db, id="h1", name="h1")
        await _create_hook(db, id="h2", name="h2", enabled=False)

        all_hooks = await db.list_hooks()
        assert len(all_hooks) == 2

        enabled = await db.list_hooks(enabled=True)
        assert len(enabled) == 1
        assert enabled[0].id == "h1"

    @pytest.mark.asyncio
    async def test_update_hook(self, db):
        await _create_project(db)
        await _create_hook(db)
        await db.update_hook("test-hook", enabled=False, cooldown_seconds=120)
        hook = await db.get_hook("test-hook")
        assert hook.enabled is False
        assert hook.cooldown_seconds == 120

    @pytest.mark.asyncio
    async def test_delete_hook(self, db):
        await _create_project(db)
        hook = await _create_hook(db)

        # Add a run
        run = HookRun(
            id="run1", hook_id=hook.id, project_id="test-project",
            trigger_reason="manual", started_at=time.time(),
        )
        await db.create_hook_run(run)

        await db.delete_hook(hook.id)
        assert await db.get_hook(hook.id) is None
        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 0

    @pytest.mark.asyncio
    async def test_hook_run_crud(self, db):
        await _create_project(db)
        hook = await _create_hook(db)

        run = HookRun(
            id="run1", hook_id=hook.id, project_id="test-project",
            trigger_reason="periodic", started_at=time.time(),
        )
        await db.create_hook_run(run)

        await db.update_hook_run("run1", status="completed", tokens_used=100)

        last = await db.get_last_hook_run(hook.id)
        assert last is not None
        assert last.status == "completed"
        assert last.tokens_used == 100

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1


# --- Max concurrent ---


class TestMaxConcurrent:
    @pytest.mark.asyncio
    async def test_max_concurrent_respected(self, db, engine):
        await _create_project(db)
        engine.config.hook_engine.max_concurrent_hooks = 1

        await _create_hook(
            db, id="h1", name="h1",
            trigger='{"type": "periodic", "interval_seconds": 1}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "sleep 10"}]',
        )
        await _create_hook(
            db, id="h2", name="h2",
            trigger='{"type": "periodic", "interval_seconds": 1}',
            cooldown_seconds=0,
            context_steps='[{"type": "shell", "command": "sleep 10"}]',
        )

        await engine.tick()

        # Only 1 should be running
        assert len(engine._running) == 1


# --- create_task step ---


class TestCreateTaskStep:
    @pytest.mark.asyncio
    async def test_create_task_hook_step(self, db, engine):
        """create_task step should create a task in the DB."""
        await _create_project(db)
        step = {
            "type": "create_task",
            "title_template": "Fix merge conflict",
            "description_template": "Resolve conflicts on branch feature-1",
            "project_id": "test-project",
            "priority": 25,
        }
        results = await engine._run_context_steps([step])
        assert len(results) == 1
        assert results[0].get("created") is True
        task_id = results[0]["task_id"]

        # Verify task exists in DB
        from src.models import TaskStatus
        task = await db.get_task(task_id)
        assert task is not None
        assert task.title == "Fix merge conflict"
        assert task.project_id == "test-project"
        assert task.priority == 25
        assert task.status == TaskStatus.DEFINED

    @pytest.mark.asyncio
    async def test_create_task_placeholder_resolution(self, db, engine):
        """create_task step should resolve {{event.field}} placeholders."""
        await _create_project(db)
        step = {
            "type": "create_task",
            "title_template": "Fix: {{event.branch_name}}",
            "description_template": "Resolve for task {{event.task_id}}",
            "project_id": "test-project",
            "parent_task_id": "{{event.task_id}}",
            "context_entries": [
                {"type": "system", "label": "merge_resolution_for", "content": "{{event.task_id}}"}
            ],
        }
        # Create a parent task first (needed for FK)
        from src.models import Task, TaskStatus
        parent = Task(id="t-parent", project_id="test-project", title="Parent",
                       description="desc", status=TaskStatus.VERIFYING)
        await db.create_task(parent)

        event_data = {"task_id": "t-parent", "branch_name": "feature-x"}
        results = await engine._run_context_steps([step], event_data)
        assert results[0].get("created") is True
        task_id = results[0]["task_id"]

        task = await db.get_task(task_id)
        assert "feature-x" in task.title
        assert task.parent_task_id == "t-parent"

        # Check context entries
        contexts = await db.get_task_contexts(task_id)
        assert len(contexts) == 1
        assert contexts[0]["label"] == "merge_resolution_for"
        assert contexts[0]["content"] == "t-parent"


# --- skip_llm flag ---


class TestSkipLlmFlag:
    @pytest.mark.asyncio
    async def test_skip_llm_flag(self, db, engine):
        """Hook with skip_llm=true should complete without calling LLM."""
        await _create_project(db)
        hook = await _create_hook(
            db,
            trigger='{"type": "event", "event_type": "task.merge_failed", "skip_llm": true}',
            context_steps='[{"type": "shell", "command": "echo resolved"}]',
            prompt_template="",
        )

        with patch.object(engine, '_invoke_llm', new_callable=AsyncMock) as mock_llm:
            await engine._execute_hook(hook, "manual")
            mock_llm.assert_not_called()

        runs = await db.list_hook_runs(hook.id)
        assert len(runs) == 1
        assert runs[0].status == "completed"
