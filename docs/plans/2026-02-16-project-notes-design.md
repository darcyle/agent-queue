# Project Notes Design

## Overview

Add a notes system to agent-queue that lets users build up project knowledge as named markdown documents through the Discord control channel. Notes are `.md` files stored in each project's workspace directory. Agents can write notes directly during tasks, and the bot's LLM can read, edit, and create notes through natural conversation.

## Storage

Notes are files on disk in a `notes/` subdirectory of each project's workspace:

```
~/agent-queue-workspaces/my-project/
├── notes/
│   ├── architecture.md
│   ├── api-design.md
│   └── brainstorm-auth.md
├── repos/
│   └── ...
```

- Filenames are slugified from the note title (e.g. "API Design" → `api-design.md`)
- The `notes/` directory is created on first write
- No database table needed — notes are just files

## LLM Tools

Three new tools in `src/discord/bot.py`:

### `list_notes`
- **Input:** `project_id` (required)
- **Behavior:** Reads the `notes/` directory in the project's workspace. Returns list of `{name, title, size_bytes, modified}` for each `.md` file. Title derived from first `# heading` in the file or reverse-slugified filename.
- **Returns:** `{project_id, notes: [...]}`

### `write_note`
- **Input:** `project_id` (required), `title` (required), `content` (required)
- **Behavior:** Slugifies title to get filename, writes to `<workspace>/notes/<slug>.md`. Creates `notes/` directory if needed.
- **Returns:** `{path, title, created_or_updated}`
- Used for both creating new notes and editing existing ones (LLM reads with `read_file`, modifies, writes back)

### `delete_note`
- **Input:** `project_id` (required), `title` (required)
- **Behavior:** Deletes the `.md` file from disk.
- **Returns:** `{deleted, title}`

No `read_note` tool — the existing `read_file` tool handles reading.

All tools require `project_id`. The LLM infers the project from conversation context.

## System Prompt Addition

```
Notes management — use notes to build up project knowledge:
- Use `list_notes` to see what notes exist for a project
- Use `read_file` to read a note's contents (path: <workspace>/notes/<name>.md)
- Use `write_note` to create or update a note (reads existing, edits, writes back)
- Use `delete_note` to remove a note
- When a user asks to "turn a note into tasks" or "create tasks from the spec",
  read the note, propose a list of tasks with titles and descriptions, and wait
  for the user to approve before calling create_task for each one.
```

## Notes → Tasks Flow

Purely prompt-driven, no special tool:

1. User says "turn the architecture note into tasks for project X"
2. LLM calls `read_file` on the note
3. LLM proposes tasks in chat (titles + descriptions)
4. User approves/edits
5. LLM calls `create_task` for each one

## Agent → Notes Flow

No new code needed. When the bot creates a brainstorming task, the task description tells the agent to write its output to a specific notes path:

> Brainstorm an authentication strategy for this project. Write your output to /path/to/workspace/notes/auth-strategy.md as a structured markdown document.

The agent writes the file as part of its normal work.

## Key Decisions

- **Filesystem over database:** Notes are markdown files, not DB rows. Agents write them directly. Git-trackable. No schema changes.
- **No implicit project scoping:** All tools require `project_id` explicitly. The LLM handles inference from conversation context.
- **LLM-driven editing:** No structured edit commands. The LLM reads the note, modifies content based on natural language instructions, and writes it back.
- **Slugified filenames:** Titles map to filenames via slugification (lowercase, hyphens, strip special chars). Uses the existing `GitManager.slugify()` method.

## Files Modified

- `src/discord/bot.py` — Add 3 tool definitions, 3 tool handlers, update system prompt
