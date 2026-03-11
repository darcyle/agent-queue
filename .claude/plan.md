# Implementation Plan: New Project Wizard

## Background & Design

### Overview

The New Project Wizard is an interactive Discord-based flow that guides users through creating a fully-configured project in agent-queue. Instead of requiring users to run multiple separate commands (`/create-project`, `/add-workspace` × N, channel setup), the wizard asks a series of questions via Discord modals/buttons and then executes all setup steps automatically.

### Architecture

The wizard will be implemented as a **Discord UI flow** using discord.py's `Modal` and `View` components, orchestrated by a new wizard controller that delegates to existing `CommandHandler` commands. This keeps all business logic in the command handler while providing a guided UX layer.

**Key design decisions:**
- **Discord-first**: The wizard runs as a Discord slash command (`/new-project`) that presents interactive modals and buttons. No CLI wizard needed since all project management is Discord-driven.
- **Reuse existing commands**: The wizard calls `CommandHandler.execute()` for each step (create_project, add_workspace, set_project_channel) rather than duplicating logic.
- **GitHub repo creation via `gh` CLI**: A new `_cmd_create_github_repo` command will be added to `CommandHandler` that wraps `gh repo create`.
- **Multi-step with rollback**: If a later step fails, earlier steps are cleaned up (delete project, remove workspaces).

### Proposed User Flow

```
User: /new-project
Bot: Opens Modal 1 — "Project Information"
     ┌─────────────────────────────────────────┐
     │ Project Name: [___________________]     │
     │ Description:  [___________________]     │
     │               [___________________]     │
     │ Tech Stack:   [___________________]     │
     │ Default Branch: [main_____________]     │
     │                      [Submit]           │
     └─────────────────────────────────────────┘

Bot: Shows embed with collected info + buttons
     ┌─────────────────────────────────────────┐
     │ 📋 Project: my-awesome-app              │
     │ Description: A web app for...           │
     │ Tech Stack: TypeScript, React, Node     │
     │ Branch: main                            │
     │                                         │
     │ Repository Options:                     │
     │ [Create GitHub Repo] [Use Existing] [Skip] │
     └─────────────────────────────────────────┘

If "Create GitHub Repo":
Bot: Opens Modal 2 — "GitHub Repository"
     ┌─────────────────────────────────────────┐
     │ Repo Name: [my-awesome-app________]     │
     │ Visibility: (●) Private  ( ) Public     │
     │ GitHub Org: [___________________]       │
     │     (leave blank for personal repo)     │
     │                      [Create]           │
     └─────────────────────────────────────────┘

If "Use Existing":
Bot: Opens Modal — "Existing Repository"
     ┌─────────────────────────────────────────┐
     │ Repo URL: [https://github.com/...]      │
     │                      [Submit]           │
     └─────────────────────────────────────────┘

Bot: Shows workspace configuration
     ┌─────────────────────────────────────────┐
     │ How many workspaces? (for parallel      │
     │ agent execution)                        │
     │                                         │
     │ [2] [3] [4] [5] [Custom]               │
     └─────────────────────────────────────────┘

Bot: Shows final confirmation
     ┌─────────────────────────────────────────┐
     │ ✅ Project Setup Complete!               │
     │                                         │
     │ Project: my-awesome-app                 │
     │ Repo: github.com/user/my-awesome-app    │
     │ Workspaces: 3 clones ready              │
     │ Channel: #my-awesome-app                │
     │                                         │
     │ README.md generated and committed.      │
     │ You're ready to create tasks!           │
     └─────────────────────────────────────────┘
```

### Questions Collected from User

**Modal 1 — Project Information:**
| Field | Required | Default | Purpose |
|-------|----------|---------|---------|
| Project Name | Yes | — | Display name, used to generate slug ID |
| Description | No | "" | README intro, project channel topic |
| Tech Stack | No | "" | README content, helps agents understand codebase |
| Default Branch | No | "main" | Git default branch |

**Modal 2 — GitHub Repository (if creating new):**
| Field | Required | Default | Purpose |
|-------|----------|---------|---------|
| Repo Name | No | project slug | GitHub repository name |
| Visibility | No | "private" | Public or private repo |
| GitHub Org | No | personal | Organization to create under |

**Button Selection — Workspaces:**
| Field | Required | Default | Purpose |
|-------|----------|---------|---------|
| Workspace Count | No | 3 | Number of parallel clone workspaces |

### Error Handling & UX Considerations

1. **Timeouts**: Discord modals timeout after 15 minutes. The wizard stores state in memory keyed by user ID so partial progress isn't lost on modal resubmission.
2. **Rollback on failure**: If workspace cloning fails mid-way, already-created workspaces and the project record are cleaned up. The user gets an error embed with details.
3. **GitHub auth**: Before attempting repo creation, validate that `gh auth status` succeeds. Show a helpful error if not authenticated.
4. **Duplicate projects**: The command handler already rejects duplicate project IDs. The wizard catches this and suggests an alternative name.
5. **Long operations**: Git cloning and GitHub repo creation can take time. The wizard defers the Discord interaction and shows progress updates via followup messages.
6. **Permissions**: Only authorized users (from config) can run the wizard.
7. **README generation**: A basic README.md is generated from the collected info (name, description, tech stack) and committed to the repo.

