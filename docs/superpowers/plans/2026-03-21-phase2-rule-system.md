# Phase 2: Rule System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a memory-native rule system that stores persistent autonomous behaviors as structured markdown files, provides CRUD tools for rule management, generates hooks from active rules, and reconciles rule-hook state on startup.

**Architecture:** `RuleManager` manages rule file I/O under `~/.agent-queue/memory/{project_id}/rules/` and `~/.agent-queue/memory/global/rules/`. Active rules generate hooks via a rule processor. `PromptBuilder.load_relevant_rules()` is wired to load rules from disk. All four rule tools (`save_rule`, `delete_rule`, `browse_rules`, `load_rule`) are registered in `CommandHandler` and exposed as chat agent tools.

**Tech Stack:** Python 3.12+, pytest with pytest-asyncio, dataclasses, YAML frontmatter parsing, filesystem-based storage

**Spec:** `docs/superpowers/specs/2026-03-21-supervisor-refactor-design.md` (Section 3)

**Dependency:** Phase 1 (PromptBuilder) — must be complete. Uses `PromptBuilder.load_relevant_rules()` stub.

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `specs/rule-system.md` | Behavioral spec for rule system |
| Create | `src/rule_manager.py` | Rule file I/O, CRUD, hook generation, reconciliation |
| Create | `tests/test_rule_manager.py` | All RuleManager unit tests |
| Create | `src/prompts/default_rules/` | Default global rule markdown files |
| Modify | `src/command_handler.py` | Add `_cmd_save_rule`, `_cmd_delete_rule`, `_cmd_browse_rules`, `_cmd_load_rule` |
| Modify | `src/chat_agent.py` | Add rule tool definitions to TOOLS list |
| Modify | `src/prompt_builder.py` | Wire `load_relevant_rules()` to RuleManager |
| Modify | `src/orchestrator.py` | Initialize RuleManager, call startup reconciliation |
| Modify | `src/memory.py` | Add `_rules_dir()` helper, expose global scope path |

---

### Task 1: Write the Rule System spec

**Files:**
- Create: `specs/rule-system.md`

- [ ] **Step 1: Write the spec**

Write the behavioral specification for the rule system. This spec describes *what* the system does, not *how*.

```markdown
# Rule System

## Purpose

Persistent autonomous behaviors for the Supervisor. Rules are natural language intentions stored as structured markdown files in the memory filesystem. Active rules generate hooks for automated execution. Passive rules influence reasoning via semantic search.

## Concepts

### Rule Types

**Active rules** have a trigger (periodic, event-driven) and logic. They generate one or more hooks that execute via the existing hook engine. The rule is the source of truth; hooks are derived, disposable artifacts.

**Passive rules** have no triggers. They influence the Supervisor's reasoning by surfacing through semantic search when relevant to the current action. Example: "When reviewing PRs, always check for SQL injection."

### Rule Storage

Rules are structured markdown files with YAML frontmatter:
- Project-scoped: `~/.agent-queue/memory/{project_id}/rules/`
- Global: `~/.agent-queue/memory/global/rules/`

### Rule Document Structure

```
---
id: rule-keep-tunnel-open
type: active
project_id: my-game-server
hooks: [hook-abc123]
created: 2026-03-20T10:00:00Z
updated: 2026-03-21T14:30:00Z
---

# Keep Cloudflare tunnel open

## Intent
...

## Trigger
Check every 5 minutes.

## Logic
1. Run `cloudflared tunnel status`
2. ...
```

### Hook Generation

When an active rule is saved or updated:
1. Parse trigger and logic from the rule content
2. Compare against existing hooks for this rule
3. If trigger/logic changed or no hooks exist: generate new hooks
4. Delete old hooks, create new ones
5. Update the rule's `hooks` frontmatter field

Hook generation may use an LLM call to translate natural language into context steps and prompt templates. This happens once at creation/edit, not on every execution.

Generated hooks reference the rule ID in their prompt template so the executing LLM knows which rule it is fulfilling.

### Reconciliation

On startup, the RuleManager:
1. Scans all rule files (project + global)
2. For each active rule, checks that its listed hooks exist in the DB
3. Regenerates missing hooks
4. Removes orphaned hooks (hooks referencing rules that no longer exist)

### Degradation Without Memsearch

- Rule files are always readable from disk (plain markdown)
- `browse_rules` and `load_rule` read directly from filesystem
- Without memsearch: `load_relevant_rules()` loads ALL rules for project + globals
- Hook reconciliation is filesystem-based, no search index needed

## Interfaces

### RuleManager

Constructor: `RuleManager(storage_root: str, db: Database, hook_engine: HookEngine | None)`

Methods:
- `save_rule(id, project_id, type, content) -> dict` — Write rule file, trigger hook generation for active rules
- `delete_rule(id) -> dict` — Delete rule file and associated hooks
- `browse_rules(project_id) -> list[dict]` — List rules for project + globals
- `load_rule(id) -> dict | None` — Load full rule content
- `reconcile() -> dict` — Startup reconciliation
- `get_rules_for_prompt(project_id, query) -> str` — Load rules for PromptBuilder Layer 3

### CommandHandler Commands

- `save_rule` — delegates to RuleManager.save_rule()
- `delete_rule` — delegates to RuleManager.delete_rule()
- `browse_rules` — delegates to RuleManager.browse_rules()
- `load_rule` — delegates to RuleManager.load_rule()

## Invariants

- Rule files are the source of truth; hooks are derived
- Deleting a rule always deletes its hooks
- Hook generation failures do not prevent rule save (rule saved, hooks marked as failed)
- Global rules are visible from all projects
- Rule IDs are unique across all scopes (project + global)
- Frontmatter timestamps are always UTC ISO 8601
```

