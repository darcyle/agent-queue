"""Test cases for git tools: get_git_status, git_commit, git_push, git_create_branch,
git_merge, git_create_pr, git_changed_files, git_log, git_diff, checkout_branch.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- get_git_status ---
    TestCase(
        id="git-status-simple",
        description="Check git status with active project",
        category="git",
        difficulty=Difficulty.TRIVIAL,
        tags=["get_git_status"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="check git status",
                expected_tools=[ExpectedTool(name="get_git_status")],
            ),
        ],
    ),
    TestCase(
        id="git-status-explicit-project",
        description="Check git status for a specific project",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["get_git_status"],
        turns=[
            Turn(
                user_message="what's the git status for project p-2?",
                expected_tools=[
                    ExpectedTool(name="get_git_status", args={"project_id": "p-2"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-status-natural",
        description="Natural phrasing asking about repo state",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["get_git_status", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="are there any uncommitted changes?",
                expected_tools=[ExpectedTool(name="get_git_status")],
            ),
        ],
    ),
    # --- git_commit / commit_changes ---
    TestCase(
        id="git-commit-simple",
        description="Commit with a message using git_commit",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_commit", "commit_changes"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="commit with message 'fix login bug'",
                expected_tools=[
                    ExpectedTool(name="git_commit", args={"message": "fix login bug"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-commit-natural",
        description="Commit changes with natural phrasing",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_commit", "commit_changes"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="commit everything with the message 'update readme'",
                expected_tools=[
                    ExpectedTool(name="git_commit", args={"message": "update readme"}),
                ],
                not_expected_tools=["git_push"],
            ),
        ],
    ),
    TestCase(
        id="git-commit-changes-variant",
        description="Commit using casual 'save changes' phrasing",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_commit"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="save all changes as a commit called 'refactor auth module'",
                expected_tools=[
                    ExpectedTool(name="git_commit", args={"message": "refactor auth module"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-commit-for-project",
        description="Commit changes for a specific project",
        category="git",
        difficulty=Difficulty.MEDIUM,
        tags=["git_commit"],
        turns=[
            Turn(
                user_message="commit project p-3's changes with message 'add tests'",
                expected_tools=[
                    ExpectedTool(
                        name="git_commit",
                        args={"project_id": "p-3", "message": "add tests"},
                    ),
                ],
            ),
        ],
    ),
    # --- git_push ---
    TestCase(
        id="git-push-simple",
        description="Push current branch",
        category="git",
        difficulty=Difficulty.TRIVIAL,
        tags=["git_push"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="push changes",
                expected_tools=[ExpectedTool(name="git_push")],
            ),
        ],
    ),
    TestCase(
        id="git-push-branch-name",
        description="Push a specific branch by name",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_push"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="push the feature/auth branch",
                expected_tools=[
                    ExpectedTool(name="git_push", args={"branch": "feature/auth"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-push-branch-variant",
        description="Push a named branch to origin",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_push"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="push branch develop to origin",
                expected_tools=[
                    ExpectedTool(
                        name="git_push",
                        args={"branch": "develop"},
                    ),
                ],
            ),
        ],
    ),
    # --- git_pull ---
    TestCase(
        id="git-pull-simple",
        description="Pull latest changes from remote",
        category="git",
        difficulty=Difficulty.TRIVIAL,
        tags=["git_pull"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="pull the latest changes",
                expected_tools=[ExpectedTool(name="git_pull")],
            ),
        ],
    ),
    TestCase(
        id="git-pull-project",
        description="Pull changes for a specific project",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_pull"],
        turns=[
            Turn(
                user_message="pull updates for project p-2",
                expected_tools=[
                    ExpectedTool(name="git_pull", args={"project_id": "p-2"}),
                ],
            ),
        ],
    ),
    # --- git_create_branch / create_branch ---
    TestCase(
        id="git-create-branch-simple",
        description="Create a new branch",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_create_branch", "create_branch"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="create branch feature/auth",
                expected_tools=[
                    ExpectedTool(
                        name="git_create_branch",
                        args={"branch_name": "feature/auth"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-create-branch-natural",
        description="Create a branch with natural phrasing",
        category="git",
        difficulty=Difficulty.MEDIUM,
        tags=["git_create_branch", "create_branch", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="I need a new branch called hotfix/memory-leak",
                expected_tools=[
                    ExpectedTool(
                        name="git_create_branch",
                        args={"branch_name": "hotfix/memory-leak"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-create-branch-variant",
        description="Create branch with 'make a new branch' phrasing",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_create_branch"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="make a new branch named release/v2.0",
                expected_tools=[
                    ExpectedTool(
                        name="git_create_branch",
                        args={"branch_name": "release/v2.0"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-create-branch-for-project",
        description="Create a branch in a specific project",
        category="git",
        difficulty=Difficulty.MEDIUM,
        tags=["git_create_branch"],
        turns=[
            Turn(
                user_message="create a branch called feature/payments in project p-2",
                expected_tools=[
                    ExpectedTool(
                        name="git_create_branch",
                        args={"project_id": "p-2", "branch_name": "feature/payments"},
                    ),
                ],
            ),
        ],
    ),
    # --- checkout_branch ---
    TestCase(
        id="git-checkout-branch",
        description="Switch to an existing branch",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["checkout_branch"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="switch to branch develop",
                expected_tools=[
                    ExpectedTool(
                        name="checkout_branch",
                        args={"branch_name": "develop"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-checkout-branch-natural",
        description="Checkout branch using natural language",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["checkout_branch", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="go to the main branch",
                expected_tools=[
                    ExpectedTool(
                        name="checkout_branch",
                        args={"branch_name": "main"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-checkout-branch-explicit",
        description="Checkout using explicit git checkout phrasing",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["checkout_branch"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="checkout feature/notifications",
                expected_tools=[
                    ExpectedTool(
                        name="checkout_branch",
                        args={"branch_name": "feature/notifications"},
                    ),
                ],
            ),
        ],
    ),
    # --- git_merge / merge_branch ---
    TestCase(
        id="git-merge-simple",
        description="Merge a branch into default",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_merge", "merge_branch"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="merge feature/auth into main",
                expected_tools=[
                    ExpectedTool(
                        name="git_merge",
                        args={"branch_name": "feature/auth"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-merge-natural",
        description="Merge using natural phrasing",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_merge", "merge_branch", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="merge main",
                expected_tools=[
                    ExpectedTool(name="git_merge", args={"branch_name": "main"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-merge-branch-variant",
        description="Merge with casual 'merge back' phrasing",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_merge"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="merge the develop branch back",
                expected_tools=[
                    ExpectedTool(
                        name="git_merge",
                        args={"branch_name": "develop"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-merge-with-target",
        description="Merge specifying both source and target branch",
        category="git",
        difficulty=Difficulty.MEDIUM,
        tags=["git_merge"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="merge feature/checkout into release/v2",
                expected_tools=[
                    ExpectedTool(
                        name="git_merge",
                        args={
                            "branch_name": "feature/checkout",
                            "default_branch": "release/v2",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- git_create_pr ---
    TestCase(
        id="git-create-pr-simple",
        description="Create a pull request with a title",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_create_pr"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="create a PR titled 'Add auth'",
                expected_tools=[
                    ExpectedTool(
                        name="git_create_pr",
                        args={"title": "Add auth"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-create-pr-with-body",
        description="Create a PR with title and description",
        category="git",
        difficulty=Difficulty.MEDIUM,
        tags=["git_create_pr"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "open a pull request titled 'Fix rate limiter' with the description "
                    "'Fixes the token bucket overflow issue'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="git_create_pr",
                        args={
                            "title": "Fix rate limiter",
                            "body": "Fixes the token bucket overflow issue",
                        },
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-create-pr-with-branches",
        description="Create a PR specifying head and base branches",
        category="git",
        difficulty=Difficulty.HARD,
        tags=["git_create_pr"],
        active_project="p-1",
        turns=[
            Turn(
                user_message=(
                    "create a PR from feature/payments to main titled 'Payment integration'"
                ),
                expected_tools=[
                    ExpectedTool(
                        name="git_create_pr",
                        args={
                            "title": "Payment integration",
                            "branch": "feature/payments",
                            "base": "main",
                        },
                    ),
                ],
            ),
        ],
    ),
    # --- git_changed_files ---
    TestCase(
        id="git-changed-files-simple",
        description="List changed files",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_changed_files"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what files changed?",
                expected_tools=[ExpectedTool(name="git_changed_files")],
            ),
        ],
    ),
    TestCase(
        id="git-changed-files-vs-branch",
        description="List files changed compared to a specific branch",
        category="git",
        difficulty=Difficulty.MEDIUM,
        tags=["git_changed_files"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="which files changed compared to develop?",
                expected_tools=[
                    ExpectedTool(
                        name="git_changed_files",
                        args={"base_branch": "develop"},
                    ),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-changed-files-natural",
        description="Ask about modified files - git_changed_files or get_git_status both valid",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_changed_files", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show me the files that were modified",
                # get_git_status also shows modified files, so both are valid
                not_expected_tools=["create_task", "delete_project", "git_commit"],
            ),
        ],
    ),
    # --- git_log ---
    TestCase(
        id="git-log-simple",
        description="Show recent git log",
        category="git",
        difficulty=Difficulty.TRIVIAL,
        tags=["git_log"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show the git log",
                expected_tools=[ExpectedTool(name="git_log")],
            ),
        ],
    ),
    TestCase(
        id="git-log-with-count",
        description="Show git log with specific number of commits",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_log"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show the last 5 commits",
                expected_tools=[
                    ExpectedTool(name="git_log", args={"count": 5}),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-log-natural",
        description="View commit history with natural phrasing",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_log", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what's the commit history?",
                expected_tools=[ExpectedTool(name="git_log")],
            ),
        ],
    ),
    # --- git_diff ---
    TestCase(
        id="git-diff-simple",
        description="Show working tree diff",
        category="git",
        difficulty=Difficulty.TRIVIAL,
        tags=["git_diff"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show diff",
                expected_tools=[ExpectedTool(name="git_diff")],
            ),
        ],
    ),
    TestCase(
        id="git-diff-against-branch",
        description="Show diff against a specific branch",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_diff"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="show the diff against main",
                expected_tools=[
                    ExpectedTool(name="git_diff", args={"base_branch": "main"}),
                ],
            ),
        ],
    ),
    TestCase(
        id="git-diff-what-changed",
        description="Natural phrasing asking for code changes - diff or status both valid",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_diff", "natural-language"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="what code changes are pending?",
                # git_diff or get_git_status both answer this question
                not_expected_tools=["create_task", "delete_task", "git_commit"],
            ),
        ],
    ),
    # --- commit + push combo ---
    TestCase(
        id="git-commit-and-push",
        description="Commit and push in a single request",
        category="git",
        difficulty=Difficulty.MEDIUM,
        tags=["git_commit", "git_push", "multi-tool"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="commit with message 'deploy fix' and push",
                expected_tools=[
                    ExpectedTool(name="git_commit", args={"message": "deploy fix"}),
                    ExpectedTool(name="git_push"),
                ],
                ordered=True,
            ),
        ],
    ),
    # --- create branch + checkout combo ---
    TestCase(
        id="git-create-and-switch",
        description="Create a branch and switch to it (git_create_branch does both)",
        category="git",
        difficulty=Difficulty.EASY,
        tags=["git_create_branch"],
        active_project="p-1",
        turns=[
            Turn(
                user_message="create and switch to a new branch called feature/search",
                expected_tools=[
                    ExpectedTool(
                        name="git_create_branch",
                        args={"branch_name": "feature/search"},
                    ),
                ],
            ),
        ],
    ),
]