### Integration with Existing Architecture

```
/new-project (Discord slash command)
    │
    ├─► ProjectWizardModal (discord.ui.Modal)
    │       Collects: name, description, tech_stack, default_branch
    │
    ├─► RepoChoiceView (discord.ui.View)
    │       Buttons: Create GitHub Repo | Use Existing | Skip
    │
    ├─► GitHubRepoModal (discord.ui.Modal) — if creating
    │       Collects: repo_name, visibility, org
    │
    ├─► WorkspaceCountView (discord.ui.View)
    │       Buttons: 2 | 3 | 4 | 5 | Custom
    │
    └─► WizardExecutor (new class)
            │
            ├─► CommandHandler.execute("create_github_repo", {...})  [NEW]
            │       └─► GitManager.create_github_repo()              [NEW]
            │
            ├─► CommandHandler.execute("create_project", {...})
            │       └─► Database.create_project()
            │
            ├─► CommandHandler.execute("add_workspace", {...})  × N
            │       └─► GitManager.create_checkout()
            │
            ├─► _auto_create_project_channels()
            │       └─► Discord channel creation + linking
            │
            └─► CommandHandler.execute("generate_readme", {...})  [NEW]
                    └─► Git commit README.md to repo
```

---

## Phase 1: Add GitHub Repo Creation to GitManager and CommandHandler

Add the ability to create GitHub repositories via the `gh` CLI, which is the missing primitive needed for the wizard.

**Files to modify:**
- `src/git/manager.py` — Add `create_github_repo(name, private, org, description)` method that runs `gh repo create` via subprocess
- `src/command_handler.py` — Add `_cmd_create_github_repo(args)` command that:
  - Validates `gh auth status` first
  - Calls `GitManager.create_github_repo()`
  - Returns the repo URL on success
- `src/chat_agent.py` — Register `create_github_repo` as an LLM tool (optional, for chat agent parity)

**New `GitManager.create_github_repo()` method:**
```python
def create_github_repo(self, name: str, private: bool = True,
                       org: str | None = None,
                       description: str = "") -> str:
    """Create a GitHub repo via `gh` CLI. Returns the repo URL."""
    cmd = ["gh", "repo", "create"]
    full_name = f"{org}/{name}" if org else name
    cmd.append(full_name)
    cmd.append("--private" if private else "--public")
    if description:
        cmd.extend(["--description", description])
    cmd.append("--confirm")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           env=self._SUBPROCESS_ENV)
    if result.returncode != 0:
        raise GitError(f"gh repo create failed: {result.stderr}")
    # Parse URL from output
    url = result.stdout.strip()
    return url
```

**New `_cmd_create_github_repo` command:**
```python
async def _cmd_create_github_repo(self, args: dict) -> dict:
    name = args["name"]
    private = args.get("private", True)
    org = args.get("org")
    description = args.get("description", "")
    try:
        url = self.orchestrator.git.create_github_repo(
            name, private=private, org=org, description=description
        )
        return {"created": True, "repo_url": url, "name": name}
    except GitError as e:
        return {"error": str(e)}
```

**Tests:**
- Unit test for `GitManager.create_github_repo()` with mocked subprocess
- Unit test for `_cmd_create_github_repo` command handler

---

## Phase 2: Add README Generation Command

Add a command that generates a basic README.md from project metadata and commits it to the repository.

**Files to modify:**
- `src/command_handler.py` — Add `_cmd_generate_readme(args)` that:
  - Takes project_id, description, tech_stack
  - Generates a templated README.md
  - Writes it to the first workspace's path
  - Commits via `GitManager.commit_all()`
  - Pushes via `GitManager.push_branch()`
- `src/git/manager.py` — No changes needed (commit_all and push_branch already exist)

**README template:**
```markdown
# {project_name}

{description}

## Tech Stack

{tech_stack_list}

## Getting Started

TODO: Add setup instructions

## Development

This project uses [AgentQueue](https://github.com/...) for AI-assisted development.
```

**Command signature:**
```python
async def _cmd_generate_readme(self, args: dict) -> dict:
    project_id = args["project_id"]
    description = args.get("description", "")
    tech_stack = args.get("tech_stack", "")
    workspace_path = args.get("workspace_path")
    # ... generate, write, commit, push
```

---

## Phase 3: Implement Discord Wizard UI Components

Create the interactive Discord modals and views that drive the wizard flow.

**New file:** `src/discord/project_wizard.py`

This file contains:

1. **`ProjectWizardState`** — Dataclass holding wizard state per user:
   ```python
   @dataclass
   class ProjectWizardState:
       user_id: int
       project_name: str = ""
       description: str = ""
       tech_stack: str = ""
       default_branch: str = "main"
       repo_url: str = ""
       repo_visibility: str = "private"
       repo_org: str = ""
       workspace_count: int = 3
       started_at: float = 0.0
   ```

