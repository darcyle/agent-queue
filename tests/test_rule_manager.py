"""Tests for RuleManager -- rule file I/O and CRUD."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def storage_root(tmp_path):
    """Temporary storage root mimicking ~/.agent-queue."""
    return str(tmp_path)


@pytest.fixture
def rule_manager(storage_root):
    from src.rule_manager import RuleManager
    return RuleManager(storage_root=storage_root)


def test_save_rule_creates_file(rule_manager, storage_root):
    """save_rule writes a markdown file with YAML frontmatter."""
    result = rule_manager.save_rule(
        id="rule-test",
        project_id="my-project",
        rule_type="passive",
        content="# Test Rule\n\n## Intent\nDo the thing.",
    )
    assert result["success"] is True
    assert result["id"] == "rule-test"

    rule_path = os.path.join(
        storage_root, "memory", "my-project", "rules", "rule-test.md"
    )
    assert os.path.isfile(rule_path)

    with open(rule_path) as f:
        raw = f.read()
    assert "id: rule-test" in raw
    assert "type: passive" in raw
    assert "# Test Rule" in raw


def test_save_rule_global_scope(rule_manager, storage_root):
    """project_id=None saves to global rules directory."""
    result = rule_manager.save_rule(
        id="rule-global",
        project_id=None,
        rule_type="passive",
        content="# Global Rule\n\n## Intent\nApply everywhere.",
    )
    assert result["success"] is True

    rule_path = os.path.join(
        storage_root, "memory", "global", "rules", "rule-global.md"
    )
    assert os.path.isfile(rule_path)


def test_save_rule_auto_generates_id(rule_manager):
    """When id is omitted, save_rule generates one from the title."""
    result = rule_manager.save_rule(
        id=None,
        project_id="my-project",
        rule_type="passive",
        content="# Keep Tunnel Open\n\n## Intent\nMonitor tunnel.",
    )
    assert result["success"] is True
    assert result["id"].startswith("rule-")
    assert "tunnel" in result["id"].lower() or len(result["id"]) > 5


def test_save_rule_updates_existing(rule_manager, storage_root):
    """Saving with same id overwrites the file and updates timestamp."""
    rule_manager.save_rule(
        id="rule-update",
        project_id="proj",
        rule_type="passive",
        content="# V1\n\nOriginal.",
    )
    time.sleep(0.05)  # ensure timestamp differs
    result = rule_manager.save_rule(
        id="rule-update",
        project_id="proj",
        rule_type="passive",
        content="# V2\n\nUpdated.",
    )
    assert result["success"] is True

    loaded = rule_manager.load_rule("rule-update")
    assert "V2" in loaded["content"]


def test_load_rule(rule_manager):
    """load_rule returns full rule data with frontmatter fields."""
    rule_manager.save_rule(
        id="rule-load",
        project_id="proj",
        rule_type="active",
        content="# Active Rule\n\n## Trigger\nEvery 5 minutes.",
    )

    loaded = rule_manager.load_rule("rule-load")
    assert loaded is not None
    assert loaded["id"] == "rule-load"
    assert loaded["type"] == "active"
    assert loaded["project_id"] == "proj"
    assert "Active Rule" in loaded["content"]
    assert "created" in loaded
    assert "updated" in loaded
    assert isinstance(loaded["hooks"], list)


def test_load_rule_not_found(rule_manager):
    """load_rule returns None for nonexistent rule."""
    assert rule_manager.load_rule("nonexistent") is None


def test_delete_rule(rule_manager, storage_root):
    """delete_rule removes the file from disk."""
    rule_manager.save_rule(
        id="rule-delete",
        project_id="proj",
        rule_type="passive",
        content="# Delete Me",
    )
    result = rule_manager.delete_rule("rule-delete")
    assert result["success"] is True
    assert rule_manager.load_rule("rule-delete") is None


def test_delete_rule_not_found(rule_manager):
    """delete_rule returns error for nonexistent rule."""
    result = rule_manager.delete_rule("nonexistent")
    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_browse_rules_project_and_global(rule_manager):
    """browse_rules returns both project-scoped and global rules."""
    rule_manager.save_rule("rule-proj", "proj", "passive", "# Proj Rule")
    rule_manager.save_rule("rule-glob", None, "passive", "# Global Rule")

    rules = rule_manager.browse_rules("proj")
    ids = [r["id"] for r in rules]
    assert "rule-proj" in ids
    assert "rule-glob" in ids


def test_browse_rules_summary_truncation(rule_manager):
    """browse_rules returns first 200 chars as summary."""
    long_intent = "A" * 500
    rule_manager.save_rule(
        "rule-long", "proj", "passive",
        f"# Long Rule\n\n## Intent\n{long_intent}",
    )
    rules = rule_manager.browse_rules("proj")
    long_rule = [r for r in rules if r["id"] == "rule-long"][0]
    assert len(long_rule["summary"]) <= 200


def test_get_rules_for_prompt(rule_manager):
    """get_rules_for_prompt returns formatted markdown for PromptBuilder."""
    rule_manager.save_rule("rule-style", "proj", "passive", "# Code Style\n\n## Intent\nUse black.")
    rule_manager.save_rule("rule-glob", None, "active", "# Monitor\n\n## Trigger\nEvery 5 min.")

    text = rule_manager.get_rules_for_prompt("proj")
    assert "Applicable Rules" in text
    assert "Code Style" in text
    assert "Monitor" in text
    assert "[Passive]" in text
    assert "[Active]" in text


def test_get_rules_for_prompt_empty(rule_manager):
    """get_rules_for_prompt returns empty string when no rules exist."""
    text = rule_manager.get_rules_for_prompt("proj")
    assert text == ""


# ------------------------------------------------------------------
# Async hook generation and deletion (Task 3)
# ------------------------------------------------------------------

@pytest.fixture
def mock_db():
    """Mock database that tracks hook create/delete calls."""
    db = AsyncMock()
    db.create_hook = AsyncMock()
    db.delete_hook = AsyncMock()
    db.get_hook = AsyncMock(return_value=None)
    db.list_hooks_by_id_prefix = AsyncMock(return_value=[])
    return db


@pytest.fixture
def rule_manager_with_db(storage_root, mock_db):
    from src.rule_manager import RuleManager
    return RuleManager(storage_root=storage_root, db=mock_db)


def test_async_delete_rule_removes_hooks(rule_manager_with_db, mock_db):
    """async_delete_rule removes associated hooks from the database."""
    rm = rule_manager_with_db

    # Save rule and manually set hooks in frontmatter
    rm.save_rule(
        id="rule-with-hooks",
        project_id="proj",
        rule_type="active",
        content="# Active Rule\n\n## Trigger\nEvery 5 min.",
    )
    rm._update_rule_hooks("rule-with-hooks", ["hook-from-rule"])

    result = asyncio.run(
        rm.async_delete_rule("rule-with-hooks")
    )
    assert result["success"] is True
    assert "hook-from-rule" in result["hooks_removed"]

    # Verify delete_hook was called on DB
    mock_db.delete_hook.assert_any_call("hook-from-rule")


def test_async_save_active_rule_generates_hooks(
    rule_manager_with_db, mock_db
):
    """async_save_rule generates hooks for active rules with triggers."""
    rm = rule_manager_with_db

    result = asyncio.run(
        rm.async_save_rule(
            id="rule-periodic",
            project_id="proj",
            rule_type="active",
            content=(
                "# Check Status\n\n## Trigger\nEvery 10 minutes."
                "\n\n## Logic\nRun check."
            ),
        )
    )
    assert result["success"] is True
    assert len(result["hooks_generated"]) == 1

    # Verify hook was created in DB
    mock_db.create_hook.assert_called_once()
    created_hook = mock_db.create_hook.call_args[0][0]
    assert created_hook.project_id == "proj"
    assert "rule-periodic" in created_hook.prompt_template


def test_reconcile_regenerates_hooks(rule_manager_with_db, mock_db):
    """reconcile regenerates hooks for all active rules."""
    rm = rule_manager_with_db

    rm.save_rule(
        id="rule-active",
        project_id="proj",
        rule_type="active",
        content=(
            "# Active Rule\n\n## Trigger\nEvery 10 min."
            "\n\n## Logic\nCheck status."
        ),
    )

    result = asyncio.run(rm.reconcile())
    assert result["rules_scanned"] > 0
    assert result["hooks_regenerated"] > 0
    mock_db.create_hook.assert_called()


def test_reconcile_preserves_last_triggered_at(storage_root, mock_db):
    """Reconciliation preserves last_triggered_at from old hooks.

    When hooks are deleted and recreated during reconciliation, the new hooks
    should inherit the last_triggered_at timestamp from the old hooks so
    periodic hooks don't re-fire immediately after a restart.
    """
    from src.models import Hook
    from src.rule_manager import RuleManager

    # Simulate an old hook with a recent last_triggered_at
    old_timestamp = time.time() - 300  # 5 minutes ago
    old_hook = Hook(
        id="rule-my-rule-abc123",
        project_id="proj",
        name="Rule: My Rule",
        trigger='{"type": "periodic", "interval_seconds": 3600}',
        last_triggered_at=old_timestamp,
    )
    mock_db.list_hooks_by_id_prefix = AsyncMock(return_value=[old_hook])
    mock_db.delete_hooks_by_id_prefix = AsyncMock(return_value=1)

    rm = RuleManager(storage_root=storage_root, db=mock_db)

    rm.save_rule(
        id="my-rule",
        project_id="proj",
        rule_type="active",
        content=(
            "# My Rule\n\n## Trigger\nEvery 60 minutes."
            "\n\n## Logic\nDo something."
        ),
    )

    result = asyncio.run(rm.reconcile())
    assert result["hooks_regenerated"] > 0

    # The newly created hook should have the old timestamp
    created_hook = mock_db.create_hook.call_args[0][0]
    assert created_hook.last_triggered_at == old_timestamp


def test_hook_generation_uses_llm_expansion(storage_root, mock_db):
    """Hook generation calls supervisor.expand_rule_prompt when available."""
    from src.rule_manager import RuleManager

    mock_supervisor = MagicMock()
    mock_supervisor.expand_rule_prompt = AsyncMock(
        return_value="Check tunnel with: pgrep -f cloudflared"
    )
    mock_orch = MagicMock()
    mock_orch._supervisor = mock_supervisor

    rm = RuleManager(
        storage_root=storage_root, db=mock_db, orchestrator=mock_orch,
    )
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-tunnel",
            project_id="proj",
            rule_type="active",
            content=(
                "# Keep Tunnel Open\n\n## Trigger\nEvery 5 minutes."
                "\n\n## Logic\nCheck if tunnel is running."
            ),
        )
    )
    assert result["success"] is True
    mock_supervisor.expand_rule_prompt.assert_called_once()
    created_hook = mock_db.create_hook.call_args[0][0]
    assert "pgrep" in created_hook.prompt_template
    assert "rule-tunnel" not in created_hook.prompt_template


def test_global_rule_generates_hooks_for_all_projects(storage_root, mock_db):
    """Global rules (project_id=None) create one hook per project."""
    from src.rule_manager import RuleManager
    from src.models import Project

    mock_db.list_projects = AsyncMock(return_value=[
        Project(id="proj-a", name="A"),
        Project(id="proj-b", name="B"),
    ])

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-global",
            project_id=None,
            rule_type="active",
            content=(
                "# Global Check\n\n## Trigger\nEvery 10 minutes."
                "\n\n## Logic\nDo stuff."
            ),
        )
    )
    assert result["success"] is True
    assert len(result["hooks_generated"]) == 2
    assert mock_db.create_hook.call_count == 2

    # Verify hooks target different projects
    pids = {
        mock_db.create_hook.call_args_list[i][0][0].project_id
        for i in range(2)
    }
    assert pids == {"proj-a", "proj-b"}


def test_global_rule_no_projects_creates_no_hooks(storage_root, mock_db):
    """Global rules with no projects in DB create no hooks."""
    from src.rule_manager import RuleManager

    mock_db.list_projects = AsyncMock(return_value=[])

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-empty",
            project_id=None,
            rule_type="active",
            content=(
                "# Empty\n\n## Trigger\nEvery 5 minutes."
                "\n\n## Logic\nCheck."
            ),
        )
    )
    assert result["success"] is True
    assert len(result["hooks_generated"]) == 0
    mock_db.create_hook.assert_not_called()


def test_hook_generation_falls_back_without_supervisor(
    rule_manager_with_db, mock_db,
):
    """Hook generation uses static template when supervisor is unavailable."""
    result = asyncio.run(
        rule_manager_with_db.async_save_rule(
            id="rule-no-sup",
            project_id="proj",
            rule_type="active",
            content=(
                "# Check Status\n\n## Trigger\nEvery 10 minutes."
                "\n\n## Logic\nRun check."
            ),
        )
    )
    assert result["success"] is True
    created_hook = mock_db.create_hook.call_args[0][0]
    assert "rule-no-sup" in created_hook.prompt_template
    assert "Follow the rule" in created_hook.prompt_template


def test_hook_generation_falls_back_on_llm_failure(storage_root, mock_db):
    """Hook generation falls back to static template when LLM fails."""
    from src.rule_manager import RuleManager

    mock_supervisor = MagicMock()
    mock_supervisor.expand_rule_prompt = AsyncMock(return_value=None)
    mock_orch = MagicMock()
    mock_orch._supervisor = mock_supervisor

    rm = RuleManager(
        storage_root=storage_root, db=mock_db, orchestrator=mock_orch,
    )
    result = asyncio.run(
        rm.async_save_rule(
            id="rule-fail",
            project_id="proj",
            rule_type="active",
            content=(
                "# Failing\n\n## Trigger\nEvery 5 minutes."
                "\n\n## Logic\nDo stuff."
            ),
        )
    )
    assert result["success"] is True
    created_hook = mock_db.create_hook.call_args[0][0]
    assert "rule-fail" in created_hook.prompt_template


def test_parse_trigger_periodic():
    """_parse_trigger extracts periodic triggers."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Trigger\nEvery 5 minutes.\n\n## Logic\nDo stuff."
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "periodic", "interval_seconds": 300}


