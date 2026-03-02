# Database Migrations — Technical Debt Inventory

All migrations run in `Database.initialize()` on every startup. They are
idempotent (safe to re-run) but represent technical debt from schema evolution.
This document catalogs every migration and its removal criteria.

---

## Overview

```
Database.initialize()
├── SCHEMA (DDL)                          — CREATE TABLE IF NOT EXISTS (14 tables)
├── Column migrations (ALTER TABLE)       — 11 one-liners
├── Index creation                        — 2 indexes
├── _migrate_agent_workspaces()           — agents.checkout_path → agent_workspaces
├── _migrate_repos_to_projects()          — repos table → projects.repo_url columns
├── _migrate_agent_workspaces_to_workspaces()  — agent_workspaces → workspaces
├── _cleanup_cross_project_workspaces()   — fix stale cross-project entries
└── COMMIT
```

---

## Legacy Tables (candidates for DROP)

### `repos` table
**Schema location:** `database.py` line ~57
**Created:** Original schema
**Superseded by:** `projects.repo_url` + `projects.repo_default_branch` columns
**Migration that replaced it:** `_migrate_repos_to_projects()`
**Still referenced by:**
- `_migrate_repos_to_projects()` — reads from `repos` to populate project columns
- `_migrate_agent_workspaces_to_workspaces()` — reads `repos.source_type`
- `_cmd_get_task_diff()` in command_handler.py — fallback path resolution via `task.repo_id`
- `_resolve_repo_path()` in command_handler.py — fallback when no workspace found
- `Database.create_repo()`, `get_repo()`, `list_repos()`, `delete_repo()` — full CRUD still exists
- `delete_project()` cascade — `DELETE FROM repos WHERE project_id = ?`

**Removal plan:**
1. Audit `_resolve_repo_path()` — replace `repo.source_path`/`repo.checkout_base_path` fallbacks with workspace lookups
2. Remove `task.repo_id` column (and `tasks.repo_id` FK in schema)
3. Remove `agents.repo_id` column from schema
4. Remove all `Database.*_repo()` methods
5. Drop `repos` table from SCHEMA
6. Remove `_migrate_repos_to_projects()`
7. Remove `RepoConfig` from models.py

### `agent_workspaces` table
**Schema location:** `database.py` line ~179
**Created:** Intermediate migration step (agents had per-project workspace assignments)
**Superseded by:** `workspaces` table (project-scoped, agent-agnostic with dynamic locking)
**Migration that replaced it:** `_migrate_agent_workspaces_to_workspaces()`
**Still referenced by:**
- `_migrate_agent_workspaces()` — writes into it from `agents.checkout_path`
- `_migrate_agent_workspaces_to_workspaces()` — reads from it to populate `workspaces`
- `delete_agent()` cascade — `DELETE FROM agent_workspaces WHERE agent_id = ?`
- `delete_project()` cascade — `DELETE FROM agent_workspaces WHERE project_id = ?`

**Known bug (fixed):** This table contained a stale row mapping
`(agent-queue-worker, moss-and-spade-inventory-manager) → /mnt/d/Dev/agent-queue2`
which caused the migration to re-create a bogus workspace entry on every restart,
sending agents to the wrong project directory.

**Removal plan:**
1. Remove `_migrate_agent_workspaces()` method
2. Remove `_migrate_agent_workspaces_to_workspaces()` method
3. Remove cascade DELETEs in `delete_agent()` and `delete_project()`
4. Drop `agent_workspaces` table from SCHEMA

---

## Legacy Columns (candidates for DROP)

### `agents.checkout_path` and `agents.repo_id`
**Schema location:** `database.py` line ~125-126
**Superseded by:** `workspaces` table (agents acquire workspace locks dynamically)
**Migration that reads it:** `_migrate_agent_workspaces()` — copies to `agent_workspaces`
**Removal plan:** Drop columns from SCHEMA DDL. The migration already checks
`if "checkout_path" not in columns: return` so it's safe to remove the column first.

### `projects.workspace_path`
**Schema location:** `database.py` line ~51
**Superseded by:** `workspaces` table
**Column migration:** `ALTER TABLE projects ADD COLUMN workspace_path TEXT` (line ~299)
**Still referenced by:** Only the schema DDL and the ALTER migration. No production code reads it.
**Removal plan:** Remove from SCHEMA DDL and remove the ALTER migration line.

### `projects.discord_control_channel_id`
**Schema location:** `database.py` line ~53 (in DDL after ALTER migration)
**Column migration:** line ~305
**Status:** Appears unused — `discord_channel_id` is the only channel column used in production.
**Removal plan:** Verify no code reads it, then remove from SCHEMA and migrations.

---

## Column Migrations (ALTER TABLE)

All in `Database.initialize()` lines ~298-311. Each runs in a try/except that
swallows "column already exists" errors.

