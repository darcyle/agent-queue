"""LLM-based plan parser using the ChatProvider infrastructure.

Uses Claude (or another configured provider) to extract actionable
implementation steps from raw plan markdown, returning structured JSON
via tool_use.  Falls back to the regex-based ``parse_plan()`` when the
LLM call fails or no provider is available.
"""

from __future__ import annotations

from src.chat_providers.base import ChatProvider
from src.plan_parser import ParsedPlan, PlanStep, parse_plan
from src.prompt_registry import registry as _prompt_registry

# The plan parser system prompt now lives in src/prompts/plan_parser_system.md.
# Loaded via the prompt registry at call time.
_SYSTEM_PROMPT: str | None = None


def _get_system_prompt() -> str:
    """Lazily load the plan-parser system prompt from the registry."""
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = _prompt_registry.get("plan-parser-system")
    return _SYSTEM_PROMPT

_TOOL = {
    "name": "extract_plan_steps",
    "description": "Extract high-level implementation phases from a plan. Each phase should group many related steps into one substantial chunk of work. Each phase description must start with a numbered outline of steps.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short imperative title for the phase (a cohesive area of work).",
                        },
                        "description": {
                            "type": "string",
                            "description": "Must start with 'Steps in this phase:' followed by a numbered outline, then full implementation details for each step.",
                        },
                    },
                    "required": ["title", "description"],
                },
                "description": "Ordered list of high-level implementation phases.",
            },
        },
        "required": ["steps"],
    },
}


async def parse_plan_with_llm(
    raw_content: str,
    provider: ChatProvider,
    source_file: str = "",
    max_steps: int = 20,
) -> ParsedPlan:
    """Parse a plan using an LLM provider with tool_use for structured output.

    Falls back to the regex-based parser on any error.
    """
    response = await provider.create_message(
        messages=[{"role": "user", "content": raw_content}],
        system=_get_system_prompt(),
        tools=[_TOOL],
        max_tokens=4096,
    )

    # Extract the tool_use result
    for block in response.tool_uses:
        if block.name == "extract_plan_steps":
            raw_steps = block.input.get("steps", [])
            steps = []
            for i, s in enumerate(raw_steps[:max_steps]):
                title = s.get("title", "").strip()
                description = s.get("description", "").strip()
                if title and description:
                    steps.append(PlanStep(
                        title=title,
                        description=description,
                        priority_hint=i,
                    ))
            return ParsedPlan(
                source_file=source_file,
                steps=steps,
                raw_content=raw_content,
            )

    # No tool_use block found — fall back to regex parser
    return parse_plan(raw_content, source_file=source_file, max_steps=max_steps)
