# Plan: Add Core Filesystem Tools to Supervisor Agent

## Background & Motivation

The supervisor agent currently has limited filesystem capabilities — a basic `read_file` (200-line max, no offset), a combined `search_files` (grep/find in one tool), `run_command`, and `list_directory`. These are all buried in the "system" tool category, and the supervisor's system prompt explicitly tells it "You CANNOT write code, edit files, run commands, or do technical work yourself."

The goal is to give the supervisor Claude Code–level file tools so it can investigate codebases, make small edits, and answer questions without spinning up a full Claude Code task. This means:

1. **New tools**: `write_file`, `edit_file` (targeted string replacement), `glob_files` (pattern matching), `grep` (dedicated ripgrep-style search with context lines, regex, etc.)
2. **Enhanced existing tools**: Upgrade `read_file` with offset/limit support, increase max lines
3. **New tool category**: Create a `"files"` category to group all filesystem tools together
4. **System prompt update**: Remove the "you cannot edit files" restriction and add guidance for when to use file tools vs. creating a task

## Current State

### Existing tools (in "system" category):
- `read_file` — reads up to 200 lines from start, no offset. Tool def in registry, handler at `_cmd_read_file`
- `run_command` — shell execution with 120s max timeout. Tool def in registry, handler at `_cmd_run_command`
- `search_files` — combined grep/find with basic options (50-match limit, no context lines). Tool def in registry, handler at `_cmd_search_files`
- `list_directory` — lists files in a workspace directory. Handler exists at `_cmd_list_directory`, but **no tool definition in registry** (orphaned)

### Existing but unregistered:
- `write_file` — handler exists at `_cmd_write_file` (writes full file content). **No tool definition in registry**
- `_tool_label()` in supervisor.py already has entries for both `read_file` and `write_file`

### Security model:
- All file operations go through `_validate_path()` which restricts to workspace_dir, registered repo source_paths, and registered workspace paths
- Shell commands use `_run_subprocess_shell()` with configurable timeout (max 120s)

---

## Phase 1: Create "files" tool category and register new/missing tools in `tool_registry.py`

### Changes to `src/tool_registry.py`:

1. **Add `"files"` category** to `CATEGORIES` dict:
   ```python
   "files": CategoryMeta(
       name="files",
       description="Read, write, edit, search, and browse files in project workspaces",
   ),
   ```

2. **Move existing tools** from "system" to "files" category in `_TOOL_CATEGORIES`:
   - `read_file` → `"files"`
   - `run_command` → `"files"`
   - `search_files` → `"files"`

3. **Add new tool entries** to `_TOOL_CATEGORIES`:
   - `write_file` → `"files"`
   - `edit_file` → `"files"`
   - `glob_files` → `"files"`
   - `grep` → `"files"`
   - `list_directory` → `"files"`