2. **`ProjectInfoModal`** (discord.ui.Modal) — First modal collecting name, description, tech stack, default branch. On submit, shows `RepoChoiceView`.

3. **`RepoChoiceView`** (discord.ui.View) — Three buttons:
   - "Create GitHub Repo" → opens `GitHubRepoModal`
   - "Use Existing Repo" → opens `ExistingRepoModal`
   - "Skip (No Repo)" → proceeds to workspace count

4. **`GitHubRepoModal`** (discord.ui.Modal) — Collects repo name, visibility (via select), org. On submit, shows `WorkspaceCountView`.

5. **`ExistingRepoModal`** (discord.ui.Modal) — Single text input for repo URL. On submit, shows `WorkspaceCountView`.

6. **`WorkspaceCountView`** (discord.ui.View) — Buttons for 2/3/4/5 workspaces. On click, triggers `WizardExecutor`.

7. **`WizardExecutor`** — Async method that:
   - Defers the interaction (long-running)
   - Creates GitHub repo (if requested)
   - Creates project via command handler
   - Clones N workspaces via command handler
   - Creates Discord channel
   - Generates and commits README
   - Sends final success/failure embed
   - Implements rollback on failure

**Error handling in executor:**
```python
async def execute_wizard(interaction, state, handler, bot):
    created_project = False
    created_workspaces = []
    try:
        # Step 1: Create GitHub repo (if needed)
        if state.create_repo:
            result = await handler.execute("create_github_repo", {...})
            if "error" in result:
                raise WizardError(f"GitHub repo creation failed: {result['error']}")
            state.repo_url = result["repo_url"]

        # Step 2: Create project
        result = await handler.execute("create_project", {...})
        if "error" in result:
            raise WizardError(f"Project creation failed: {result['error']}")
        created_project = True

        # Step 3: Clone workspaces
        for i in range(state.workspace_count):
            result = await handler.execute("add_workspace", {...})
            if "error" in result:
                raise WizardError(f"Workspace {i+1} failed: {result['error']}")
            created_workspaces.append(result["created"])

        # Step 4: Create Discord channel
        # (uses existing _auto_create_project_channels)

        # Step 5: Generate README
        if state.repo_url:
            await handler.execute("generate_readme", {...})

    except WizardError as e:
        # Rollback: delete workspaces, delete project
        for ws_id in created_workspaces:
            await handler.execute("remove_workspace", {"workspace_id": ws_id})
        if created_project:
            await handler.execute("delete_project", {"project_id": state.project_id})
        await interaction.followup.send(embed=error_embed(...))
        return
```

---

## Phase 4: Register Slash Command and Wire Up Bot Integration

Register the `/new-project` slash command in the Discord bot and connect all wizard components.

**Files to modify:**
- `src/discord/commands.py` — Add the `/new-project` slash command that:
  - Creates a `ProjectWizardState` for the user
  - Opens the `ProjectInfoModal`
  - Stores state in a module-level dict keyed by user ID
  - Import and use components from `src/discord/project_wizard.py`

- `src/discord/bot.py` — No changes needed (command registration is automatic via `commands.py`)

**Slash command registration:**
```python
@bot.tree.command(name="new-project", description="Interactive wizard to set up a new project")
async def new_project_wizard(interaction: discord.Interaction):
    state = ProjectWizardState(user_id=interaction.user.id, started_at=time.time())
    _wizard_states[interaction.user.id] = state
    modal = ProjectInfoModal(state, handler, bot)
    await interaction.response.send_modal(modal)
```

**State cleanup:** States older than 30 minutes are pruned on each new wizard invocation to prevent memory leaks.

---

## Phase 5: Testing and Polish

Add tests and handle edge cases.

**New test file:** `tests/test_project_wizard.py`

**Test cases:**
1. `test_create_github_repo_success` — Mock `gh repo create` subprocess, verify URL returned
2. `test_create_github_repo_auth_failure` — Mock failed `gh auth status`, verify error
3. `test_create_github_repo_private_vs_public` — Verify correct CLI flags
4. `test_generate_readme_content` — Verify README template rendering
5. `test_generate_readme_commit` — Verify git commit is created
6. `test_wizard_state_management` — Verify state creation, retrieval, cleanup
7. `test_wizard_rollback_on_workspace_failure` — Verify cleanup when workspace creation fails
8. `test_wizard_rollback_on_channel_failure` — Verify cleanup when channel creation fails
9. `test_wizard_duplicate_project_name` — Verify error handling for duplicate names
10. `test_wizard_no_repo_url_flow` — Verify skip-repo path works
11. `test_wizard_existing_repo_flow` — Verify use-existing-repo path

**Polish items:**
- Add progress messages during long operations ("Creating repository...", "Cloning workspace 2/3...")
- Add a "Cancel" button to each view that cleanly exits the wizard
- Verify the wizard works when `per_project_channels.auto_create` is both true and false
- Add rate limiting (one wizard per user at a time)
- Log wizard completions and failures for debugging