def test_parse_trigger_event():
    """_parse_trigger extracts event-based triggers."""
    from src.rule_manager import RuleManager

    content = (
        "# Test\n\n## Trigger\nWhen a task is completed.\n\n## Logic\nReview."
    )
    result = RuleManager._parse_trigger(content)
    assert result == {"type": "event", "event_type": "task.completed"}


def test_parse_trigger_none():
    """_parse_trigger returns None for unparseable triggers."""
    from src.rule_manager import RuleManager

    content = "# Test\n\n## Intent\nNo trigger here."
    result = RuleManager._parse_trigger(content)
    assert result is None


# ------------------------------------------------------------------
# CommandHandler integration (Task 4)
# ------------------------------------------------------------------

def _make_command_handler(storage_root, mock_db):
    """Create a CommandHandler with a mocked orchestrator and RuleManager.

    Patches sys.modules for missing dependencies (discord, aiosqlite)
    so CommandHandler can be imported in test environments.
    """
    import sys

    # Ensure discord and aiosqlite stubs exist so command_handler can import
    mods_to_stub = {}
    for mod_name in [
        "discord", "discord.ext", "discord.ext.commands", "discord.ui",
        "aiosqlite",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()
            mods_to_stub[mod_name] = True

    from src.rule_manager import RuleManager
    from src.command_handler import CommandHandler

    rm = RuleManager(storage_root=storage_root, db=mock_db)
    orch = MagicMock()
    orch.db = mock_db
    orch.rule_manager = rm
    orch.config = MagicMock()
    return CommandHandler(orch, orch.config), rm


def test_cmd_save_rule(storage_root, mock_db):
    """CommandHandler.execute('save_rule') creates a rule file."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    result = asyncio.run(
        handler.execute("save_rule", {
            "project_id": "proj",
            "type": "passive",
            "content": "# Check Code Style\n\n## Intent\nEnforce linting.",
        })
    )
    assert result.get("success") is True or "id" in result


def test_cmd_browse_rules(storage_root, mock_db):
    """CommandHandler.execute('browse_rules') returns rule list."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    rm.save_rule("rule-1", "proj", "passive", "# Rule One")

    result = asyncio.run(
        handler.execute("browse_rules", {"project_id": "proj"})
    )
    assert "rules" in result
    assert any(r["id"] == "rule-1" for r in result["rules"])


def test_cmd_load_rule(storage_root, mock_db):
    """CommandHandler.execute('load_rule') returns full rule."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    rm.save_rule("rule-2", "proj", "passive", "# Rule Two\n\nContent here.")

    result = asyncio.run(
        handler.execute("load_rule", {"id": "rule-2"})
    )
    assert result.get("id") == "rule-2"
    assert "Rule Two" in result.get("content", "")


def test_cmd_delete_rule(storage_root, mock_db):
    """CommandHandler.execute('delete_rule') removes a rule."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    rm.save_rule("rule-3", "proj", "passive", "# Rule Three")

    result = asyncio.run(
        handler.execute("delete_rule", {"id": "rule-3"})
    )
    assert result.get("success") is True


def test_cmd_refresh_hooks(storage_root, mock_db):
    """CommandHandler.execute('refresh_hooks') reconciles hooks from rules."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    # Save a passive rule (no hooks to generate, but should be scanned)
    rm.save_rule("rule-scan", "proj", "passive", "# Passive Rule")

    result = asyncio.run(
        handler.execute("refresh_hooks", {})
    )
    assert result.get("success") is True
    assert result.get("rules_scanned", 0) >= 1
    assert "errors" in result


def test_cmd_refresh_hooks_no_rule_manager(storage_root, mock_db):
    """refresh_hooks returns error when rule manager is not initialized."""
    handler, rm = _make_command_handler(storage_root, mock_db)
    handler.orchestrator.rule_manager = None

    result = asyncio.run(
        handler.execute("refresh_hooks", {})
    )
    assert "error" in result


# ------------------------------------------------------------------
# Orchestrator integration (Task 7)
# ------------------------------------------------------------------

def test_orchestrator_initializes_rule_manager(tmp_path):
    """Orchestrator creates RuleManager during initialize()."""
    import sys

    # Ensure discord/aiosqlite stubs exist so orchestrator can import
    for mod_name in [
        "discord", "discord.ext", "discord.ext.commands", "discord.ui",
        "aiosqlite",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    from unittest.mock import patch, AsyncMock

    from src.orchestrator import Orchestrator

    config = MagicMock()
    config.data_dir = str(tmp_path)
    config.database_path = str(tmp_path / "test.db")
    config.global_token_budget_daily = 1000000
    config.hook_engine = MagicMock()
    config.hook_engine.enabled = False
    config.chat_analyzer = MagicMock()
    config.chat_analyzer.enabled = False
    config.memory = MagicMock()
    config.memory.enabled = False
    config._config_path = None
    config.llm_logging = MagicMock()
    config.llm_logging.enabled = False
    config.llm_logging.retention_days = 30
    config.agent_profiles = []

    orch = Orchestrator(config)

    # Mock DB and recovery so initialize() doesn't need real sqlite
    orch.db = MagicMock()
    orch.db.initialize = AsyncMock()
    orch.db.list_agents = AsyncMock(return_value=[])
    orch.db.list_workspaces = AsyncMock(return_value=[])
    orch.db.list_tasks = AsyncMock(return_value=[])

    with patch.object(orch, '_sync_profiles_from_config', new_callable=AsyncMock):
        asyncio.run(orch.initialize())

    assert hasattr(orch, 'rule_manager')
    assert orch.rule_manager is not None


# ------------------------------------------------------------------
# Default rule installation (Task 8)
# ------------------------------------------------------------------

def test_install_default_rules(storage_root):
    """install_defaults creates global rules from bundled templates."""
    from src.rule_manager import RuleManager
    rm = RuleManager(storage_root=storage_root)

    installed = rm.install_defaults()
    assert len(installed) >= 2

    # Verify rules are browseable
    rules = rm.browse_rules(None)
    ids = [r["id"] for r in rules]
    assert any("reflection" in rid for rid in ids)
    assert any("review" in rid for rid in ids)


def test_install_defaults_idempotent(storage_root):
    """install_defaults does not duplicate rules on re-run."""
    from src.rule_manager import RuleManager
    rm = RuleManager(storage_root=storage_root)

    rm.install_defaults()
    rm.install_defaults()

    rules = rm.browse_rules(None)
    # Count rules -- should not have duplicates
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids))