- [ ] **Step 2: Commit the spec**

```bash
git add specs/rule-system.md
git commit -m "Add Rule System behavioral spec for Phase 2 of supervisor refactor"
```

---

### Task 2: Core RuleManager — rule file I/O

**Files:**
- Create: `tests/test_rule_manager.py`
- Create: `src/rule_manager.py`

- [ ] **Step 1: Write failing tests for rule file I/O**

```python
"""Tests for RuleManager — rule file I/O and CRUD."""

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_manager.py -v`
Expected: FAIL — `src.rule_manager` does not exist yet

- [ ] **Step 3: Implement RuleManager with file I/O**

```python
"""Rule system for persistent autonomous behaviors.

Rules are structured markdown files with YAML frontmatter stored in the
memory filesystem. Active rules generate hooks for automated execution.
Passive rules influence reasoning via semantic search.

Storage layout:
    ~/.agent-queue/memory/{project_id}/rules/{rule_id}.md
    ~/.agent-queue/memory/global/rules/{rule_id}.md
"""

from __future__ import annotations

import glob
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_GLOBAL_SCOPE = "global"


class RuleManager:
    """Manages rule file I/O, hook generation, and reconciliation.

    Rules are the source of truth. Hooks are derived artifacts that
    implement active rules via the existing hook engine.
    """

    def __init__(
        self,
        storage_root: str,
        db: Any | None = None,
        hook_engine: Any | None = None,
    ):
        self._storage_root = os.path.expanduser(storage_root)
        self._db = db
        self._hook_engine = hook_engine

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _rules_dir(self, project_id: str | None) -> str:
        scope = project_id or _GLOBAL_SCOPE
        return os.path.join(self._storage_root, "memory", scope, "rules")

    def _rule_path(self, rule_id: str, project_id: str | None) -> str:
        return os.path.join(self._rules_dir(project_id), f"{rule_id}.md")

    def _find_rule_path(self, rule_id: str) -> tuple[str, str | None] | None:
        """Find a rule file by ID across all scopes.

        Returns (file_path, project_id_or_None) or None.
        """
        memory_root = os.path.join(self._storage_root, "memory")
        if not os.path.isdir(memory_root):
            return None
        for scope_dir in os.listdir(memory_root):
            rules_dir = os.path.join(memory_root, scope_dir, "rules")
            candidate = os.path.join(rules_dir, f"{rule_id}.md")
            if os.path.isfile(candidate):
                pid = None if scope_dir == _GLOBAL_SCOPE else scope_dir
                return candidate, pid
        return None

    # ------------------------------------------------------------------
    # Frontmatter parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict, str]:
        if not content.startswith("---"):
            return {}, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return {}, content
        return meta, parts[2].strip()

    @staticmethod
    def _build_file_content(meta: dict, body: str) -> str:
        frontmatter = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\n{body}\n"

    @staticmethod
    def _extract_title(content: str) -> str:
        """Extract the first H1 heading from markdown content."""
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
        return ""

    @staticmethod
    def _id_from_title(title: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        return f"rule-{slug}" if slug else f"rule-{int(time.time())}"

    @staticmethod
    def _extract_summary(body: str, max_len: int = 200) -> str:
        """Extract first 200 chars of content after the title."""
        lines = body.strip().split("\n")
        text_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# "):
                continue  # skip title
            if stripped:
                text_lines.append(stripped)
        text = " ".join(text_lines)
        if len(text) > max_len:
            return text[:max_len]
        return text

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def save_rule(
        self,
        id: str | None,
        project_id: str | None,
        rule_type: str,
        content: str,
    ) -> dict:
        """Write a rule file with YAML frontmatter.

        If id matches an existing rule, it is updated. Otherwise a new
        rule is created. Auto-generates id from title if omitted.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Auto-generate ID if not provided
        if not id:
            title = self._extract_title(content)
            id = self._id_from_title(title) if title else f"rule-{int(time.time())}"

        # Check if updating an existing rule
        existing = self._find_rule_path(id)
        created = now
        hooks: list[str] = []
        if existing:
            path, old_pid = existing
            old_meta, _ = self._split_frontmatter(
                open(path).read()
            )
            created = old_meta.get("created", now)
            hooks = old_meta.get("hooks", [])
            # If project scope changed, delete old file
            if old_pid != project_id:
                os.remove(path)

        meta = {
            "id": id,
            "type": rule_type,
            "project_id": project_id,
            "hooks": hooks,
            "created": created,
            "updated": now,
        }

        file_content = self._build_file_content(meta, content)
        rule_path = self._rule_path(id, project_id)
        os.makedirs(os.path.dirname(rule_path), exist_ok=True)

        with open(rule_path, "w") as f:
            f.write(file_content)

        return {
            "success": True,
            "id": id,
            "hooks_generated": [],  # Hook generation handled in async method
        }

    def load_rule(self, rule_id: str) -> dict | None:
        """Load a rule by ID, searching all scopes."""
        found = self._find_rule_path(rule_id)
        if not found:
            return None
        path, project_id = found

        with open(path) as f:
            raw = f.read()

        meta, body = self._split_frontmatter(raw)
        return {
            "id": meta.get("id", rule_id),
            "type": meta.get("type", "passive"),
            "project_id": meta.get("project_id"),
            "hooks": meta.get("hooks", []),
            "content": body,
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
        }

    def delete_rule(self, rule_id: str) -> dict:
        """Delete a rule file. Hook cleanup handled by async wrapper."""
        found = self._find_rule_path(rule_id)
        if not found:
            return {"success": False, "error": f"Rule '{rule_id}' not found"}

        path, _ = found
        meta, _ = self._split_frontmatter(open(path).read())
        hook_ids = meta.get("hooks", [])
        os.remove(path)

        return {
            "success": True,
            "hooks_removed": hook_ids,
        }

    def browse_rules(self, project_id: str | None = None) -> list[dict]:
        """List rules for a project plus all global rules."""
        results = []
        dirs_to_scan = []

        # Project-specific rules
        if project_id:
            dirs_to_scan.append((self._rules_dir(project_id), project_id))

        # Global rules always included
        dirs_to_scan.append((self._rules_dir(None), None))

        seen_ids: set[str] = set()
        for rules_dir, scope_pid in dirs_to_scan:
            if not os.path.isdir(rules_dir):
                continue
            for filename in sorted(os.listdir(rules_dir)):
                if not filename.endswith(".md"):
                    continue
                filepath = os.path.join(rules_dir, filename)
                try:
                    with open(filepath) as f:
                        raw = f.read()
                    meta, body = self._split_frontmatter(raw)
                    rid = meta.get("id", filename[:-3])
                    if rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    title = self._extract_title(body)
                    results.append({
                        "id": rid,
                        "name": title or rid,
                        "type": meta.get("type", "passive"),
                        "project_id": meta.get("project_id"),
                        "summary": self._extract_summary(body),
                        "hook_count": len(meta.get("hooks", [])),
                        "updated": meta.get("updated", ""),
                    })
                except Exception as e:
                    logger.warning("Failed to read rule file %s: %s", filepath, e)

        return results

    def get_rules_for_prompt(
        self, project_id: str | None, query: str | None = None
    ) -> str:
        """Load rules as formatted text for PromptBuilder Layer 3.

        Without memsearch: returns ALL rules for the project + globals.
        With memsearch (future): would use semantic search against query.
        """
        rules = self.browse_rules(project_id)
        if not rules:
            return ""

        sections = []
        for rule_info in rules:
            loaded = self.load_rule(rule_info["id"])
            if loaded:
                rule_type = loaded["type"]
                prefix = "[Active]" if rule_type == "active" else "[Passive]"
                sections.append(
                    f"### {prefix} {rule_info['name']}\n{loaded['content']}"
                )

        if not sections:
            return ""

        return "## Applicable Rules\n\n" + "\n\n---\n\n".join(sections)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_manager.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/rule_manager.py tests/test_rule_manager.py
git commit -m "Add RuleManager core with rule file I/O and CRUD operations"
```

