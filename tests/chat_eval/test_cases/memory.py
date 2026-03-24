"""Test cases for memory tools (supervisor evaluation).

Covers: memory_search, memory_stats, memory_reindex, compact_memory.

11 test cases: verified against current supervisor-based architecture.
memory_search is a core tool (always loaded); other memory tools are on-demand.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

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
    # --- compact_memory ---
    TestCase(
        id="memory-compact-basic",
        description="Compact memory for a specific project",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["compact_memory"],
        turns=[
            Turn(
                user_message="compact memory for project backend",
                expected_tools=[
                    ExpectedTool(
                        name="compact_memory",
                        args={"project_id": "backend"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="memory-compact-active-project",
        description="Compact memory using active project context",
        category="memory",
        difficulty=Difficulty.EASY,
        tags=["compact_memory"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="compact the memory, it's getting large",
                expected_tools=[
                    ExpectedTool(
                        name="compact_memory",
                        args={"project_id": "my-app"},
                    ),
                ],
            ),
        ],
    ),
    # --- multi-turn memory usage ---
    TestCase(
        id="memory-search-then-stats",
        description="Search memory then check stats in a multi-turn conversation",
        category="memory",
        difficulty=Difficulty.MEDIUM,
        tags=["memory_search", "memory_stats"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="search memory for 'deployment pipeline'",
                expected_tools=[
                    ExpectedTool(
                        name="memory_search",
                        args={"project_id": "my-app", "query": "deployment pipeline"},
                    ),
                ],
            ),
            Turn(
                user_message="how many memories are indexed?",
                expected_tools=[
                    ExpectedTool(
                        name="memory_stats",
                        args={"project_id": "my-app"},
                    ),
                ],
            ),
        ],
    ),
]
