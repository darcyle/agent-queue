"""Test cases for hook tools (supervisor evaluation).

Covers: create_hook, list_hooks, edit_hook, delete_hook, list_hook_runs, fire_hook.

20 test cases: verified against current supervisor-based architecture.
Hooks are automation triggers (periodic or event-driven) managed through the Supervisor.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- list_hooks ---
    TestCase(
        id="hooks-list-all",
        description="List all hooks across all projects",
        category="hooks",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_hooks"],
        turns=[
            Turn(
                user_message="list all hooks",
                expected_tools=[ExpectedTool(name="list_hooks")],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-by-project",
        description="List hooks filtered by project",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hooks"],
        turns=[
            Turn(
                user_message="show hooks for project p-1",
                expected_tools=[
                    ExpectedTool(name="list_hooks", args={"project_id": "p-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-natural",
        description="List hooks with natural phrasing",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hooks", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what hooks are set up?",
                expected_tools=[ExpectedTool(name="list_hooks")],
            ),
        ],
    ),
    # --- create_hook ---
    TestCase(
        id="hooks-create-periodic",
        description="Create a periodic hook with basic configuration",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["create_hook", "periodic"],
        turns=[
            Turn(
                user_message=(
                    "create a hook for project p-1 named 'deploy-check' that runs every "
                    "30 minutes with the prompt 'Check if deployment is healthy'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_hook",
                        args={
                            "project_id": "p-1",
                            "name": "deploy-check",
                            "prompt_template": "Check if deployment is healthy",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-create-event",
        description="Create an event-triggered hook",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["create_hook", "event"],
        turns=[
            Turn(
                user_message=(
                    "create a hook for project p-2 called 'on-task-complete' that triggers "
                    "on task completion events with prompt 'Review the completed task results'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_hook",
                        args={
                            "project_id": "p-2",
                            "name": "on-task-complete",
                            "prompt_template": "Review the completed task results",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-create-with-context-steps",
        description="Create a hook with context steps and custom cooldown",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["create_hook", "periodic", "context-steps"],
        turns=[
            Turn(
                user_message=(
                    "set up a periodic hook for project p-1 named 'health-monitor' that "
                    "runs a shell command 'curl http://localhost:8080/health' and uses the "
                    "prompt 'Analyze health check result: {{step_0}}' with a 15-minute cooldown"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_hook",
                        args={
                            "project_id": "p-1",
                            "name": "health-monitor",
                            "prompt_template": "Analyze health check result: {{step_0}}",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-create-natural",
        description="Create a hook with casual natural language",
        category="hooks",
        difficulty=Difficulty.HARD,
        tags=["create_hook", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "I want a hook called 'test-runner' that periodically runs and checks "
                    "if all tests pass with the prompt 'Run test suite and report failures'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="create_hook",
                        args={
                            "name": "test-runner",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- edit_hook ---
    TestCase(
        id="hooks-edit-prompt",
        description="Edit a hook to change its prompt template",
        category="hooks",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_hook"],
        turns=[
            Turn(
                user_message=(
                    "edit hook h-1 and change the prompt to 'Check service health and "
                    "create a task if down'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="edit_hook",
                        args={
                            "hook_id": "h-1",
                            "prompt_template": (
                                "Check service health and create a task if down"
                            ),
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-edit-disable",
        description="Disable a hook",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["edit_hook"],
        turns=[
            Turn(
                user_message="disable hook h-2",
                expected_tools=[
                    ExpectedTool(
                        name="edit_hook",
                        args={"hook_id": "h-2", "enabled": False},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-edit-enable",
        description="Re-enable a hook",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["edit_hook"],
        turns=[
            Turn(
                user_message="enable hook h-2",
                expected_tools=[
                    ExpectedTool(
                        name="edit_hook",
                        args={"hook_id": "h-2", "enabled": True},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-edit-cooldown",
        description="Change a hook's cooldown period",
        category="hooks",
        difficulty=Difficulty.MEDIUM,
        tags=["edit_hook"],
        turns=[
            Turn(
                user_message="set the cooldown for hook h-1 to 10 minutes",
                expected_tools=[
                    ExpectedTool(
                        name="edit_hook",
                        args={"hook_id": "h-1", "cooldown_seconds": 600},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-edit-rename",
        description="Rename a hook",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["edit_hook"],
        turns=[
            Turn(
                user_message="rename hook h-3 to 'nightly-check'",
                expected_tools=[
                    ExpectedTool(
                        name="edit_hook",
                        args={"hook_id": "h-3", "name": "nightly-check"},
                    ),
                ],
            ),
        ],
    ),
    # --- delete_hook ---
    TestCase(
        id="hooks-delete-explicit",
        description="Delete a hook by ID",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["delete_hook"],
        turns=[
            Turn(
                user_message="delete hook h-1",
                expected_tools=[
                    ExpectedTool(name="delete_hook", args={"hook_id": "h-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-delete-natural",
        description="Remove a hook using natural phrasing",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["delete_hook", "natural-language"],
        turns=[
            Turn(
                user_message="remove hook h-4",
                expected_tools=[
                    ExpectedTool(name="delete_hook", args={"hook_id": "h-4"}),
                ],
            ),
        ],
    ),
    # --- list_hook_runs ---
    TestCase(
        id="hooks-list-runs",
        description="Show run history for a hook",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hook_runs"],
        turns=[
            Turn(
                user_message="show runs for hook h-1",
                expected_tools=[
                    ExpectedTool(name="list_hook_runs", args={"hook_id": "h-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-runs-natural",
        description="Ask about hook execution history with natural language",
        category="hooks",
        difficulty=Difficulty.MEDIUM,
        tags=["list_hook_runs", "natural-language"],
        turns=[
            Turn(
                user_message="how many times has hook h-2 run recently?",
                expected_tools=[
                    ExpectedTool(name="list_hook_runs", args={"hook_id": "h-2"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-list-runs-with-limit",
        description="Show limited number of hook runs",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["list_hook_runs"],
        turns=[
            Turn(
                user_message="show the last 5 runs for hook h-1",
                expected_tools=[
                    ExpectedTool(
                        name="list_hook_runs",
                        args={"hook_id": "h-1", "limit": 5},
                    ),
                ],
            ),
        ],
    ),
    # --- fire_hook ---
    TestCase(
        id="hooks-fire-explicit",
        description="Manually trigger a hook by ID",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["fire_hook"],
        turns=[
            Turn(
                user_message="fire hook h-1 now",
                expected_tools=[
                    ExpectedTool(name="fire_hook", args={"hook_id": "h-1"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-fire-trigger-phrasing",
        description="Manually trigger a hook using 'trigger' phrasing",
        category="hooks",
        difficulty=Difficulty.EASY,
        tags=["fire_hook", "natural-language"],
        turns=[
            Turn(
                user_message="trigger hook h-2 manually",
                expected_tools=[
                    ExpectedTool(name="fire_hook", args={"hook_id": "h-2"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="hooks-fire-run-phrasing",
        description="Fire a hook using 'run' phrasing",
        category="hooks",
        difficulty=Difficulty.MEDIUM,
        tags=["fire_hook", "natural-language"],
        turns=[
            Turn(
                user_message="run the deploy-check hook right now",
                expected_tools=[ExpectedTool(name="fire_hook")],
            ),
        ],
    ),
]