---

### Task 3: Async hook generation and deletion for active rules

**Files:**
- Modify: `tests/test_rule_manager.py`
- Modify: `src/rule_manager.py`

- [ ] **Step 1: Write failing tests for async hook operations**

Add to `tests/test_rule_manager.py`:

```python
@pytest.fixture
async def db(tmp_path):
    from src.database import Database
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
async def rule_manager_with_db(storage_root, db):
    from src.rule_manager import RuleManager
    return RuleManager(storage_root=storage_root, db=db)


async def test_async_delete_rule_removes_hooks(rule_manager_with_db, db):
    """async_delete_rule removes associated hooks from the database."""
    from src.models import Hook, Project
    await db.create_project(Project(id="proj", name="Test"))

    # Create a hook that an active rule references
    hook = Hook(id="hook-from-rule", project_id="proj", name="Rule Hook")
    await db.create_hook(hook)

    # Save rule referencing that hook
    rule_manager_with_db.save_rule(
        id="rule-with-hooks",
        project_id="proj",
        rule_type="active",
        content="# Active Rule\n\n## Trigger\nEvery 5 min.",
    )
    # Manually update frontmatter to reference the hook
    rule_data = rule_manager_with_db.load_rule("rule-with-hooks")
    rule_manager_with_db._update_rule_hooks("rule-with-hooks", ["hook-from-rule"])

    result = await rule_manager_with_db.async_delete_rule("rule-with-hooks")
    assert result["success"] is True
    assert "hook-from-rule" in result["hooks_removed"]

    # Verify hook is gone from DB
    hook = await db.get_hook("hook-from-rule")
    assert hook is None


async def test_reconcile_regenerates_missing_hooks(rule_manager_with_db, db):
    """reconcile detects active rules with missing hooks."""
    from src.models import Project
    await db.create_project(Project(id="proj", name="Test"))

    # Save active rule referencing a hook that doesn't exist in DB
    rule_manager_with_db.save_rule(
        id="rule-orphan",
        project_id="proj",
        rule_type="active",
        content="# Orphan Rule\n\n## Trigger\nEvery 10 min.\n\n## Logic\nCheck status.",
    )
    rule_manager_with_db._update_rule_hooks("rule-orphan", ["hook-missing"])

    result = await rule_manager_with_db.reconcile()
    assert result["rules_scanned"] > 0
    assert result["hooks_missing"] > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_manager.py -v -k "async"`
Expected: FAIL — async methods not implemented

- [ ] **Step 3: Implement async methods**

Add to `RuleManager` class in `src/rule_manager.py`:

