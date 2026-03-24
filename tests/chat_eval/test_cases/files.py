"""Test cases for filesystem tools (supervisor evaluation).

Covers: read_file, write_file, edit_file, glob_files, grep, search_files, list_directory.

8 test cases: verified against current supervisor-based architecture.
File tools are loaded on-demand via the 'files' tool category.

Updated: supervisor refactor review — all tests confirmed relevant; no outdated patterns.
"""

from tests.chat_eval.test_cases._types import TestCase, Turn, ExpectedTool, Difficulty

CASES: list[TestCase] = [
    # --- read_file ---
    TestCase(
        id="files-read-simple",
        description="Read a specific file",
        category="files",
        difficulty=Difficulty.TRIVIAL,
        tags=["read_file"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="read the file src/main.py in the test-project workspace",
                expected_tools=[ExpectedTool(name="read_file")],
            ),
        ],
    ),
    TestCase(
        id="files-read-with-offset",
        description="Read a file starting from a specific line",
        category="files",
        difficulty=Difficulty.EASY,
        tags=["read_file"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="show me lines 50-100 of src/config.py in the test-project workspace",
                expected_tools=[ExpectedTool(name="read_file")],
            ),
        ],
    ),
    # --- write_file ---
    TestCase(
        id="files-write-simple",
        description="Write content to a file",
        category="files",
        difficulty=Difficulty.EASY,
        tags=["write_file"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="create a file at /tmp/test.txt with content 'hello world'",
                expected_tools=[ExpectedTool(name="write_file")],
            ),
        ],
    ),
    # --- edit_file ---
    TestCase(
        id="files-edit-simple",
        description="Edit a file with string replacement",
        category="files",
        difficulty=Difficulty.EASY,
        tags=["edit_file"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="in /tmp/test.py, replace 'def foo' with 'def bar'",
                expected_tools=[ExpectedTool(name="edit_file")],
            ),
        ],
    ),
    # --- glob_files ---
    TestCase(
        id="files-glob-simple",
        description="Find files matching a glob pattern",
        category="files",
        difficulty=Difficulty.EASY,
        tags=["glob_files"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="find all Python files in the test-project workspace",
                expected_tools=[ExpectedTool(name="glob_files")],
            ),
        ],
    ),
    # --- grep ---
    TestCase(
        id="files-grep-simple",
        description="Search for a pattern in files",
        category="files",
        difficulty=Difficulty.EASY,
        tags=["grep"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="search for 'TODO' in the test-project source code",
                expected_tools=[ExpectedTool(name="grep")],
            ),
        ],
    ),
    # --- search_files ---
    TestCase(
        id="files-search-grep-mode",
        description="Search file contents with search_files",
        category="files",
        difficulty=Difficulty.EASY,
        tags=["search_files"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="search for 'import os' in /mnt/d/Dev/test-project",
                expected_tools=[ExpectedTool(name="search_files")],
            ),
        ],
    ),
    # --- list_directory ---
    TestCase(
        id="files-list-directory",
        description="List files in a project directory",
        category="files",
        difficulty=Difficulty.EASY,
        tags=["list_directory"],
        active_project="test-project",
        turns=[
            Turn(
                user_message="list the files in the test-project workspace root directory",
                expected_tools=[ExpectedTool(name="list_directory")],
            ),
        ],
    ),
]
