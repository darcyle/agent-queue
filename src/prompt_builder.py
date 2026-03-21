"""Unified prompt assembly pipeline.

Replaces prompt_registry.py, prompt_manager.py, and scattered string
concatenation across orchestrator, adapters, chat_agent, and hooks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass
class _TemplateCache:
    """Parsed template with metadata and body."""

    name: str
    body: str
    variables: list[dict] = field(default_factory=list)
    category: str = ""
    metadata: dict = field(default_factory=dict)


class PromptBuilder:
    """Unified 5-layer prompt assembly pipeline.

    Layers:
        1. Identity — who is the LLM? (loaded from template)
        2. Project context — what project? (from memory)
        3. Relevant rules — what rules apply? (Phase 2)
        4. Specific context — named context blocks
        5. Tools — JSON Schema tool definitions
    """

    def __init__(
        self,
        project_id: str | None = None,
        memory_manager: Any | None = None,
        prompts_dir: Path | str | None = None,
    ):
        self._project_id = project_id
        self._memory_manager = memory_manager
        self._prompts_dir = Path(prompts_dir) if prompts_dir else _DEFAULT_PROMPTS_DIR

        # Layer state
        self._identity: str = ""
        self._project_context: str = ""
        self._rules: str = ""
        self._context_blocks: list[tuple[str, str]] = []
        self._tools: list[dict] = []

        # Template cache
        self._templates: dict[str, _TemplateCache] = {}
        self._templates_loaded = False

    # ------------------------------------------------------------------
    # Template loading
    # ------------------------------------------------------------------

    def _ensure_templates_loaded(self) -> None:
        if self._templates_loaded:
            return
        self._templates_loaded = True
        if not self._prompts_dir.is_dir():
            return
        for md_file in self._prompts_dir.glob("*.md"):
            tpl = self._parse_template_file(md_file)
            if tpl:
                self._templates[tpl.name] = tpl

    def _parse_template_file(self, path: Path) -> _TemplateCache | None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None

        frontmatter, body = self._split_frontmatter(content)
        name = frontmatter.get("name", path.stem)
        variables = frontmatter.get("variables", [])
        category = frontmatter.get("category", "")

        return _TemplateCache(
            name=name,
            body=body.strip(),
            variables=variables if isinstance(variables, list) else [],
            category=category,
            metadata=frontmatter,
        )

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
        return meta, parts[2]

    def _render_variables(self, body: str, variables: dict[str, str] | None) -> str:
        if not variables:
            return body

        def replacer(match: re.Match) -> str:
            var_name = match.group(1).strip()
            return str(variables.get(var_name, match.group(0)))

        return re.sub(r"\{\{(.+?)\}\}", replacer, body)

    # ------------------------------------------------------------------
    # Public template API
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Force re-read templates from disk."""
        self._templates.clear()
        self._templates_loaded = False

    def get_template(self, name: str) -> str | None:
        """Return raw template body with {{placeholders}} intact."""
        self._ensure_templates_loaded()
        tpl = self._templates.get(name)
        return tpl.body if tpl else None

    def render_template(
        self, name: str, variables: dict[str, str] | None = None
    ) -> str | None:
        """Load and render a template with variable substitution."""
        self._ensure_templates_loaded()
        tpl = self._templates.get(name)
        if not tpl:
            return None
        return self._render_variables(tpl.body, variables)
