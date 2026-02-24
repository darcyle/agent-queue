"""LLM-based plan parser using the ChatProvider infrastructure.

Uses Claude (or another configured provider) to extract actionable
implementation steps from raw plan markdown, returning structured JSON
via tool_use.  Falls back to the regex-based ``parse_plan()`` when the
LLM call fails or no provider is available.
"""

from __future__ import annotations

from src.chat_providers.base import ChatProvider
from src.plan_parser import ParsedPlan, PlanStep, parse_plan

_SYSTEM_PROMPT = """\
You are a plan parser. Given a markdown implementation plan, extract ONLY the
actionable implementation steps — concrete coding tasks that an agent should execute.

Skip non-actionable sections: overviews, summaries, background, conclusions,
dependency graphs, file inventories, etc.

Each step title should be an imperative action (e.g. "Add user auth endpoint").
Each step description should contain all implementation details needed.

Return the steps using the extract_plan_steps tool."""

_TOOL = {
    "name": "extract_plan_steps",
    "description": "Extract actionable implementation steps from a plan.",
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
                            "description": "Short imperative title for the step.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Full implementation details for the step.",
                        },
                    },
                    "required": ["title", "description"],
                },
                "description": "Ordered list of actionable implementation steps.",
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
        system=_SYSTEM_PROMPT,
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