4. **Add tool definitions** to `_ALL_TOOL_DEFINITIONS`:

   **`write_file`** — Create or overwrite a file:
   ```python
   {
       "name": "write_file",
       "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Path can be absolute or relative to workspaces root.",
       "input_schema": {
           "type": "object",
           "properties": {
               "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
               "content": {"type": "string", "description": "Full file content to write"},
           },
           "required": ["path", "content"],
       },
   }
   ```

   **`edit_file`** — Targeted string replacement (like Claude Code's Edit tool):
   ```python
   {
       "name": "edit_file",
       "description": "Edit a file by replacing an exact string match. Use read_file first to see current contents. The old_string must match exactly one location in the file. For multiple replacements, set replace_all to true.",
       "input_schema": {
           "type": "object",
           "properties": {
               "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
               "old_string": {"type": "string", "description": "The exact text to find and replace"},
               "new_string": {"type": "string", "description": "The replacement text"},
               "replace_all": {
                   "type": "boolean",
                   "description": "Replace all occurrences (default: false, fails if old_string matches multiple locations)",
                   "default": False,
               },
           },
           "required": ["path", "old_string", "new_string"],
       },
   }
   ```

   **`glob_files`** — Find files by glob pattern:
   ```python
   {
       "name": "glob_files",
       "description": "Find files matching a glob pattern within a directory. Returns matching file paths sorted by modification time. Supports patterns like '**/*.py', 'src/**/*.ts', '*.json'.",
       "input_schema": {
           "type": "object",
           "properties": {
               "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py', 'src/*.ts')"},
               "path": {"type": "string", "description": "Root directory to search in (absolute or relative to workspaces root)"},
               "max_results": {
                   "type": "integer",
                   "description": "Maximum number of results to return (default 100)",
                   "default": 100,
               },
           },
           "required": ["pattern", "path"],
       },
   }
   ```

   **`grep`** — Dedicated content search with ripgrep-style options:
   ```python
   {
       "name": "grep",
       "description": "Search file contents using regex patterns (powered by ripgrep). More powerful than search_files — supports context lines, file type filtering, case-insensitive search, and match count mode.",
       "input_schema": {
           "type": "object",
           "properties": {
               "pattern": {"type": "string", "description": "Regex pattern to search for"},
               "path": {"type": "string", "description": "Directory or file to search (absolute or relative to workspaces root)"},
               "glob": {"type": "string", "description": "File glob filter (e.g. '*.py', '*.{ts,tsx}')"},
               "context_lines": {
                   "type": "integer",
                   "description": "Number of context lines before and after each match (default 0)",
                   "default": 0,
               },
               "case_insensitive": {
                   "type": "boolean",
                   "description": "Case insensitive search (default false)",
                   "default": False,
               },
               "max_results": {
                   "type": "integer",
                   "description": "Maximum number of matching lines to return (default 50)",
                   "default": 50,
               },
               "files_only": {
                   "type": "boolean",
                   "description": "Only return file paths with matches, not matching lines (default false)",
                   "default": False,
               },
           },
           "required": ["pattern", "path"],
       },
   }
   ```

   **`list_directory`** — Already has a handler, needs a tool definition:
   ```python
   {
       "name": "list_directory",
       "description": "List files and directories at a given path within a project workspace. Returns directory names and file names with sizes.",
       "input_schema": {
           "type": "object",
           "properties": {
               "project_id": {"type": "string", "description": "Project ID (uses active project if omitted)"},
               "path": {"type": "string", "description": "Relative path within workspace (default: root)"},
               "workspace": {"type": "string", "description": "Workspace name or ID (uses first workspace if omitted)"},
           },
       },
   }
   ```

5. **Upgrade `read_file` definition** — add `offset` parameter:
   Replace current definition with:
   ```python
   {
       "name": "read_file",
       "description": "Read a file's contents. Path can be absolute or relative to the workspaces root. Use offset and max_lines to read specific sections of large files.",
       "input_schema": {
           "type": "object",
           "properties": {
               "path": {"type": "string", "description": "File path (absolute or relative to workspaces root)"},
               "offset": {
                   "type": "integer",
                   "description": "Line number to start reading from (0-based, default 0)",
                   "default": 0,
               },
               "max_lines": {
                   "type": "integer",
                   "description": "Max lines to return (default 500)",
                   "default": 500,
               },
           },
           "required": ["path"],
       },
   }
   ```

---

## Phase 2: Implement command handlers in `src/command_handler.py`

### New handlers to add:

1. **`_cmd_edit_file`** — String replacement with uniqueness check:
   - Validate path via `_validate_path()`
   - Read file, find `old_string` occurrences
   - If `replace_all=False` and count != 1, return error with count
   - Replace and write back
   - Return `{"path", "replacements_made"}` with a few-line preview around the replacement

2. **`_cmd_glob_files`** — Glob pattern matching:
   - Validate path via `_validate_path()`
   - Use `pathlib.Path.glob()` with recursive support (`**` patterns)
   - Sort results by mtime descending
   - Limit to `max_results`
   - Return relative paths from the search root

3. **`_cmd_grep`** — Ripgrep-powered search:
   - Validate path via `_validate_path()`
   - Build `rg` command with flags: `--glob` for file filtering, `-C N` for context, `-i` for case insensitive, `-l` for files-only, `-m` for max matches
   - Fall back to `grep -rn` if `rg` not available
   - Return structured results with file paths and line numbers
   - Truncate output to 4000 chars

### Enhanced handlers:

4. **Upgrade `_cmd_read_file`** — Add `offset` support:
   - Accept `offset` parameter (default 0)
   - Skip `offset` lines before collecting
   - Increase default `max_lines` from 200 to 500
   - Include total line count in response metadata

---

## Phase 3: Update supervisor system prompt and tool labels

### Changes to `src/prompts/supervisor_system.md`:

1. **Remove** the line: "You are a **dispatcher**, not a worker. You CANNOT write code, edit files, run commands, or do technical work yourself."

2. **Replace** with guidance like:
   ```markdown
   ## Direct Work vs. Task Delegation

   You have filesystem tools (read, write, edit, grep, glob, bash) for direct investigation
   and small changes. Use them when:
   - Investigating a bug or reading code to answer a question
   - Making small, targeted edits (config changes, single-file fixes)
   - Running quick commands (tests, status checks, builds)

   Create a task for an agent when:
   - The work spans multiple files or requires significant reasoning
   - It's a feature, refactor, or multi-step implementation
   - You need Claude Code's full context window and tool suite
   ```

3. **Update** the tool category list to mention `files`:
   ```
   Call `browse_tools` to see available categories (git, project, agent, hooks, memory, system, files)
   ```

### Changes to `src/supervisor.py`:

4. **Add tool labels** for new tools in `_tool_label()`:
   ```python
   elif name == "edit_file":
       detail = input_data.get("path")
   elif name == "glob_files":
       detail = input_data.get("pattern")
   elif name == "grep":
       detail = input_data.get("pattern")
   elif name == "list_directory":
       detail = input_data.get("path") or input_data.get("project_id")
   ```

---

## Phase 4: Tests

### Add test cases:

1. **Tool registry tests** — verify the new "files" category exists and contains the expected tools
2. **Command handler tests** for each new handler:
   - `test_edit_file_single_replacement` — happy path
   - `test_edit_file_ambiguous_match` — error when multiple matches and replace_all=False
   - `test_edit_file_replace_all` — multiple replacements
   - `test_edit_file_no_match` — error when old_string not found
   - `test_glob_files_basic` — find Python files
   - `test_glob_files_recursive` — `**/*.py` pattern
   - `test_glob_files_max_results` — respects limit
   - `test_grep_basic` — content search
   - `test_grep_context_lines` — verify context in output
   - `test_grep_case_insensitive` — verify -i flag
   - `test_grep_files_only` — only returns paths
   - `test_read_file_with_offset` — verify offset skips lines
   - `test_list_directory_tool_exists` — verify tool def + handler work together
3. **Chat eval test cases** — add cases to verify the supervisor uses file tools appropriately
4. **Path validation tests** — ensure all new tools respect `_validate_path()` security boundary

### Existing tests to update:
- `test_all_tools_have_test_cases` — add cases for new tools
- Any tests that assert the exact set of tools in the "system" category (since we're moving 3 out)
