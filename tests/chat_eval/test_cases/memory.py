"""Test cases for memory tools: memory_search, memory_stats, memory_reindex."""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- memory_search ---
    TestCase(
        id="memory-search-basic",
        description="Search project memory with explicit project and query",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_search"],
        turns=[
            Turn(
                user_message="search memory in test-project for 'authentication'",
                expected_tools=[
                    ExpectedTool(
                        name="memory_search",
                        args={"project_id": "test-project", "query": "authentication"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-search-natural",
        description="Search memory with natural phrasing",
        category="memory",
        difficulty=Difficulty.MEDIUM,
        tags=["memory_search"],
        turns=[
            Turn(
                user_message="what do we know about the database migration in project backend?",
                expected_tools=[
                    ExpectedTool(
                        name="memory_search",
                        args={"project_id": "backend"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-search-active-project",
        description="Search memory using active project context",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_search"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="search memory for 'API rate limiting'",
                expected_tools=[
                    ExpectedTool(
                        name="memory_search",
                        args={"project_id": "my-app", "query": "API rate limiting"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-search-with-top-k",
        description="Search memory specifying number of results",
        category="memory",
        difficulty=Difficulty.MEDIUM,
        tags=["memory_search"],
        turns=[
            Turn(
                user_message="find the top 3 memories about testing in project webapp",
                expected_tools=[
                    ExpectedTool(
                        name="memory_search",
                        args={"project_id": "webapp", "top_k": 3},
                    ),
                ],
            ),
        ],
    ),
    # --- memory_stats ---
    TestCase(
        id="memory-stats-basic",
        description="Show memory stats for a project",
        category="memory",
        difficulty=Difficulty.TRIVIAL,
        tags=["memory_stats"],
        turns=[
            Turn(
                user_message="show memory stats for test-project",
                expected_tools=[
                    ExpectedTool(
                        name="memory_stats",
                        args={"project_id": "test-project"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-stats-natural",
        description="Ask about memory status naturally",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_stats"],
        active_project="backend",
        turns=[
            Turn(
                user_message="how's the memory index looking?",
                expected_tools=[
                    ExpectedTool(
                        name="memory_stats",
                        args={"project_id": "backend"},
                    ),
                ],
            ),
        ],
    ),
    # --- memory_reindex ---
    TestCase(
        id="memory-reindex-basic",
        description="Force reindex of project memory",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["memory_reindex"],
        turns=[
            Turn(
                user_message="reindex memory for project my-app",
                expected_tools=[
                    ExpectedTool(
                        name="memory_reindex",
                        args={"project_id": "my-app"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-reindex-natural",
        description="Rebuild memory index with natural phrasing",
        category="memory",
        difficulty=Difficulty.MEDIUM,
        tags=["memory_reindex"],
        active_project="webapp",
        turns=[
            Turn(
                user_message="rebuild the memory index, it seems out of date",
                expected_tools=[
                    ExpectedTool(
                        name="memory_reindex",
                        args={"project_id": "webapp"},
                    ),
                ],
            ),
        ],
    ),
]
