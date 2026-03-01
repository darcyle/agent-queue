"""Test cases where the user provides incomplete or wrong information.

These verify that the LLM still picks the correct tool even when required arguments
are missing. The LLM should either request clarification or attempt the call with
what it has (allowing the command handler to return an appropriate error).
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- Missing required arguments ---
    TestCase(
        id="error-create-task-no-details",
        description="Create task with no title or description - should still pick create_task",
        turns=[
            Turn(
                user_message="create a task",
                expected_tools=[ExpectedTool(name="create_task")],
                not_expected_tools=["create_project", "delete_task"],
            ),
        ],
        category="error_handling",
        tags=["task", "missing-args", "incomplete"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="error-delete-project-no-id",
        description="Delete project with no project ID specified",
        turns=[
            Turn(
                user_message="delete project",
                # LLM should either ask for clarification or attempt delete_project
                expected_tools=[ExpectedTool(name="delete_project")],
                not_expected_tools=["delete_task", "delete_agent"],
            ),
        ],
        category="error_handling",
        tags=["project", "missing-args", "incomplete"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="error-stop-no-task-id",
        description="Stop command with no task ID - should pick stop_task",
        turns=[
            Turn(
                user_message="stop",
                expected_tools=[ExpectedTool(name="stop_task")],
                not_expected_tools=["delete_task", "delete_project"],
            ),
        ],
        category="error_handling",
        tags=["task", "missing-args", "minimal"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="error-approve-no-task-id",
        description="Approve with no task ID specified",
        turns=[
            Turn(
                user_message="approve",
                expected_tools=[ExpectedTool(name="approve_task")],
                not_expected_tools=["delete_task", "create_task"],
            ),
        ],
        category="error_handling",
        tags=["task", "approval", "missing-args", "minimal"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="error-add-dependency-no-args",
        description="Add dependency with no task IDs specified at all",
        turns=[
            Turn(
                user_message="add dependency",
                expected_tools=[ExpectedTool(name="add_dependency")],
                not_expected_tools=["remove_dependency", "create_task"],
            ),
        ],
        category="error_handling",
        tags=["dependency", "missing-args", "minimal"],
        difficulty=Difficulty.HARD,
    ),
    TestCase(
        id="error-edit-task-no-id",
        description="Edit task with no task ID - should still pick edit_task",
        turns=[
            Turn(
                user_message="edit task",
                expected_tools=[ExpectedTool(name="edit_task")],
                not_expected_tools=["edit_project", "edit_agent", "create_task"],
            ),
        ],
        category="error_handling",
        tags=["task", "missing-args", "minimal"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="error-commit-no-message",
        description="Git commit with no commit message provided",
        turns=[
            Turn(
                user_message="commit",
                expected_tools=[ExpectedTool(name="git_commit")],
                not_expected_tools=["create_task", "git_push"],
            ),
        ],
        category="error_handling",
        tags=["git", "missing-args", "minimal"],
        difficulty=Difficulty.MEDIUM,
    ),

    # --- Partial arguments ---
    TestCase(
        id="error-add-dependency-partial",
        description="Add dependency with only one task ID (missing the upstream)",
        turns=[
            Turn(
                user_message="add a dependency to task t-5",
                expected_tools=[ExpectedTool(name="add_dependency")],
                not_expected_tools=["remove_dependency", "delete_task"],
            ),
        ],
        category="error_handling",
        tags=["dependency", "partial-args"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="error-edit-task-no-changes",
        description="Edit task with an ID but no actual changes specified",
        turns=[
            Turn(
                user_message="edit task t-3",
                expected_tools=[
                    ExpectedTool(name="edit_task", args={"task_id": "t-3"}),
                ],
                not_expected_tools=["delete_task", "get_task"],
            ),
        ],
        category="error_handling",
        tags=["task", "partial-args"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="error-create-project-no-name",
        description="Create project without specifying a name",
        turns=[
            Turn(
                user_message="create a new project",
                expected_tools=[ExpectedTool(name="create_project")],
                not_expected_tools=["create_task", "create_agent"],
            ),
        ],
        category="error_handling",
        tags=["project", "missing-args"],
        difficulty=Difficulty.EASY,
    ),

    # --- Typos and near-misses ---
    TestCase(
        id="error-misspelled-command",
        description="Misspelled 'archive' as 'archvie' - LLM should interpret correctly",
        turns=[
            Turn(
                user_message="archvie completed tasks",
                expected_tools=[ExpectedTool(name="archive_tasks")],
                not_expected_tools=["delete_task", "create_task"],
            ),
        ],
        category="error_handling",
        tags=["archive", "typo", "robustness"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="error-wrong-id-format",
        description="Task ID given in wrong format (number only, no prefix)",
        turns=[
            Turn(
                user_message="show me task 42",
                expected_tools=[ExpectedTool(name="get_task")],
                not_expected_tools=["create_task", "delete_task"],
            ),
        ],
        category="error_handling",
        tags=["task", "wrong-format", "robustness"],
        difficulty=Difficulty.EASY,
    ),

    # --- Ambiguous target type ---
    TestCase(
        id="error-delete-ambiguous-type",
        description="Delete with an ID but no indication of type (task vs project vs agent)",
        turns=[
            Turn(
                user_message="delete t-5",
                # With the t- prefix, should lean toward delete_task
                expected_tools=[ExpectedTool(name="delete_task", args={"task_id": "t-5"})],
                not_expected_tools=["delete_project", "delete_agent"],
            ),
        ],
        category="error_handling",
        tags=["delete", "ambiguous-type"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="error-pause-ambiguous-type",
        description="Pause with an ID but no indication of type (project vs agent)",
        turns=[
            Turn(
                user_message="pause p-1",
                # With p- prefix, should lean toward pause_project
                expected_tools=[ExpectedTool(name="pause_project", args={"project_id": "p-1"})],
                not_expected_tools=["pause_agent"],
            ),
        ],
        category="error_handling",
        tags=["pause", "ambiguous-type"],
        difficulty=Difficulty.MEDIUM,
    ),

    # --- Missing context for scoped operations ---
    TestCase(
        id="error-list-tasks-no-project",
        description="List tasks without specifying project and no active project set",
        turns=[
            Turn(
                user_message="list tasks",
                expected_tools=[ExpectedTool(name="list_tasks")],
                not_expected_tools=["create_task", "delete_task"],
            ),
        ],
        category="error_handling",
        tags=["task", "list", "no-context"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="error-restart-nonexistent",
        description="Restart a task with an ID that likely does not exist",
        turns=[
            Turn(
                user_message="restart task t-99999",
                expected_tools=[
                    ExpectedTool(name="restart_task", args={"task_id": "t-99999"}),
                ],
                not_expected_tools=["create_task", "stop_task"],
            ),
        ],
        category="error_handling",
        tags=["task", "restart", "nonexistent"],
        difficulty=Difficulty.EASY,
    ),
    TestCase(
        id="error-push-no-project",
        description="Git push without specifying which project to push for",
        turns=[
            Turn(
                user_message="push the changes",
                expected_tools=[ExpectedTool(name="git_push")],
                not_expected_tools=["git_commit", "create_task"],
            ),
        ],
        category="error_handling",
        tags=["git", "push", "missing-args"],
        difficulty=Difficulty.MEDIUM,
    ),
    TestCase(
        id="error-remove-dependency-partial",
        description="Remove dependency with only the downstream task specified",
        turns=[
            Turn(
                user_message="remove the dependency from task t-8",
                expected_tools=[ExpectedTool(name="remove_dependency")],
                not_expected_tools=["add_dependency", "delete_task"],
            ),
        ],
        category="error_handling",
        tags=["dependency", "partial-args"],
        difficulty=Difficulty.MEDIUM,
    ),
]
