"""Reflection engine for the Supervisor's action-reflect cycle.

Every action the Supervisor takes gets a verification pass. The
reflection depth varies by trigger type and configured level.

Safety controls prevent runaway loops:
- Maximum reflection depth (default 3)
- Per-cycle token cap
- Hourly circuit breaker
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from src.config import ReflectionConfig


@dataclass
class ReflectionVerdict:
    """Structured verdict from a reflection pass."""
    passed: bool
    reason: str
    suggested_followup: str | None = None

_DEEP_TRIGGERS = {"task.completed", "task.failed", "hook.failed"}
_STANDARD_TRIGGERS = {"user.request", "hook.completed"}
_LIGHT_TRIGGERS = {"passive.observation", "periodic.sweep"}


class ReflectionEngine:
    """Manages the action-reflect cycle for the Supervisor."""

    def __init__(self, config: ReflectionConfig):
        self._config = config
        self._token_ledger: list[tuple[float, int]] = []

    @property
    def level(self) -> str:
        return self._config.level

    def should_reflect(self, trigger: str) -> bool:
        if self._config.level == "off":
            return False
        if self.is_circuit_breaker_tripped():
            return False
        return True

    def determine_depth(self, trigger: str, context: dict) -> str | None:
        if self._config.level == "off":
            return None
        if self._config.level == "minimal":
            return "light"
        if self._config.level == "moderate":
            if trigger in _DEEP_TRIGGERS:
                return "standard"
            return "light"
        # full
        if trigger in _DEEP_TRIGGERS:
            return "deep"
        if trigger in _STANDARD_TRIGGERS:
            return "standard"
        return "light"

    def build_reflection_prompt(self, depth: str, trigger: str,
                                 action_summary: str, action_results: list[dict]) -> str:
        if depth == "deep":
            return self._build_deep_prompt(trigger, action_summary, action_results)
        if depth == "standard":
            return self._build_standard_prompt(trigger, action_summary, action_results)
        return self._build_light_prompt(trigger, action_summary, action_results)

    def _build_deep_prompt(self, trigger: str, summary: str, results: list[dict]) -> str:
        results_text = "\n".join(
            f"- {r.get('tool', 'action')}: {r.get('result', '')}" for r in results
        ) if results else "No tool results."
        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Action taken:** {summary}\n\n"
            f"**Results:**\n{results_text}\n\n"
            "Evaluate this action:\n"
            "1. Did I do what was asked/intended?\n"
            "2. Did the actions succeed? **Verify concretely** — if you created "
            "a task, call `list_tasks` to confirm it exists. If you updated "
            "something, read it back. Do not assume success.\n"
            "3. Are there relevant rules I should evaluate now? Use "
            "`browse_rules` to check.\n"
            "4. Did I learn anything that should update memory?\n"
            "5. Is there follow-up work needed?\n\n"
            "If follow-up is needed, take action. Otherwise, confirm completion.\n\n"
            "After your analysis, output a JSON verdict on its own line:\n"
            '```json\n{"passed": true/false, "reason": "...", "followup": "suggested followup or null"}\n```'
        )

    def _build_standard_prompt(self, trigger: str, summary: str, results: list[dict]) -> str:
        results_text = "\n".join(
            f"- {r.get('tool', 'action')}: {r.get('result', '')}" for r in results
        ) if results else "No tool results."
        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Action taken:** {summary}\n\n"
            f"**Results:**\n{results_text}\n\n"
            "Quick check:\n"
            "1. Did the action succeed?\n"
            "2. Any directly relevant rules to check?\n\n"
            "After your analysis, output a JSON verdict on its own line:\n"
            '```json\n{"passed": true/false, "reason": "...", "followup": "suggested followup or null"}\n```'
        )

    def _build_light_prompt(self, trigger: str, summary: str, results: list[dict]) -> str:
        return (
            f"## Reflection (trigger: {trigger})\n\n"
            f"**Summary:** {summary}\n\n"
            "Update memory if this is relevant. Skip if not notable."
        )

    def can_reflect_deeper(self, current_depth: int) -> bool:
        return current_depth < self._config.max_depth

    def can_continue_cycle(self, tokens_used: int) -> bool:
        return tokens_used < self._config.per_cycle_token_cap

    def record_tokens(self, tokens: int) -> None:
        self._token_ledger.append((time.time(), tokens))

    def hourly_tokens_used(self) -> int:
        cutoff = time.time() - 3600
        return sum(t for ts, t in self._token_ledger if ts > cutoff)

    def is_circuit_breaker_tripped(self) -> bool:
        return self.hourly_tokens_used() >= self._config.hourly_token_circuit_breaker

    @staticmethod
    def parse_verdict(text: str) -> ReflectionVerdict:
        """Extract a structured verdict from the LLM reflection response.

        Looks for a JSON block in the response text. Falls back to
        ``passed=True`` if no valid JSON is found (safe default — don't
        retry if we can't parse the verdict).
        """
        # Try to find JSON in a fenced code block first
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if not match:
            # Try bare JSON object on a line
            match = re.search(r'(\{"passed".*?\})', text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return ReflectionVerdict(
                    passed=bool(data.get("passed", True)),
                    reason=str(data.get("reason", "")),
                    suggested_followup=data.get("followup"),
                )
            except (json.JSONDecodeError, TypeError):
                pass
        # Default: assume passed if we can't parse
        return ReflectionVerdict(passed=True, reason="Could not parse verdict")
