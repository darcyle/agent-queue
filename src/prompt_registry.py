"""Centralized prompt registry for all system prompts.

Loads prompt templates from ``src/prompts/`` (Markdown files with YAML
frontmatter and ``{{variable}}`` placeholders) using the existing
:mod:`prompt_manager` infrastructure.  Consumers import the registry
singleton and call :func:`get` or :func:`render` instead of embedding
large string constants inline.

Usage::

    from src.prompt_registry import registry

    # Get raw template body (with {{placeholders}} intact)
    body = registry.get("plan-structure-guide")

    # Render with variable substitution
    rendered = registry.render("chat-agent-system", {"workspace_dir": "/tmp/ws"})

The registry is intentionally lazy — templates are loaded from disk on
first access and cached.  Call :func:`reload` to force a re-read (e.g.
after a hot-reload event).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.prompt_manager import PromptManager, PromptTemplate, render_template

logger = logging.getLogger(__name__)

# Directory containing the built-in system prompt templates.
_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


class PromptRegistry:
    """Singleton registry for built-in system prompt templates.

    Wraps :class:`~src.prompt_manager.PromptManager` to load templates
    from the ``src/prompts/`` directory, with caching and convenience
    accessors.
    """

    def __init__(self, prompts_dir: str = _PROMPTS_DIR):
        self._prompts_dir = prompts_dir
        self._manager = PromptManager(prompts_dir)
        self._cache: dict[str, PromptTemplate] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Lazily load all templates on first access."""
        if self._loaded:
            return
        self._cache.clear()
        for tmpl in self._manager.list_templates():
            self._cache[tmpl.name] = tmpl
        self._loaded = True
        logger.debug(
            "PromptRegistry loaded %d templates from %s",
            len(self._cache),
            self._prompts_dir,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Force a re-read of all templates from disk."""
        self._loaded = False
        self._ensure_loaded()

    def list_names(self) -> list[str]:
        """Return sorted list of all registered template names."""
        self._ensure_loaded()
        return sorted(self._cache.keys())

    def get_template(self, name: str) -> PromptTemplate | None:
        """Return the full :class:`PromptTemplate` object, or *None*."""
        self._ensure_loaded()
        return self._cache.get(name)

    def get(self, name: str) -> str:
        """Return the raw template body (with ``{{placeholders}}`` intact).

        Raises :class:`KeyError` if the template does not exist.
        """
        self._ensure_loaded()
        tmpl = self._cache.get(name)
        if tmpl is None:
            raise KeyError(f"Prompt template '{name}' not found in registry")
        return tmpl.body

    def render(
        self,
        name: str,
        variables: dict[str, str] | None = None,
        *,
        strict: bool = False,
    ) -> str:
        """Render a template with variable substitution.

        Parameters
        ----------
        name : str
            Template name (as declared in YAML frontmatter or derived
            from filename).
        variables : dict or None
            Mapping of placeholder names to values.
        strict : bool
            If *True*, raise on missing required variables.

        Raises
        ------
        KeyError
            If the template does not exist.
        ValueError
            If *strict* is True and a required variable is missing.
        """
        self._ensure_loaded()
        tmpl = self._cache.get(name)
        if tmpl is None:
            raise KeyError(f"Prompt template '{name}' not found in registry")
        return render_template(tmpl, variables, strict=strict)


# Module-level singleton.
registry = PromptRegistry()
