"""Test cases for chat analyzer tools: analyzer_status, analyzer_toggle, analyzer_history."""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- analyzer_status ---
    TestCase(
        id="analyzer-status-simple",
        description="Check if the chat analyzer is running",
        category="analyzer",
        difficulty=Difficulty.TRIVIAL,
        tags=["analyzer_status"],
        turns=[
            Turn(
                user_message="is the chat analyzer enabled?",
                expected_tools=[ExpectedTool(name="analyzer_status")],
            ),
        ],
    ),
    TestCase(
        id="analyzer-status-natural",
        description="Ask about analyzer status with natural phrasing",
        category="analyzer",
        difficulty=Difficulty.EASY,
        tags=["analyzer_status", "natural-language"],
        turns=[
            Turn(
                user_message="what's the analyzer doing?",
                expected_tools=[ExpectedTool(name="analyzer_status")],
            ),
        ],
    ),
    TestCase(
        id="analyzer-status-with-project",
        description="Get analyzer stats for a specific project",
        category="analyzer",
        difficulty=Difficulty.EASY,
        tags=["analyzer_status"],
        turns=[
            Turn(
                user_message="show me analyzer stats for the backend project",
                expected_tools=[
                    ExpectedTool(name="analyzer_status", args={"project_id": "backend"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="analyzer-status-stats",
        description="Ask about suggestion acceptance rates",
        category="analyzer",
        difficulty=Difficulty.EASY,
        tags=["analyzer_status"],
        turns=[
            Turn(
                user_message="how many analyzer suggestions have been accepted?",
                expected_tools=[ExpectedTool(name="analyzer_status")],
            ),
        ],
    ),
    # --- analyzer_toggle ---
    TestCase(
        id="analyzer-toggle-enable",
        description="Enable the chat analyzer",
        category="analyzer",
        difficulty=Difficulty.TRIVIAL,
        tags=["analyzer_toggle"],
        turns=[
            Turn(
                user_message="turn on the chat analyzer",
                expected_tools=[
                    ExpectedTool(name="analyzer_toggle", args={"enabled": True}),
                ],
            ),
        ],
    ),
    TestCase(
        id="analyzer-toggle-disable",
        description="Disable the chat analyzer",
        category="analyzer",
        difficulty=Difficulty.TRIVIAL,
        tags=["analyzer_toggle"],
        turns=[
            Turn(
                user_message="disable the analyzer",
                expected_tools=[
                    ExpectedTool(name="analyzer_toggle", args={"enabled": False}),
                ],
            ),
        ],
    ),
    TestCase(
        id="analyzer-toggle-natural",
        description="Toggle the analyzer with casual phrasing",
        category="analyzer",
        difficulty=Difficulty.EASY,
        tags=["analyzer_toggle", "natural-language"],
        turns=[
            Turn(
                user_message="stop the analyzer for now",
                expected_tools=[
                    ExpectedTool(name="analyzer_toggle", args={"enabled": False}),
                ],
            ),
        ],
    ),
    # --- analyzer_history ---
    TestCase(
        id="analyzer-history-simple",
        description="View recent analyzer suggestions",
        category="analyzer",
        difficulty=Difficulty.TRIVIAL,
        tags=["analyzer_history"],
        turns=[
            Turn(
                user_message="show me recent analyzer suggestions",
                expected_tools=[ExpectedTool(name="analyzer_history")],
            ),
        ],
    ),
    TestCase(
        id="analyzer-history-with-project",
        description="View analyzer suggestions for a specific project",
        category="analyzer",
        difficulty=Difficulty.EASY,
        tags=["analyzer_history"],
        turns=[
            Turn(
                user_message="what has the analyzer suggested for frontend?",
                expected_tools=[
                    ExpectedTool(name="analyzer_history", args={"project_id": "frontend"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="analyzer-history-with-limit",
        description="View limited number of suggestions",
        category="analyzer",
        difficulty=Difficulty.EASY,
        tags=["analyzer_history"],
        turns=[
            Turn(
                user_message="show me the last 5 analyzer suggestions",
                expected_tools=[
                    ExpectedTool(name="analyzer_history", args={"limit": 5}),
                ],
            ),
        ],
    ),
    TestCase(
        id="analyzer-history-natural",
        description="Ask about analyzer suggestions naturally",
        category="analyzer",
        difficulty=Difficulty.EASY,
        tags=["analyzer_history", "natural-language"],
        turns=[
            Turn(
                user_message="what has the analyzer been suggesting lately?",
                expected_tools=[ExpectedTool(name="analyzer_history")],
            ),
        ],
    ),
]
