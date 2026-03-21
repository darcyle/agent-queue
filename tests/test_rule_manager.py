"""Tests for RuleManager -- rule file I/O and CRUD."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml


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