```python
    def _update_rule_hooks(self, rule_id: str, hook_ids: list[str]) -> None:
        """Update the hooks list in a rule's frontmatter."""
        found = self._find_rule_path(rule_id)
        if not found:
            return
        path, _ = found
        with open(path) as f:
            raw = f.read()
        meta, body = self._split_frontmatter(raw)
        meta["hooks"] = hook_ids
        meta["updated"] = datetime.now(timezone.utc).isoformat()
        with open(path, "w") as f:
            f.write(self._build_file_content(meta, body))

    async def async_save_rule(
        self,
        id: str | None,
        project_id: str | None,
        rule_type: str,
        content: str,
    ) -> dict:
        """Save a rule and generate hooks for active rules."""
        result = self.save_rule(id, project_id, rule_type, content)
        if not result["success"]:
            return result

        # For active rules, generate hooks
        if rule_type == "active" and self._db:
            hooks = await self._generate_hooks_for_rule(result["id"], project_id, content)
            result["hooks_generated"] = hooks

        return result

    async def async_delete_rule(self, rule_id: str) -> dict:
        """Delete a rule and clean up its hooks from the database."""
        # Load hook IDs before deleting the file
        loaded = self.load_rule(rule_id)
        hook_ids = loaded["hooks"] if loaded else []

        result = self.delete_rule(rule_id)
        if not result["success"]:
            return result

        # Remove hooks from DB
        if self._db and hook_ids:
            for hid in hook_ids:
                try:
                    await self._db.delete_hook(hid)
                except Exception as e:
                    logger.warning("Failed to delete hook %s: %s", hid, e)

        return result

    async def _generate_hooks_for_rule(
        self,
        rule_id: str,
        project_id: str | None,
        content: str,
    ) -> list[str]:
        """Generate hooks from an active rule's trigger and logic.

        Parses the rule content to extract trigger parameters and
        creates appropriate hooks. For complex rules, this may
        use an LLM call in the future.
        """
        from src.models import Hook

        if not self._db or not project_id:
            return []

        # Parse trigger from content
        trigger_config = self._parse_trigger(content)
        if not trigger_config:
            logger.info("No parseable trigger in rule %s", rule_id)
            return []

        # Delete old hooks for this rule
        loaded = self.load_rule(rule_id)
        old_hooks = loaded.get("hooks", []) if loaded else []
        for hid in old_hooks:
            try:
                await self._db.delete_hook(hid)
            except Exception:
                pass

        # Create new hook
        import json
        import uuid
        hook_id = f"rule-{rule_id}-{uuid.uuid4().hex[:6]}"
        title = self._extract_title(content)

        hook = Hook(
            id=hook_id,
            project_id=project_id,
            name=f"Rule: {title or rule_id}",
            trigger=json.dumps(trigger_config),
            context_steps="[]",
            prompt_template=(
                f"You are executing rule `{rule_id}`. "
                f"The rule's intent and logic:\n\n{content}\n\n"
                f"Follow the rule's logic and take appropriate action."
            ),
            cooldown_seconds=trigger_config.get("interval_seconds", 3600) // 2,
        )
        await self._db.create_hook(hook)

        # Update rule frontmatter with hook reference
        self._update_rule_hooks(rule_id, [hook_id])

        return [hook_id]

    @staticmethod
    def _parse_trigger(content: str) -> dict | None:
        """Parse trigger configuration from rule content.

        Looks for patterns like "every N minutes/hours" in the Trigger section.
        """
        # Find the Trigger section
        trigger_section = ""
        in_trigger = False
        for line in content.split("\n"):
            if line.strip().startswith("## Trigger"):
                in_trigger = True
                continue
            if in_trigger:
                if line.strip().startswith("## "):
                    break
                trigger_section += line + "\n"

        if not trigger_section.strip():
            return None

        text = trigger_section.lower().strip()

        # Parse "every N minutes/hours"
        match = re.search(r"every\s+(\d+)\s*(minute|min|hour|hr|second|sec)s?", text)
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            if unit.startswith("hour") or unit.startswith("hr"):
                seconds = value * 3600
            elif unit.startswith("min"):
                seconds = value * 60
            else:
                seconds = value
            return {"type": "periodic", "interval_seconds": seconds}

        # Parse event-based triggers
        event_match = re.search(r"when\s+(?:a\s+)?task\s+(?:is\s+)?completed", text)
        if event_match:
            return {"type": "event", "event_type": "task.completed"}

        event_match = re.search(r"when\s+(?:a\s+)?task\s+fails", text)
        if event_match:
            return {"type": "event", "event_type": "task.failed"}

        return None

    async def reconcile(self) -> dict:
        """Startup reconciliation: verify hooks exist for all active rules.

        Scans all rule files, checks that referenced hooks exist in the DB,
        and regenerates missing hooks.
        """
        stats = {
            "rules_scanned": 0,
            "hooks_verified": 0,
            "hooks_missing": 0,
            "hooks_regenerated": 0,
            "errors": 0,
        }

        memory_root = os.path.join(self._storage_root, "memory")
        if not os.path.isdir(memory_root):
            return stats

        for scope_dir in os.listdir(memory_root):
            rules_dir = os.path.join(memory_root, scope_dir, "rules")
            if not os.path.isdir(rules_dir):
                continue

            pid = None if scope_dir == _GLOBAL_SCOPE else scope_dir

            for filename in os.listdir(rules_dir):
                if not filename.endswith(".md"):
                    continue
                filepath = os.path.join(rules_dir, filename)
                try:
                    with open(filepath) as f:
                        raw = f.read()
                    meta, body = self._split_frontmatter(raw)
                    stats["rules_scanned"] += 1

                    if meta.get("type") != "active":
                        continue

                    hook_ids = meta.get("hooks", [])
                    if not hook_ids:
                        stats["hooks_missing"] += 1
                        # Attempt regeneration
                        if self._db and pid:
                            try:
                                new_hooks = await self._generate_hooks_for_rule(
                                    meta.get("id", filename[:-3]), pid, body
                                )
                                if new_hooks:
                                    stats["hooks_regenerated"] += len(new_hooks)
                            except Exception as e:
                                logger.warning("Hook regen failed for %s: %s", filename, e)
                                stats["errors"] += 1
                        continue

                    # Verify referenced hooks exist
                    for hid in hook_ids:
                        if self._db:
                            hook = await self._db.get_hook(hid)
                            if hook:
                                stats["hooks_verified"] += 1
                            else:
                                stats["hooks_missing"] += 1
                                # Regenerate
                                try:
                                    new_hooks = await self._generate_hooks_for_rule(
                                        meta.get("id", filename[:-3]), pid, body
                                    )
                                    if new_hooks:
                                        stats["hooks_regenerated"] += len(new_hooks)
                                except Exception as e:
                                    logger.warning("Hook regen failed for %s: %s", filename, e)
                                    stats["errors"] += 1
                                break  # regenerated all hooks for this rule

                except Exception as e:
                    logger.warning("Failed to process rule %s: %s", filepath, e)
                    stats["errors"] += 1

        return stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_manager.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/rule_manager.py tests/test_rule_manager.py
git commit -m "Add async hook generation, deletion, and startup reconciliation to RuleManager"
```

