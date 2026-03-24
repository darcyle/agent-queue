"""Test cases for agent profile management tools (supervisor evaluation).

Covers: list_profiles, create_profile, get_profile, edit_profile, delete_profile,
check_profile, install_profile, export_profile, import_profile, view_profile,
regenerate_profile, list_available_tools.

16 test cases: verified against current supervisor-based architecture.
Profiles define agent configurations (model, tools, system prompt).

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- list_profiles ---
    TestCase(
        id="profile-list-all",
        description="List all agent profiles",
        category="profiles",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_profiles"],
        turns=[
            Turn(
                user_message="list all profiles",
                expected_tools=[ExpectedTool(name="list_profiles")],
            ),
        ],
    ),
    TestCase(
        id="profile-list-what-profiles",
        description="Ask what profiles are available",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["list_profiles"],
        turns=[
            Turn(
                user_message="what agent profiles do we have?",
                expected_tools=[ExpectedTool(name="list_profiles")],
            ),
        ],
    ),
    # --- create_profile ---
    TestCase(
        id="profile-create-basic",
        description="Create a new agent profile with a name",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["create_profile"],
        turns=[
            Turn(
                user_message="create a profile called 'code-reviewer' with name 'Code Reviewer'",
                expected_tools=[
                    ExpectedTool(
                        name="create_profile",
                        args={"profile_id": "code-reviewer", "name": "Code Reviewer"},
                    ),
                ],
            ),
        ],
    ),
    # --- get_profile ---
    TestCase(
        id="profile-get-details",
        description="Get details of a specific profile",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["get_profile"],
        turns=[
            Turn(
                user_message="show me the code-reviewer profile",
                expected_tools=[
                    ExpectedTool(name="get_profile", args={"profile_id": "code-reviewer"}),
                ],
            ),
        ],
    ),
    # --- edit_profile ---
    TestCase(
        id="profile-edit-model",
        description="Edit a profile to change its model",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["edit_profile"],
        turns=[
            Turn(
                user_message="update the code-reviewer profile to use claude-opus-4-20250514",
                expected_tools=[
                    ExpectedTool(
                        name="edit_profile",
                        args={"profile_id": "code-reviewer", "model": "claude-opus-4-20250514"},
                    ),
                ],
            ),
        ],
    ),
    # --- delete_profile ---
    TestCase(
        id="profile-delete",
        description="Delete an agent profile",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["delete_profile"],
        turns=[
            Turn(
                user_message="delete the code-reviewer profile",
                expected_tools=[
                    ExpectedTool(name="delete_profile", args={"profile_id": "code-reviewer"}),
                ],
            ),
        ],
    ),
    # --- check_profile ---
    TestCase(
        id="profile-check-valid",
        description="Validate a profile configuration",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["check_profile"],
        turns=[
            Turn(
                user_message="check if the reviewer profile is valid",
                expected_tools=[
                    ExpectedTool(name="check_profile", args={"profile_id": "reviewer"}),
                ],
            ),
        ],
    ),
    # --- install_profile ---
    TestCase(
        id="profile-install",
        description="Install a profile from a URL or path",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["install_profile"],
        turns=[
            Turn(
                user_message="install the profile from https://example.com/profiles/reviewer.json",
                expected_tools=[
                    ExpectedTool(
                        name="install_profile",
                        args={"source": "https://example.com/profiles/reviewer.json"},
                    ),
                ],
            ),
        ],
    ),
    # --- export_profile ---
    TestCase(
        id="profile-export",
        description="Export a profile to a file",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["export_profile"],
        turns=[
            Turn(
                user_message="export the code-reviewer profile",
                expected_tools=[
                    ExpectedTool(name="export_profile", args={"profile_id": "code-reviewer"}),
                ],
            ),
        ],
    ),
    # --- import_profile ---
    TestCase(
        id="profile-import",
        description="Import a profile from a file",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["import_profile"],
        turns=[
            Turn(
                user_message="import profile from /path/to/profile.json",
                expected_tools=[
                    ExpectedTool(
                        name="import_profile",
                        args={"source": "/path/to/profile.json"},
                    ),
                ],
            ),
        ],
    ),
    # --- view_profile ---
    TestCase(
        id="profile-view-basic",
        description="View a project profile",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["view_profile"],
        turns=[
            Turn(
                user_message="show me the profile for project backend",
                expected_tools=[
                    ExpectedTool(name="view_profile", args={"project_id": "backend"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="profile-view-active-project",
        description="View profile using active project context",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["view_profile"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="view the project profile",
                expected_tools=[
                    ExpectedTool(name="view_profile", args={"project_id": "my-app"}),
                ],
            ),
        ],
    ),
    # --- regenerate_profile ---
    TestCase(
        id="profile-regenerate-basic",
        description="Regenerate a project profile from task history",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["regenerate_profile"],
        turns=[
            Turn(
                user_message="regenerate the profile for project backend",
                expected_tools=[
                    ExpectedTool(
                        name="regenerate_profile",
                        args={"project_id": "backend"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="profile-regenerate-active-project",
        description="Regenerate profile using active project context",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["regenerate_profile"],
        active_project="my-app",
        turns=[
            Turn(
                user_message="regenerate the project profile from scratch",
                expected_tools=[
                    ExpectedTool(
                        name="regenerate_profile",
                        args={"project_id": "my-app"},
                    ),
                ],
            ),
        ],
    ),
    # --- list_available_tools ---
    TestCase(
        id="profile-list-tools",
        description="List all tools available for profiles",
        category="profiles",
        difficulty=Difficulty.EASY,
        tags=["list_available_tools"],
        turns=[
            Turn(
                user_message="what tools can I assign to a profile?",
                expected_tools=[ExpectedTool(name="list_available_tools")],
            ),
        ],
    ),
    TestCase(
        id="profile-list-tools-variant",
        description="List available tools using direct phrasing",
        category="profiles",
        difficulty=Difficulty.TRIVIAL,
        tags=["list_available_tools"],
        turns=[
            Turn(
                user_message="list available tools",
                expected_tools=[ExpectedTool(name="list_available_tools")],
            ),
        ],
    ),
]