| # | Migration SQL | Target | Status |
|---|---|---|---|
| 1 | `ALTER TABLE projects ADD COLUMN workspace_path TEXT` | `projects` | **Removable** — superseded by `workspaces` table |
| 2 | `ALTER TABLE repos ADD COLUMN source_type TEXT NOT NULL DEFAULT 'clone'` | `repos` | **Removable** when `repos` table is dropped |
| 3 | `ALTER TABLE repos ADD COLUMN source_path TEXT NOT NULL DEFAULT ''` | `repos` | **Removable** when `repos` table is dropped |
| 4 | `ALTER TABLE tasks ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0` | `tasks` | **Keep** — already in SCHEMA DDL, but migration needed for pre-existing DBs |
| 5 | `ALTER TABLE tasks ADD COLUMN pr_url TEXT` | `tasks` | **Keep** — same |
| 6 | `ALTER TABLE projects ADD COLUMN discord_channel_id TEXT` | `projects` | **Keep** — actively used |
| 7 | `ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT` | `projects` | **Removable** — appears unused |
| 8 | `ALTER TABLE tasks ADD COLUMN plan_source TEXT` | `tasks` | **Keep** — actively used |
| 9 | `ALTER TABLE tasks ADD COLUMN is_plan_subtask INTEGER NOT NULL DEFAULT 0` | `tasks` | **Keep** — actively used |
| 10 | `ALTER TABLE tasks ADD COLUMN task_type TEXT` | `tasks` | **Keep** — actively used |
| 11 | `ALTER TABLE projects ADD COLUMN repo_url TEXT DEFAULT ''` | `projects` | **Keep** — actively used |
| 12 | `ALTER TABLE projects ADD COLUMN repo_default_branch TEXT DEFAULT 'main'` | `projects` | **Keep** — actively used |

**Note:** Migrations 4-12 are technically no-ops for new databases (the columns
are already in the SCHEMA DDL) but are needed for databases created before those
columns were added. Once all production databases have been migrated, they can
be removed.

---

## Data Migration Methods

### `_migrate_agent_workspaces()`
**Location:** `database.py` line ~337
**Purpose:** Copy `agents.checkout_path` + `agents.repo_id` into `agent_workspaces` table
**Source:** `agents` table (columns: `checkout_path`, `repo_id`, `current_task_id`)
**Destination:** `agent_workspaces` table
**Guard:** Returns early if `checkout_path` column no longer exists in `agents`
**Status:** **Removable** — intermediate step, feeds into `_migrate_agent_workspaces_to_workspaces`

### `_migrate_repos_to_projects()`
**Location:** `database.py` line ~420
**Purpose:** Copy first repo's `url`/`default_branch` into `projects.repo_url`/`repo_default_branch`
**Source:** `repos` table
**Destination:** `projects` table
**Guard:** Only writes to projects where `repo_url IS NULL OR repo_url = ''`
**Status:** **Removable** once `repos` table is dropped and all projects have `repo_url` populated

### `_migrate_agent_workspaces_to_workspaces()`
**Location:** `database.py` line ~444
**Purpose:** Deduplicate `agent_workspaces` rows into the canonical `workspaces` table
**Source:** `agent_workspaces` table
**Destination:** `workspaces` table
**Guard:** `INSERT OR IGNORE` + `UNIQUE(project_id, workspace_path)` constraint. Skips entries where workspace path belongs to a different project.
**Status:** **Removable** once `agent_workspaces` table is dropped

### `_cleanup_cross_project_workspaces()`
**Location:** `database.py` line ~500
**Purpose:** Remove workspace entries where the same path is claimed by multiple projects
**Trigger:** Runs every startup to catch stale data from old migrations
**Status:** **Keep for now** — defensive cleanup. Can be removed once `agent_workspaces` table (the source of bad data) is dropped.

---

## Removal Checklist

When you're ready to clean up, do this in order:

### Phase 1: Drop `agent_workspaces` (safest, highest value)
- [ ] Remove `_migrate_agent_workspaces()` method
- [ ] Remove `_migrate_agent_workspaces_to_workspaces()` method
- [ ] Remove `_cleanup_cross_project_workspaces()` method (no longer needed)
- [ ] Remove `DELETE FROM agent_workspaces` in `delete_agent()` and `delete_project()`
- [ ] Remove `agent_workspaces` from SCHEMA DDL
- [ ] Remove migration call chain from `initialize()`

### Phase 2: Drop `repos` table
- [ ] Refactor `_resolve_repo_path()` to use only `workspaces` table (no `repos` fallback)
- [ ] Remove `task.repo_id` usage — grep for `repo_id` in command_handler.py and orchestrator.py
- [ ] Remove `Database.create_repo()`, `get_repo()`, `list_repos()`, `delete_repo()`
- [ ] Remove `_migrate_repos_to_projects()` method
- [ ] Remove `DELETE FROM repos` in `delete_project()`
- [ ] Remove `repos` from SCHEMA DDL
- [ ] Remove `RepoConfig` from models.py
- [ ] Remove ALTER TABLE migrations #2 and #3

### Phase 3: Drop legacy columns
- [ ] Remove `agents.checkout_path` and `agents.repo_id` from SCHEMA DDL
- [ ] Remove `projects.workspace_path` from SCHEMA DDL + ALTER migration #1
- [ ] Remove `projects.discord_control_channel_id` from SCHEMA DDL + ALTER migration #7
- [ ] Remove `tasks.repo_id` from SCHEMA DDL

### Phase 4: Clean up remaining ALTER migrations
- [ ] Once confident all production DBs have the columns, remove migrations #4-12
- [ ] The SCHEMA DDL alone is sufficient for new databases