---

### Task 4: Add rule commands to CommandHandler

**Files:**
- Modify: `tests/test_rule_manager.py`
- Modify: `src/command_handler.py`

- [ ] **Step 1: Write tests for command handler integration**

Add to `tests/test_rule_manager.py`:

```python
async def test_cmd_save_rule(db, storage_root):
    """CommandHandler.execute('save_rule') creates a rule file."""
    from src.models import Project
    from src.rule_manager import RuleManager
    from src.command_handler import CommandHandler

    await db.create_project(Project(id="proj", name="Test"))
    rm = RuleManager(storage_root=storage_root, db=db)

    # Create a minimal orchestrator mock
    orch = MagicMock()
    orch.db = db
    orch.rule_manager = rm
    orch.config = MagicMock()

    handler = CommandHandler(orch)
    result = await handler.execute("save_rule", {
        "project_id": "proj",
        "type": "passive",
        "content": "# Check Code Style\n\n## Intent\nEnforce linting.",
    })
    assert result.get("success") is True or "id" in result


async def test_cmd_browse_rules(db, storage_root):
    """CommandHandler.execute('browse_rules') returns rule list."""
    from src.rule_manager import RuleManager
    from src.command_handler import CommandHandler

    rm = RuleManager(storage_root=storage_root, db=db)
    rm.save_rule("rule-1", "proj", "passive", "# Rule One")

    orch = MagicMock()
    orch.db = db
    orch.rule_manager = rm
    orch.config = MagicMock()

    handler = CommandHandler(orch)
    result = await handler.execute("browse_rules", {"project_id": "proj"})
    assert "rules" in result
    assert any(r["id"] == "rule-1" for r in result["rules"])


async def test_cmd_load_rule(db, storage_root):
    """CommandHandler.execute('load_rule') returns full rule."""
    from src.rule_manager import RuleManager
    from src.command_handler import CommandHandler

    rm = RuleManager(storage_root=storage_root, db=db)
    rm.save_rule("rule-2", "proj", "passive", "# Rule Two\n\nContent here.")

    orch = MagicMock()
    orch.db = db
    orch.rule_manager = rm
    orch.config = MagicMock()

    handler = CommandHandler(orch)
    result = await handler.execute("load_rule", {"id": "rule-2"})
    assert result.get("id") == "rule-2"
    assert "Rule Two" in result.get("content", "")


async def test_cmd_delete_rule(db, storage_root):
    """CommandHandler.execute('delete_rule') removes a rule."""
    from src.rule_manager import RuleManager
    from src.command_handler import CommandHandler

    rm = RuleManager(storage_root=storage_root, db=db)
    rm.save_rule("rule-3", "proj", "passive", "# Rule Three")

    orch = MagicMock()
    orch.db = db
    orch.rule_manager = rm
    orch.config = MagicMock()

    handler = CommandHandler(orch)
    result = await handler.execute("delete_rule", {"id": "rule-3"})
    assert result.get("success") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_manager.py -v -k "cmd_"`
Expected: FAIL — `_cmd_save_rule` etc. not implemented

- [ ] **Step 3: Add command handlers**

Add to `src/command_handler.py` (in the command methods section, after hook commands):

