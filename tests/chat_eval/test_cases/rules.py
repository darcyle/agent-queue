"""Test cases for rule and tool-discovery tools: browse_rules, load_rule,
save_rule, delete_rule, browse_tools, load_tools, process_task_completion,
send_message.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- browse_rules ---
    TestCase(
        id="rules-browse-all",
        description="Browse all rules for a project",
        category="rules",
        difficulty=Difficulty.TRIVIAL,
        tags=["browse_rules"],
        active_project="proj-1",
        turns=[
            Turn(
                user_message="show me all the rules",
                expected_tools=[ExpectedTool(name="browse_rules")],
            ),
        ],
    ),
    TestCase(
        id="rules-browse-project",
        description="Browse rules for a specific project",
        category="rules",
        difficulty=Difficulty.EASY,
        tags=["browse_rules"],
        turns=[
            Turn(
                user_message="list rules for project proj-1",
                expected_tools=[
                    ExpectedTool(name="browse_rules", args={"project_id": "proj-1"}),
                ],
            ),
        ],
    ),

    # --- load_rule ---
    TestCase(
        id="rules-load-by-id",
        description="Load a specific rule by ID",
        category="rules",
        difficulty=Difficulty.EASY,
        tags=["load_rule"],
        turns=[
            Turn(
                user_message="show me the details of rule rule-style-guide",
                expected_tools=[
                    ExpectedTool(name="load_rule", args={"id": "rule-style-guide"}),
                ],
            ),
        ],
    ),

    # --- save_rule ---
    TestCase(
        id="rules-save-new",
        description="Save a new passive rule",
        category="rules",
        difficulty=Difficulty.MEDIUM,
        tags=["save_rule"],
        active_project="proj-1",
        turns=[
            Turn(
                user_message="create a rule to enforce code style using black formatter",
                expected_tools=[ExpectedTool(name="save_rule")],
            ),
        ],
    ),

    # --- delete_rule ---
    TestCase(
        id="rules-delete-by-id",
        description="Delete a rule by its ID",
        category="rules",
        difficulty=Difficulty.EASY,
        tags=["delete_rule"],
        turns=[
            Turn(
                user_message="delete rule rule-old-lint",
                expected_tools=[
                    ExpectedTool(name="delete_rule", args={"id": "rule-old-lint"}),
                ],
            ),
        ],
    ),

    # --- browse_tools ---
    TestCase(
        id="tools-browse-categories",
        description="Browse available tool categories",
        category="rules",
        difficulty=Difficulty.TRIVIAL,
        tags=["browse_tools"],
        turns=[
            Turn(
                user_message="what tools do you have available?",
                expected_tools=[ExpectedTool(name="browse_tools")],
            ),
        ],
    ),

    # --- load_tools ---
    TestCase(
        id="tools-load-category",
        description="Load a specific tool category",
        category="rules",
        difficulty=Difficulty.EASY,
        tags=["load_tools"],
        turns=[
            Turn(
                user_message="I need to work with rules, load the rules tools",
                expected_tools=[
                    ExpectedTool(name="load_tools", args={"category": "rules"}),
                ],
            ),
        ],
    ),

    # --- process_task_completion ---
    TestCase(
        id="task-process-completion",
        description="Process task completion to check for plans",
        category="rules",
        difficulty=Difficulty.MEDIUM,
        tags=["process_task_completion"],
        turns=[
            Turn(
                user_message="process the completion of task t-123 in workspace /tmp/ws",
                expected_tools=[
                    ExpectedTool(
                        name="process_task_completion",
                        args={"task_id": "t-123", "workspace_path": "/tmp/ws"},
                    ),
                ],
            ),
        ],
    ),

    # --- send_message ---
    TestCase(
        id="send-message-to-thread",
        description="Send a message to a Discord thread",
        category="rules",
        difficulty=Difficulty.EASY,
        tags=["send_message"],
        turns=[
            Turn(
                user_message="send a message to the task thread saying 'build passed'",
                expected_tools=[ExpectedTool(name="send_message")],
            ),
        ],
    ),
]
