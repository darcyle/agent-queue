"""Rule system for persistent autonomous behaviors.

Rules are structured markdown files with YAML frontmatter stored in the
memory filesystem.  There are two rule types:

- **Active rules** generate hooks for automated execution.  When an active
  rule is saved, the ``RuleManager`` creates (or updates) a corresponding
  hook in the hook engine so the rule's intent is carried out on a
  schedule or in response to events.
- **Passive rules** influence reasoning via semantic search.  They are
  injected into the Supervisor's system prompt when relevant to the
  current conversation.

Storage layout::

    ~/.agent-queue/memory/{project_id}/rules/{rule_id}.md
    ~/.agent-queue/memory/global/rules/{rule_id}.md

File format: YAML frontmatter (``id``, ``type``, ``project_id``, ``hooks``,
``created``, ``updated``) followed by Markdown body content.

The rule system intentionally keeps the rule file as the single source of
truth.  Hooks are derived artifacts — ``reconcile_hooks()`` can always
reconstruct the hook set from the rule files.

See ``specs/rules.md`` for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_GLOBAL_SCOPE = "global"


def _compute_source_hash(trigger_config: dict, prompt_content: str) -> str:
    """Compute a stable content hash from trigger config + prompt content.

    Used for idempotent reconciliation — if the hash hasn't changed,
    hooks don't need to be regenerated.
    """
    import json

    # Sort keys for deterministic JSON serialisation
    canonical = json.dumps(trigger_config, sort_keys=True) + "\n" + prompt_content
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class RuleManager:
    """Manages rule file I/O, hook generation, and reconciliation.

    Rules are the source of truth.  Hooks are derived artifacts that
    implement active rules via the existing hook engine.  The manager
    provides CRUD operations for rule files and coordinates with the
    hook engine and database when rules change.

    Usage::

        mgr = RuleManager("~/.agent-queue", db=db, hook_engine=hooks)
        result = mgr.save_rule(None, "my-project", "active", "# Check tests\\n...")
        await mgr.generate_hooks_for_rule(result["id"], "my-project")

    Attributes:
        _storage_root: Base directory for rule file storage.
        _db: Database instance for hook metadata queries.
        _hook_engine: Hook engine for creating/deleting derived hooks.
        _orchestrator: Orchestrator reference for LLM-based rule expansion.
    """

    def __init__(
        self,
        storage_root: str,
        db: Any | None = None,
        hook_engine: Any | None = None,
        orchestrator: Any | None = None,
    ):
        """Initialise the rule manager.

        Args:
            storage_root: Base directory (e.g. ``~/.agent-queue``).
                Rule files are stored under ``{storage_root}/memory/``.
            db: Optional database for hook metadata queries.
            hook_engine: Optional hook engine for creating derived hooks.
            orchestrator: Optional orchestrator for LLM-based rule prompt
                expansion.
        """
        self._storage_root = os.path.expanduser(storage_root)
        self._db = db
        self._hook_engine = hook_engine
        self._orchestrator = orchestrator
        # Prevents concurrent reconciliation runs (e.g. rapid Discord reconnects)
        self._reconcile_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _rules_dir(self, project_id: str | None) -> str:
        """Return the directory containing rules for a given scope.

        Args:
            project_id: Project ID, or ``None`` for global scope.
        """
        scope = project_id or _GLOBAL_SCOPE
        return os.path.join(self._storage_root, "memory", scope, "rules")

    def _rule_path(self, rule_id: str, project_id: str | None) -> str:
        """Return the full filesystem path for a rule file.

        Args:
            rule_id: Unique rule identifier (used as filename stem).
            project_id: Project scope, or ``None`` for global.
        """
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
        """Serialise metadata and body into a frontmatter Markdown file.

        Args:
            meta: YAML frontmatter dict.
            body: Markdown body content.

        Returns:
            Complete file content string with ``---`` delimiters.
        """
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

        If *id* matches an existing rule, it is updated (preserving
        ``created`` timestamp and hook associations).  Otherwise a new
        rule is created.  Auto-generates id from the first ``# Title``
        heading if omitted.

        Args:
            id: Rule identifier, or ``None`` to auto-generate.
            project_id: Project scope, or ``None`` for global.
            rule_type: ``"active"`` or ``"passive"``.
            content: Markdown body content for the rule.

        Returns:
            Dict with ``success``, ``id``, and ``hooks_generated`` keys.
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
        """Load a rule by ID, searching all scopes.

        Args:
            rule_id: The rule identifier to look up.

        Returns:
            Dict with ``id``, ``type``, ``project_id``, ``hooks``,
            ``content``, ``created``, ``updated`` keys — or ``None``
            if the rule was not found.
        """
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
        """Delete a rule file.  Hook cleanup is handled by the async wrapper.

        Args:
            rule_id: The rule identifier to delete.

        Returns:
            Dict with ``success`` and ``hooks_removed`` keys.
        """
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
        """List rules for a project plus all global rules.

        Args:
            project_id: Project to list rules for.  Global rules are
                always included regardless of this value.

        Returns:
            List of rule summary dicts with ``id``, ``type``,
            ``project_id``, ``title``, ``summary``, and ``hooks`` keys.
        """
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

        Uses content-hash based reconciliation to skip regeneration when
        the rule hasn't changed. When regeneration IS needed, uses atomic
        replacement: create new hooks first, verify success, then delete old.

        For global rules (project_id=None), creates one hook per active project.
        """
        if not self._db:
            return []

        # Parse trigger from content
        trigger_config = self._parse_trigger(content)
        if not trigger_config:
            logger.info("No parseable trigger in rule %s", rule_id)
            return []

        # Compute content hash for this rule's current state
        new_hash = _compute_source_hash(trigger_config, content)

        # Check existing hooks — if all share the same source_hash,
        # the rule hasn't changed and we can skip regeneration entirely.
        prefix = f"rule-{rule_id}-"
        existing_hooks = []
        preserved_timestamps: dict[str, float] = {}  # project_id -> last_triggered_at
        try:
            existing_hooks = await self._db.list_hooks_by_id_prefix(prefix)
            for old_hook in existing_hooks:
                if old_hook.last_triggered_at:
                    prev = preserved_timestamps.get(old_hook.project_id, 0)
                    if old_hook.last_triggered_at > prev:
                        preserved_timestamps[old_hook.project_id] = old_hook.last_triggered_at
        except Exception as e:
            logger.debug(
                "Could not read old hooks for rule %s: %s", rule_id, e,
            )

        # Content-hash check: skip regeneration if all existing hooks match
        if existing_hooks:
            all_match = all(h.source_hash == new_hash for h in existing_hooks)
            if all_match:
                logger.debug(
                    "Rule %s unchanged (hash=%s), skipping hook regeneration",
                    rule_id, new_hash,
                )
                return [h.id for h in existing_hooks]

        # --- Regeneration needed ---

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

        # ATOMIC REPLACEMENT: Create new hooks first, then delete old ones.
        # This prevents the window where no hooks exist for a rule.
        import json
        import uuid

        from src.models import Hook

        title = self._extract_title(content)
        new_hook_ids: list[str] = []
        creation_succeeded = True

        for pid in target_project_ids:
            hook_id = f"rule-{rule_id}-{uuid.uuid4().hex[:6]}"
            # Restore last_triggered_at from the old hook (if any) so periodic
            # hooks don't re-fire immediately after reconciliation/restart.
            restored_ts = preserved_timestamps.get(pid)
            hook = Hook(
                id=hook_id,
                project_id=pid,
                name=f"Rule: {title or rule_id}",
                trigger=json.dumps(trigger_config),
                context_steps="[]",
                prompt_template=prompt_template,
                cooldown_seconds=trigger_config.get("interval_seconds", 3600) // 2,
                last_triggered_at=restored_ts,
                source_hash=new_hash,
            )
            try:
                await self._db.create_hook(hook)
                new_hook_ids.append(hook_id)
            except Exception as e:
                logger.error(
                    "Failed to create hook %s for rule %s: %s",
                    hook_id, rule_id, e,
                )
                creation_succeeded = False
                break

        if not creation_succeeded:
            # Clean up any partially created new hooks
            for hid in new_hook_ids:
                try:
                    await self._db.delete_hook(hid)
                except Exception:
                    pass
            logger.error(
                "Atomic hook creation failed for rule %s, keeping old hooks",
                rule_id,
            )
            return [h.id for h in existing_hooks]

        # New hooks created successfully — now delete old hooks
        old_hook_ids = [h.id for h in existing_hooks]
        for hid in old_hook_ids:
            try:
                await self._db.delete_hook(hid)
            except Exception as e:
                logger.warning(
                    "Failed to delete old hook %s during atomic replacement: %s",
                    hid, e,
                )

        if not project_id and len(new_hook_ids) > 1:
            logger.info(
                "Global rule %s: created %d hooks (one per project)",
                rule_id, len(new_hook_ids),
            )

        # Update rule frontmatter with new hook references atomically
        self._update_rule_hooks(rule_id, new_hook_ids)

        return new_hook_ids

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

    # ------------------------------------------------------------------
    # Orphan hook migration
    # ------------------------------------------------------------------

    async def migrate_orphan_hooks(self) -> dict:
        """Migrate direct/orphan hooks into rule-backed hooks.

        Queries all hooks from the database and converts any hook whose ID
        does **not** start with ``rule-`` into a proper rule markdown file.
        After the rule file is saved, the original orphan hook is deleted.
        Subsequent reconciliation will regenerate rule-backed hooks from the
        new rule files.

        This method is idempotent: if a rule file already exists for a given
        hook name, the hook is simply deleted (the rule is the source of
        truth).

        Returns:
            Dict with ``migrated``, ``skipped``, and ``errors`` counts.
        """
        import json

        stats = {"migrated": 0, "skipped": 0, "errors": 0}

        if not self._db:
            return stats

        try:
            all_hooks = await self._db.list_hooks()
        except Exception as e:
            logger.error("Failed to list hooks for orphan migration: %s", e)
            return stats

        for hook in all_hooks:
            if hook.id.startswith("rule-"):
                stats["skipped"] += 1
                continue

            try:
                # Build rule content from hook config
                title = hook.name or hook.id
                body_parts = [f"# {title}"]

                # Reverse-parse trigger JSON into natural language
                trigger_section = self._reverse_parse_trigger(hook.trigger)
                if hook.cooldown_seconds:
                    trigger_section += f"\nCooldown: {hook.cooldown_seconds} seconds."
                body_parts.append(f"\n## Trigger\n\n{trigger_section}")

                # Use prompt_template as the Logic section
                logic = hook.prompt_template or "No logic defined."
                body_parts.append(f"\n## Logic\n\n{logic}")

                content = "\n".join(body_parts)

                # Derive a rule ID from the hook name/id
                rule_id = self._id_from_title(title)

                # Check if a rule already exists for this hook (idempotent)
                if self.load_rule(rule_id):
                    logger.info(
                        "Rule %s already exists for orphan hook %s, deleting hook only",
                        rule_id, hook.id,
                    )
                else:
                    # Save the rule file
                    result = self.save_rule(
                        id=rule_id,
                        project_id=hook.project_id,
                        rule_type="active",
                        content=content,
                    )
                    if not result.get("success"):
                        logger.warning(
                            "Failed to save rule for orphan hook %s: %s",
                            hook.id, result,
                        )
                        stats["errors"] += 1
                        continue

                    logger.info(
                        "Migrated orphan hook %s → rule %s",
                        hook.id, rule_id,
                    )

                # Delete the original orphan hook
                await self._db.delete_hook(hook.id)
                stats["migrated"] += 1

            except Exception as e:
                logger.warning(
                    "Failed to migrate orphan hook %s: %s", hook.id, e,
                )
                stats["errors"] += 1

        if stats["migrated"]:
            logger.info(
                "Orphan hook migration complete: %d migrated, %d skipped, %d errors",
                stats["migrated"], stats["skipped"], stats["errors"],
            )

        return stats

    @staticmethod
    def _reverse_parse_trigger(trigger_json: str) -> str:
        """Convert a hook trigger JSON string back into natural language.

        Args:
            trigger_json: JSON string like ``{"type": "periodic", "interval_seconds": 3600}``.

        Returns:
            Human-readable trigger description for a rule's ``## Trigger`` section.
        """
        import json

        try:
            trigger = json.loads(trigger_json) if trigger_json else {}
        except (json.JSONDecodeError, TypeError):
            return "Unknown trigger configuration."

        if not trigger:
            return "No trigger defined."

        trigger_type = trigger.get("type", "unknown")

        if trigger_type == "periodic":
            seconds = trigger.get("interval_seconds", 0)
            if seconds >= 86400 and seconds % 86400 == 0:
                value = seconds // 86400
                unit = "day" if value == 1 else "days"
            elif seconds >= 3600 and seconds % 3600 == 0:
                value = seconds // 3600
                unit = "hour" if value == 1 else "hours"
            elif seconds >= 60 and seconds % 60 == 0:
                value = seconds // 60
                unit = "minute" if value == 1 else "minutes"
            else:
                value = seconds
                unit = "second" if value == 1 else "seconds"
            return f"Every {value} {unit}."

        if trigger_type == "event":
            event_type = trigger.get("event_type", "unknown")
            # Convert dotted event types to natural language
            readable = event_type.replace(".", " ").replace("_", " ")
            return f"When {readable}."

        if trigger_type == "cron":
            expression = trigger.get("cron", trigger.get("expression", ""))
            return f"Cron schedule: `{expression}`."

        # Fallback for unknown trigger types
        return f"Trigger type: {trigger_type}."

    async def reconcile(self) -> dict:
        """Startup reconciliation: regenerate hooks for all active rules.

        Uses an asyncio.Lock to prevent concurrent reconciliation runs
        (e.g. from rapid Discord reconnects). Content-hash based comparison
        in _generate_hooks_for_rule skips regeneration for unchanged rules.

        Also migrates any orphan hooks (created directly, not via rules) into
        rule files before reconciliation, so that all automation has a rule
        file as its source of truth.
        """
        if self._reconcile_lock.locked():
            logger.info("Reconciliation already in progress, skipping")
            return {
                "rules_scanned": 0,
                "hooks_regenerated": 0,
                "errors": 0,
                "skipped": True,
            }

        async with self._reconcile_lock:
            return await self._reconcile_inner()

    async def _reconcile_inner(self) -> dict:
        """Inner reconciliation logic, called under the reconcile lock."""
        # Migrate orphan hooks first (idempotent — safe to run every startup)
        migration_stats = await self.migrate_orphan_hooks()
        if migration_stats["migrated"]:
            logger.info(
                "Pre-reconciliation migration: %d orphan hooks converted to rules",
                migration_stats["migrated"],
            )

        stats = {
            "rules_scanned": 0,
            "hooks_regenerated": 0,
            "errors": 0,
            "orphan_hooks_migrated": migration_stats["migrated"],
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