```python
    # -----------------------------------------------------------------------
    # Rule commands
    # -----------------------------------------------------------------------

    async def _cmd_save_rule(self, args: dict) -> dict:
        """Create or update a rule."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        project_id = args.get("project_id")
        rule_type = args.get("type", "passive")
        content = args.get("content", "")

        if not content:
            return {"error": "content is required"}
        if rule_type not in ("active", "passive"):
            return {"error": f"type must be 'active' or 'passive', got '{rule_type}'"}

        result = await rm.async_save_rule(
            id=rule_id,
            project_id=project_id,
            rule_type=rule_type,
            content=content,
        )
        return result

    async def _cmd_delete_rule(self, args: dict) -> dict:
        """Delete a rule and its associated hooks."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        if not rule_id:
            return {"error": "id is required"}

        return await rm.async_delete_rule(rule_id)

    async def _cmd_browse_rules(self, args: dict) -> dict:
        """List rules for a project (plus globals)."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        project_id = args.get("project_id")
        rules = rm.browse_rules(project_id)
        return {"rules": rules}

    async def _cmd_load_rule(self, args: dict) -> dict:
        """Load full details of a specific rule."""
        rm = getattr(self.orchestrator, "rule_manager", None)
        if not rm:
            return {"error": "Rule manager not initialized"}

        rule_id = args.get("id")
        if not rule_id:
            return {"error": "id is required"}

        loaded = rm.load_rule(rule_id)
        if not loaded:
            return {"error": f"Rule '{rule_id}' not found"}

        return loaded
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_manager.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/command_handler.py tests/test_rule_manager.py
git commit -m "Add save_rule, delete_rule, browse_rules, load_rule commands to CommandHandler"
```

---

### Task 5: Add rule tool definitions to ChatAgent

**Files:**
- Modify: `src/chat_agent.py`

- [ ] **Step 1: Add tool definitions**

Add the following tool definitions to the `TOOLS` list in `src/chat_agent.py` (after the existing hook tools):

```python
    {
        "name": "save_rule",
        "description": (
            "Create or update a persistent rule. Rules define autonomous behaviors. "
            "Active rules have triggers (periodic/event) and generate hooks automatically. "
            "Passive rules have no triggers but influence reasoning when relevant. "
            "Content should be markdown with sections: # Title, ## Intent, ## Trigger (active only), ## Logic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Rule ID (optional, auto-generated from title if omitted). Use existing ID to update.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project ID. Omit or null for global rules that apply to all projects.",
                },
                "type": {
                    "type": "string",
                    "enum": ["active", "passive"],
                    "description": "active = has triggers, generates hooks. passive = influences reasoning only.",
                },
                "content": {
                    "type": "string",
                    "description": "Rule content in markdown. Include # Title, ## Intent, ## Trigger (for active), ## Logic.",
                },
            },
            "required": ["type", "content"],
        },
    },
    {
        "name": "delete_rule",
        "description": "Delete a rule and all its associated hooks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Rule ID to delete"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "browse_rules",
        "description": (
            "List all rules for a project (plus global rules). Returns summaries — "
            "use load_rule for full details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID. Returns project rules + all global rules.",
                },
            },
        },
    },
    {
        "name": "load_rule",
        "description": "Load the full content and metadata of a specific rule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Rule ID to load"},
            },
            "required": ["id"],
        },
    },
```

- [ ] **Step 2: Run existing chat agent tests**

Run: `pytest tests/ -v -k "chat" --ignore=tests/chat_eval`
Expected: All existing tests PASS

- [ ] **Step 3: Commit**

```bash
git add src/chat_agent.py
git commit -m "Add save_rule, delete_rule, browse_rules, load_rule tool definitions to ChatAgent"
```

---

### Task 6: Wire PromptBuilder.load_relevant_rules() to RuleManager

**Files:**
- Modify: `tests/test_prompt_builder.py`
- Modify: `src/prompt_builder.py`

- [ ] **Step 1: Write tests for rule loading in PromptBuilder**

Add to `tests/test_prompt_builder.py`:

```python
def test_load_relevant_rules_from_rule_manager(tmp_path, prompts_dir):
    """load_relevant_rules populates Layer 3 from RuleManager."""
    import asyncio
    from src.prompt_builder import PromptBuilder
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root=str(tmp_path))
    rm.save_rule("rule-style", "proj", "passive", "# Code Style\n\n## Intent\nUse black formatter.")
    rm.save_rule("rule-global", None, "passive", "# Global\n\n## Intent\nBe nice.")

    builder = PromptBuilder(
        project_id="proj",
        rule_manager=rm,
        prompts_dir=prompts_dir,
    )
    builder.set_identity("simple")
    asyncio.get_event_loop().run_until_complete(
        builder.load_relevant_rules("code formatting")
    )
    system_prompt, _ = builder.build()

    assert "Code Style" in system_prompt
    assert "Global" in system_prompt
    assert "Applicable Rules" in system_prompt


def test_load_relevant_rules_empty_when_no_rules(prompts_dir):
    """load_relevant_rules produces no output when no rules exist."""
    import asyncio
    from src.prompt_builder import PromptBuilder
    from src.rule_manager import RuleManager

    rm = RuleManager(storage_root="/nonexistent")
    builder = PromptBuilder(
        project_id="proj",
        rule_manager=rm,
        prompts_dir=prompts_dir,
    )
    builder.set_identity("simple")
    asyncio.get_event_loop().run_until_complete(
        builder.load_relevant_rules("anything")
    )
    system_prompt, _ = builder.build()

    assert "Applicable Rules" not in system_prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_prompt_builder.py -v -k "rules"`
Expected: FAIL — `rule_manager` parameter not accepted

- [ ] **Step 3: Update PromptBuilder to accept and use RuleManager**

Modify `src/prompt_builder.py`:

In the constructor, add `rule_manager` parameter:
```python
    def __init__(
        self,
        project_id: str | None = None,
        memory_manager: Any | None = None,
        rule_manager: Any | None = None,
        prompts_dir: Path | str | None = None,
    ):
        self._project_id = project_id
        self._memory_manager = memory_manager
        self._rule_manager = rule_manager
        # ... rest unchanged
```

