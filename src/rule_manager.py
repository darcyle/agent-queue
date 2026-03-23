"""Rule system for persistent autonomous behaviors.

Rules are structured markdown files with YAML frontmatter stored in the
memory filesystem. Active rules generate hooks for automated execution.
Passive rules influence reasoning via semantic search.

Storage layout:
    ~/.agent-queue/memory/{project_id}/rules/{rule_id}.md
    ~/.agent-queue/memory/global/rules/{rule_id}.md
"""

from __future__ import annotations

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
        orchestrator: Any | None = None,
    ):
        self._storage_root = os.path.expanduser(storage_root)
        self._db = db
        self._hook_engine = hook_engine
        self._orchestrator = orchestrator

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
        frontmatter = yaml.dump(
            meta, default_flow_style=False, sort_keys=False
        ).strip()
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
                    logger.warning(
                        "Failed to read rule file %s: %s", filepath, e
                    )

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

    # ------------------------------------------------------------------
    # Frontmatter update helper
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Async operations (hook generation, deletion, reconciliation)
    # ------------------------------------------------------------------

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
            hooks = await self._generate_hooks_for_rule(
                result["id"], project_id, content
            )
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

        Parses the rule content to extract trigger parameters, then uses the
        supervisor's LLM to expand the rule into a specific, actionable prompt.
        Falls back to a static template when the supervisor is unavailable
        (e.g. during startup reconciliation).

        For global rules (project_id=None), creates one hook per active project.
        """
        if not self._db:
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

        # Try LLM expansion via the supervisor (done once, shared across hooks)
        prompt_template = None
        supervisor = getattr(self._orchestrator, "_supervisor", None)
        if supervisor is not None:
            try:
                expanded = await supervisor.expand_rule_prompt(
                    content, project_id,
                )
                if expanded:
                    prompt_template = expanded
                    logger.info(
                        "LLM-expanded prompt for rule %s (%d chars)",
                        rule_id, len(expanded),
                    )
            except Exception as e:
                logger.warning(
                    "LLM expansion failed for rule %s, using static template: %s",
                    rule_id, e,
                )

        # Fall back to static template
        if not prompt_template:
            prompt_template = (
                f"You are executing rule `{rule_id}`. "
                f"The rule's intent and logic:\n\n{content}\n\n"
                f"Follow the rule's logic and take appropriate action."
            )

        # Determine target projects: specific project or all active projects
        if project_id:
            target_project_ids = [project_id]
        else:
            projects = await self._db.list_projects()
            target_project_ids = [p.id for p in projects]
            if not target_project_ids:
                logger.info(
                    "No projects found for global rule %s, skipping hook creation",
                    rule_id,
                )
                return []

        # Create one hook per target project
        import json
        import uuid

        from src.models import Hook

        title = self._extract_title(content)
        all_hook_ids: list[str] = []

        for pid in target_project_ids:
            hook_id = f"rule-{rule_id}-{uuid.uuid4().hex[:6]}"
            hook = Hook(
                id=hook_id,
                project_id=pid,
                name=f"Rule: {title or rule_id}",
                trigger=json.dumps(trigger_config),
                context_steps="[]",
                prompt_template=prompt_template,
                cooldown_seconds=trigger_config.get("interval_seconds", 3600) // 2,
            )
            await self._db.create_hook(hook)
            all_hook_ids.append(hook_id)

        if not project_id:
            logger.info(
                "Global rule %s: created %d hooks (one per project)",
                rule_id, len(all_hook_ids),
            )

        # Update rule frontmatter with hook references
        self._update_rule_hooks(rule_id, all_hook_ids)

        return all_hook_ids

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
        match = re.search(
            r"every\s+(\d+)\s*(minute|min|hour|hr|second|sec)s?", text
        )
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
        event_match = re.search(
            r"when\s+(?:a\s+)?task\s+(?:is\s+)?completed", text
        )
        if event_match:
            return {"type": "event", "event_type": "task.completed"}

        event_match = re.search(r"when\s+(?:a\s+)?task\s+fails", text)
        if event_match:
            return {"type": "event", "event_type": "task.failed"}

        return None

    async def reconcile(self) -> dict:
        """Startup reconciliation: regenerate hooks for all active rules.

        Scans all rule files and unconditionally regenerates hooks from each
        active rule.  This ensures hooks stay in sync with rule content even
        if the rule files or database were edited outside the running system.
        """
        stats = {
            "rules_scanned": 0,
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

                    if not self._db:
                        continue

                    try:
                        new_hooks = await self._generate_hooks_for_rule(
                            meta.get("id", filename[:-3]),
                            pid,
                            body,
                        )
                        if new_hooks:
                            stats["hooks_regenerated"] += len(new_hooks)
                    except Exception as e:
                        logger.warning(
                            "Hook regen failed for %s: %s",
                            filename, e,
                        )
                        stats["errors"] += 1

                except Exception as e:
                    logger.warning(
                        "Failed to process rule %s: %s", filepath, e
                    )
                    stats["errors"] += 1

        return stats

    # ------------------------------------------------------------------
    # Default rule installation
    # ------------------------------------------------------------------

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
