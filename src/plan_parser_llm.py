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
You are a plan parser. Given a markdown implementation plan, extract the
HIGH-LEVEL IMPLEMENTATION PHASES — coarse groups of related work that each
represent a substantial, independently-executable chunk of the project.

IMPORTANT — granularity rules:
- Extract 3-5 phases (never more than 8).
- Each phase should bundle several related steps or sub-tasks together.
- Do NOT extract individual fine-grained steps (e.g. "add import", "create file",
  "write test for X") as separate phases. Group them under a broader phase.
- A good phase title describes a cohesive area of work
  (e.g. "Implement database layer and migrations",
  "Build REST API endpoints with validation",
  "Add frontend components and integrate with API").

Skip non-actionable sections: overviews, summaries, background, conclusions,
dependency graphs, file inventories, etc.

Each phase title should be an imperative action phrase.
Each phase description should contain ALL implementation details and sub-steps
needed to complete the phase — the executing agent will rely on this description.

Return the phases using the extract_plan_steps tool."""

_TOOL = {
    "name": "extract_plan_steps",
    "description": "Extract high-level implementation phases from a plan. Each phase should group several related steps into one substantial chunk of work. Return 3-5 phases (max 8).",
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
                            "description": "Full implementation details including all sub-steps needed for this phase.",
                        },
                    },
                    "required": ["title", "description"],
                },
                "maxItems": 8,
                "description": "Ordered list of high-level implementation phases (3-5 recommended, max 8).",
            },
        },
        "required": ["steps"],
    },
}


async def parse_plan_with_llm(
    raw_content: str,
    provider: ChatProvider,
    source_file: str = "",
    max_steps: int = 8,
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