Replace the `load_relevant_rules` stub:
```python
    async def load_relevant_rules(self, query: str) -> None:
        """Layer 3: Load relevant rules from the rule system.

        Uses RuleManager to load all applicable rules for the current
        project (plus globals). Without memsearch, loads ALL rules.
        Future: semantic search against query for relevance filtering.
        """
        if not self._rule_manager or not self._project_id:
            self._rules = ""
            return
        try:
            rules_text = self._rule_manager.get_rules_for_prompt(
                self._project_id, query
            )
            self._rules = rules_text
        except Exception:
            self._rules = ""  # graceful degradation
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_prompt_builder.py -v`
Expected: All tests PASS (including new rule tests)

- [ ] **Step 5: Commit**

```bash
git add src/prompt_builder.py tests/test_prompt_builder.py
git commit -m "Wire PromptBuilder.load_relevant_rules() to RuleManager"
```

---

### Task 7: Wire RuleManager into Orchestrator startup

**Files:**
- Modify: `tests/test_rule_manager.py`
- Modify: `src/orchestrator.py`

- [ ] **Step 1: Write test for orchestrator integration**

Add to `tests/test_rule_manager.py`:

```python
async def test_orchestrator_initializes_rule_manager(tmp_path):
    """Orchestrator creates RuleManager at initialize()."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.config import AppConfig
    from src.database import Database
    from src.event_bus import EventBus

    config = AppConfig()
    config.data_dir = str(tmp_path)
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    bus = EventBus()

    # Import and create orchestrator
    from src.orchestrator import Orchestrator
    orch = Orchestrator(config, db, bus)

    # After initialize, rule_manager should exist
    with patch.object(orch, '_recover_stale_state', new_callable=AsyncMock):
        with patch.object(orch, '_sync_profiles_from_config', new_callable=AsyncMock):
            await orch.initialize()

    assert hasattr(orch, 'rule_manager')
    assert orch.rule_manager is not None

    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rule_manager.py -v -k "orchestrator"`
Expected: FAIL — `rule_manager` attribute not set

- [ ] **Step 3: Add RuleManager initialization to orchestrator**

In `src/orchestrator.py`, modify `initialize()` (after hook engine setup, around line 703):

```python
        # Initialize rule manager
        from src.rule_manager import RuleManager
        self.rule_manager = RuleManager(
            storage_root=self.config.data_dir,
            db=self.db,
            hook_engine=self.hooks if hasattr(self, 'hooks') else None,
        )

        # Run startup reconciliation for rules → hooks
        try:
            reconcile_stats = await self.rule_manager.reconcile()
            if reconcile_stats.get("rules_scanned", 0) > 0:
                logger.info(
                    "Rule reconciliation: %d rules scanned, %d hooks verified, "
                    "%d hooks missing, %d regenerated",
                    reconcile_stats["rules_scanned"],
                    reconcile_stats["hooks_verified"],
                    reconcile_stats["hooks_missing"],
                    reconcile_stats["hooks_regenerated"],
                )
        except Exception as e:
            logger.warning("Rule reconciliation failed: %s", e)
```

Also add `self.rule_manager = None` in `__init__()` for clean attribute access before initialization.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_manager.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator.py tests/test_rule_manager.py
git commit -m "Initialize RuleManager in orchestrator with startup reconciliation"
```

---

### Task 8: Create default global rules

**Files:**
- Create: `src/prompts/default_rules/post-action-reflection.md`
- Create: `src/prompts/default_rules/periodic-project-review.md`
- Modify: `src/rule_manager.py`
- Modify: `tests/test_rule_manager.py`

- [ ] **Step 1: Create default rule files**

Create `src/prompts/default_rules/post-action-reflection.md`:
```markdown
# Post-Action Reflection

## Intent
After each significant action (task completion, hook execution, user request),
evaluate the result and determine if follow-up work is needed.

## Trigger
When a task is completed.

## Logic
1. Review the completed task's summary and output
2. Check if the result matches the task's acceptance criteria
3. Evaluate any project-specific rules that might apply
4. Determine if follow-up tasks are needed
5. Update project memory with any insights learned
```

Create `src/prompts/default_rules/periodic-project-review.md`:
```markdown
# Periodic Project Review

## Intent
Regularly review project health and catch issues before they become problems.

## Trigger
Check every 15 minutes.

## Logic
1. Check for stuck tasks (tasks in ASSIGNED or IN_PROGRESS for too long)
2. Check for orphaned hooks (hooks referencing deleted projects)
3. Verify rule-hook synchronization
4. Look for BLOCKED tasks with no resolution path
5. Post a summary if any issues are found
```

- [ ] **Step 2: Write test for default rule installation**

Add to `tests/test_rule_manager.py`:

```python
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
    # Count rules — should not have duplicates
    ids = [r["id"] for r in rules]
    assert len(ids) == len(set(ids))
