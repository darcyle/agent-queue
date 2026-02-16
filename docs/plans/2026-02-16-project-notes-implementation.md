# Project Notes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `list_notes`, `write_note`, and `delete_note` LLM tools to the Discord bot so users can manage per-project markdown notes through conversation.

**Architecture:** Notes are `.md` files in `<project_workspace>/notes/`. Three new tools in `bot.py` plus a system prompt update. Uses existing `GitManager.slugify()` for title→filename mapping. No database changes.

**Tech Stack:** Python, Discord.py, existing bot tool pattern

---

### Task 1: Add tool definitions to TOOLS list

**Files:**
- Modify: `src/discord/bot.py:349` (insert before the closing `]` of the TOOLS list)

**Step 1: Add the three tool definitions**

Insert these three entries before the closing `]` on line 350, after the `get_token_usage` tool:

```python
    {
        "name": "list_notes",
        "description": "List all notes for a project. Notes are markdown documents stored in the project workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "write_note",
        "description": "Create or overwrite a project note. Use to create new notes or to save edits (read with read_file first, modify, then write back).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (used as filename)"},
                "content": {"type": "string", "description": "Full markdown content"},
            },
            "required": ["project_id", "title", "content"],
        },
    },
    {
        "name": "delete_note",
        "description": "Delete a project note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project ID"},
                "title": {"type": "string", "description": "Note title (as used when creating it)"},
            },
            "required": ["project_id", "title"],
        },
    },
```

**Step 2: Verify syntax**

Run: `python -c "import ast; ast.parse(open('src/discord/bot.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add src/discord/bot.py
git commit -m "feat(notes): add list_notes, write_note, delete_note tool definitions"
```

---

### Task 2: Add tool handlers in `_execute_tool`

**Files:**
- Modify: `src/discord/bot.py:1332` (insert before the `else: return {"error": f"Unknown tool: {name}"}` block)

**Step 1: Add the `list_notes` handler**

Import is already available — `os` is imported at the top of the file. `GitManager.slugify()` is accessible via `self.orchestrator.git`. Insert before the `else:` on line 1333:

```python
            elif name == "list_notes":
                project = await db.get_project(input_data["project_id"])
                if not project:
                    return {"error": f"Project '{input_data['project_id']}' not found"}
                workspace = project.workspace_path or os.path.join(
                    self.config.workspace_dir, input_data["project_id"]
                )
                notes_dir = os.path.join(workspace, "notes")
                if not os.path.isdir(notes_dir):
                    return {"project_id": input_data["project_id"], "notes": []}
                notes = []
                for fname in sorted(os.listdir(notes_dir)):
                    if not fname.endswith(".md"):
                        continue
                    fpath = os.path.join(notes_dir, fname)
                    stat = os.stat(fpath)
                    # Try to extract title from first heading
                    title = fname[:-3].replace("-", " ").title()
                    try:
                        with open(fpath, "r") as f:
                            first_line = f.readline().strip()
                        if first_line.startswith("# "):
                            title = first_line[2:].strip()
                    except Exception:
                        pass
                    notes.append({
                        "name": fname,
                        "title": title,
                        "size_bytes": stat.st_size,
                        "modified": stat.st_mtime,
                        "path": fpath,
                    })
                return {"project_id": input_data["project_id"], "notes": notes}
```

**Step 2: Add the `write_note` handler**

```python
            elif name == "write_note":
                project = await db.get_project(input_data["project_id"])
                if not project:
                    return {"error": f"Project '{input_data['project_id']}' not found"}
                workspace = project.workspace_path or os.path.join(
                    self.config.workspace_dir, input_data["project_id"]
                )
                notes_dir = os.path.join(workspace, "notes")
                os.makedirs(notes_dir, exist_ok=True)
                slug = self.orchestrator.git.slugify(input_data["title"])
                if not slug:
                    return {"error": "Title produces an empty filename"}
                fpath = os.path.join(notes_dir, f"{slug}.md")
                existed = os.path.isfile(fpath)
                with open(fpath, "w") as f:
                    f.write(input_data["content"])
                return {
                    "path": fpath,
                    "title": input_data["title"],
                    "status": "updated" if existed else "created",
                }
```

**Step 3: Add the `delete_note` handler**

```python
            elif name == "delete_note":
                project = await db.get_project(input_data["project_id"])
                if not project:
                    return {"error": f"Project '{input_data['project_id']}' not found"}
                workspace = project.workspace_path or os.path.join(
                    self.config.workspace_dir, input_data["project_id"]
                )
                slug = self.orchestrator.git.slugify(input_data["title"])
                fpath = os.path.join(workspace, "notes", f"{slug}.md")
                if not os.path.isfile(fpath):
                    return {"error": f"Note '{input_data['title']}' not found"}
                os.remove(fpath)
                return {"deleted": fpath, "title": input_data["title"]}
```

**Step 4: Verify syntax**

Run: `python -c "import ast; ast.parse(open('src/discord/bot.py').read()); print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add src/discord/bot.py
git commit -m "feat(notes): add list_notes, write_note, delete_note tool handlers"
```

---

### Task 3: Update system prompt

**Files:**
- Modify: `src/discord/bot.py:374` (insert after the `delete_project` bullet in the system prompt)

**Step 1: Add notes bullet to capabilities list**

After the line `- Delete entire projects (cascading) with \`delete_project\``, add:

```
- Create, read, edit, and delete project notes with `list_notes`, `write_note`, `delete_note`, and `read_file`
```

**Step 2: Add notes management section to system prompt**

After the repository management paragraph (ends around line 382), insert:

```
Notes management — use notes to build up project knowledge:
- Use `list_notes` to see what notes exist for a project
- Use `read_file` to read a note's contents (path: <workspace>/notes/<name>.md)
- Use `write_note` to create or update a note (read with `read_file` first, edit content, \
then write back with `write_note`)
- Use `delete_note` to remove a note
- When a user asks to "turn a note into tasks" or "create tasks from the spec", \
read the note, propose a list of tasks with titles and descriptions, and wait \
for the user to approve before calling `create_task` for each one.
- When creating a brainstorming task for an agent, include the notes path in the \
task description so the agent writes its output to `<workspace>/notes/<name>.md`.
```

**Step 3: Verify syntax**

Run: `python -c "import ast; ast.parse(open('src/discord/bot.py').read()); print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add src/discord/bot.py
git commit -m "feat(notes): update system prompt with notes management guidance"
```

---

### Task 4: Manual verification

**Step 1: Verify all syntax passes**

Run: `python -c "import ast; ast.parse(open('src/discord/bot.py').read()); print('OK')"`
Expected: `OK`

**Step 2: Verify slugify works as expected**

Run: `python -c "from src.git.manager import GitManager; print(GitManager.slugify('API Design')); print(GitManager.slugify('Auth Strategy'))"`
Expected:
```
api-design
auth-strategy
```

**Step 3: Verify tool count**

Run: `python -c "exec(open('src/discord/bot.py').read().split('SYSTEM_PROMPT')[0]); print(f'{len(TOOLS)} tools'); print([t[\"name\"] for t in TOOLS[-3:]])"`
Expected: tool count increases by 3, last 3 are `list_notes`, `write_note`, `delete_note`