```

- [ ] **Step 3: Implement install_defaults**

Add to `RuleManager` class in `src/rule_manager.py`:

```python
    def install_defaults(self) -> list[str]:
        """Install default global rules from bundled templates.

        Reads markdown files from src/prompts/default_rules/ and saves
        them as global rules. Skips rules that already exist (idempotent).
        """
        defaults_dir = os.path.join(
            os.path.dirname(__file__), "prompts", "default_rules"
        )
        if not os.path.isdir(defaults_dir):
            return []

        installed = []
        for filename in sorted(os.listdir(defaults_dir)):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(defaults_dir, filename)
            with open(filepath) as f:
                content = f.read()

            # Derive ID from filename
            rule_id = f"rule-{filename[:-3]}"

            # Skip if already exists
            if self.load_rule(rule_id):
                continue

            # Determine type from content
            rule_type = "active" if "## Trigger" in content else "passive"

            # Strip any existing frontmatter from the template
            _, body = self._split_frontmatter(content)
            body = body.strip() if body.strip() else content.strip()

            self.save_rule(
                id=rule_id,
                project_id=None,  # global
                rule_type=rule_type,
                content=body,
            )
            installed.append(rule_id)

        return installed
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_rule_manager.py -v -k "default"`
Expected: Both tests PASS

- [ ] **Step 5: Wire install_defaults into orchestrator startup**

In `src/orchestrator.py`, add after rule reconciliation:

```python
        # Install default global rules if not already present
        try:
            installed = self.rule_manager.install_defaults()
            if installed:
                logger.info("Installed %d default global rules: %s", len(installed), installed)
        except Exception as e:
            logger.warning("Default rule installation failed: %s", e)
```

- [ ] **Step 6: Commit**

```bash
git add src/prompts/default_rules/ src/rule_manager.py src/orchestrator.py tests/test_rule_manager.py
git commit -m "Add default global rules and install_defaults() to RuleManager"
```

---

### Task 9: Update specs and PromptBuilder documentation

**Files:**
- Modify: `specs/prompt-builder.md` — document Layer 3 implementation
- Modify: `specs/hooks.md` — note that hooks can be rule-generated
- Modify: `specs/orchestrator.md` — document RuleManager initialization

- [ ] **Step 1: Update prompt-builder spec**

Add to Layer 3 section in `specs/prompt-builder.md`:

```markdown
### Layer 3: Relevant Rules

`load_relevant_rules(query)` queries the `RuleManager` for applicable rules.
Without memsearch, it loads ALL rules for the current project + globals.
With memsearch (future), it would use semantic search against the query
for relevance filtering.

Rules are formatted as a single markdown block under `## Applicable Rules`
with each rule's content included, prefixed with `[Active]` or `[Passive]`.
```

- [ ] **Step 2: Update hooks spec**

Add to `specs/hooks.md`:

```markdown
### Rule-Generated Hooks

Hooks can be generated automatically from active rules via the Rule System
(see `specs/rule-system.md`). Rule-generated hooks:
- Have IDs prefixed with `rule-{rule_id}-`
- Include the rule content in their prompt template
- Are deleted and regenerated when the rule is updated
- Are verified during startup reconciliation
```

- [ ] **Step 3: Update orchestrator spec**

Add to `specs/orchestrator.md`:

```markdown
### Rule Manager Initialization

During `initialize()`, after hook engine setup:
1. Create `RuleManager` with the data directory, database, and hook engine
2. Run `reconcile()` to verify all active rules have valid hooks
3. Run `install_defaults()` to create default global rules if not present
```

- [ ] **Step 4: Commit spec updates**

```bash
git add specs/prompt-builder.md specs/hooks.md specs/orchestrator.md specs/rule-system.md
git commit -m "Update specs for Phase 2 Rule System"
```

---

### Task 10: Final verification and cleanup

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --ignore=tests/chat_eval`
Expected: All tests PASS. No regressions.

- [ ] **Step 2: Run linter**

Run: `ruff check src/rule_manager.py`
Expected: No errors

- [ ] **Step 3: Verify end-to-end flow**

Manually verify:
- `RuleManager.save_rule()` creates valid markdown files with YAML frontmatter
- `browse_rules()` returns both project and global rules
- `load_rule()` finds rules across all scopes
- `delete_rule()` removes files and associated hooks
- `PromptBuilder.load_relevant_rules()` populates Layer 3
- Orchestrator initialization creates RuleManager and runs reconciliation
- Default global rules are installed on first startup

- [ ] **Step 4: Commit any final cleanup**

```bash
git add -A
git commit -m "Phase 2 complete: Rule System with memory-native storage and hook generation"
```

---

## Summary

| Task | What | Files | Tests |
|------|------|-------|-------|
| 1 | Write Rule System spec | `specs/rule-system.md` | -- |
| 2 | Core RuleManager file I/O | `src/rule_manager.py`, tests | 11 tests |
| 3 | Async hook generation + reconciliation | `src/rule_manager.py`, tests | 2 tests |
| 4 | CommandHandler rule commands | `src/command_handler.py`, tests | 4 tests |
| 5 | ChatAgent tool definitions | `src/chat_agent.py` | existing tests |
| 6 | Wire PromptBuilder Layer 3 | `src/prompt_builder.py`, tests | 2 tests |
| 7 | Orchestrator startup integration | `src/orchestrator.py`, tests | 1 test |
| 8 | Default global rules | `src/prompts/default_rules/`, tests | 2 tests |
| 9 | Update specs | `specs/*.md` | -- |
| 10 | Final verification | -- | Full suite |

**Total new tests:** ~22
**Files created:** 5 (`src/rule_manager.py`, `specs/rule-system.md`, `tests/test_rule_manager.py`, 2 default rule templates)
**Files modified:** 4 (`src/command_handler.py`, `src/chat_agent.py`, `src/prompt_builder.py`, `src/orchestrator.py`) + 3 specs
**Backward compatibility:** Existing hooks continue working unchanged. Rules are additive.
