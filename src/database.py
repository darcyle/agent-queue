"""Persistence layer for the agent queue system.

Single SQLite database using WAL journal mode for concurrent reads from
the orchestrator loop, Discord bot, and chat agent without blocking writers.

Follows the repository pattern -- all SQL is encapsulated here. The rest of
the codebase interacts with the database exclusively through the
:class:`Database` class, receiving and returning domain model dataclasses.

The schema covers 14 tables organized around the core domain concepts:
projects, repos, tasks (with dependencies, criteria, context, tools, and
results), agents, token_ledger, events, rate_limits, hooks, hook_runs,
and system_config.

Migrations are applied as idempotent ``ALTER TABLE ADD COLUMN`` statements
during initialization. If a column already exists the error is silently
caught, so migrations are safe to re-run on every startup.

See specs/database.md for the full schema and behavioral specification.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import aiosqlite

from src.models import (
    Agent, AgentState, AgentWorkspace, Hook, HookRun, Project, ProjectStatus,
    RepoConfig, RepoSourceType, Task, TaskStatus, VerificationType,
)
from src.state_machine import is_valid_status_transition

logger = logging.getLogger(__name__)

# Complete DDL for all 14 tables. Executed via executescript() on startup,
# so every statement uses CREATE TABLE IF NOT EXISTS for idempotency.
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    credit_weight REAL NOT NULL DEFAULT 1.0,
    max_concurrent_agents INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    budget_limit INTEGER,
    workspace_path TEXT,
    discord_channel_id TEXT,
    discord_control_channel_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    url TEXT NOT NULL,
    default_branch TEXT NOT NULL DEFAULT 'main',
    checkout_base_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    parent_task_id TEXT REFERENCES tasks(id),
    repo_id TEXT REFERENCES repos(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'DEFINED',
    verification_type TEXT NOT NULL DEFAULT 'auto_test',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    assigned_agent_id TEXT REFERENCES agents(id),
    branch_name TEXT,
    resume_after REAL,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT,
    plan_source TEXT,
    is_plan_subtask INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_criteria (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id != depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS task_context (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    label TEXT,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_tools (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL,
    config TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'IDLE',
    current_task_id TEXT REFERENCES tasks(id),
    checkout_path TEXT,
    repo_id TEXT REFERENCES repos(id),
    pid INTEGER,
    last_heartbeat REAL,
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    session_tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS token_ledger (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    task_id TEXT NOT NULL REFERENCES tasks(id),
    tokens_used INTEGER NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    agent_id TEXT,
    payload TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL,
    limit_type TEXT NOT NULL,
    max_tokens INTEGER NOT NULL,
    current_tokens INTEGER NOT NULL DEFAULT 0,
    window_start REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_results (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    result TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    files_changed TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_workspaces (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    workspace_path TEXT NOT NULL,
    repo_id TEXT REFERENCES repos(id),
    created_at REAL NOT NULL,
    PRIMARY KEY (agent_id, project_id)
);

CREATE TABLE IF NOT EXISTS hooks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    trigger TEXT NOT NULL,
    context_steps TEXT NOT NULL DEFAULT '[]',
    prompt_template TEXT NOT NULL,
    llm_config TEXT,
    cooldown_seconds INTEGER NOT NULL DEFAULT 3600,
    max_tokens_per_run INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hook_runs (
    id TEXT PRIMARY KEY,
    hook_id TEXT NOT NULL REFERENCES hooks(id),
    project_id TEXT NOT NULL,
    trigger_reason TEXT NOT NULL,
    event_data TEXT,
    context_results TEXT,
    prompt_sent TEXT,
    llm_response TEXT,
    actions_taken TEXT,
    skipped_reason TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    started_at REAL NOT NULL,
    completed_at REAL
);
"""


class Database:
    """Async SQLite persistence layer implementing the repository pattern.

    All database access in the system goes through this class. It owns the
    connection lifecycle, schema creation, migrations, and provides typed
    CRUD methods that accept and return domain dataclasses from
    :mod:`src.models`.

    The connection uses WAL journal mode and has foreign keys enabled, so
    referential integrity is enforced at the database level. Row factory is
    set to ``aiosqlite.Row`` for dict-like column access.

    State transitions go through :meth:`transition_task`, which validates
    against the state machine but always applies the update (logging-only
    enforcement) to avoid blocking production on unexpected edge cases.
    """

    def __init__(self, path: str):
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # Migrations for existing databases
        for migration in [
            "ALTER TABLE projects ADD COLUMN workspace_path TEXT",
            "ALTER TABLE repos ADD COLUMN source_type TEXT NOT NULL DEFAULT 'clone'",
            "ALTER TABLE repos ADD COLUMN source_path TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN requires_approval INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tasks ADD COLUMN pr_url TEXT",
            "ALTER TABLE projects ADD COLUMN discord_channel_id TEXT",
            "ALTER TABLE projects ADD COLUMN discord_control_channel_id TEXT",
            "ALTER TABLE tasks ADD COLUMN plan_source TEXT",
            "ALTER TABLE tasks ADD COLUMN is_plan_subtask INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                await self._db.execute(migration)
            except Exception:
                pass  # Column already exists
        # Migrate existing agent checkout_path/repo_id into agent_workspaces
        await self._migrate_agent_workspaces()
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def _migrate_agent_workspaces(self) -> None:
        """Migrate existing agent.checkout_path/repo_id into agent_workspaces.

        Idempotent: uses INSERT OR IGNORE so it's safe to re-run on every startup.

        Two strategies for determining project_id:
        1. From the agent's current_task_id (if still set from a previous run)
        2. From the agent's most recent task result (for IDLE agents)
        """
        try:
            # Check if checkout_path column still exists
            cursor = await self._db.execute("PRAGMA table_info(agents)")
            columns = {row["name"] for row in await cursor.fetchall()}
            if "checkout_path" not in columns:
                return  # Column already dropped, nothing to migrate

            cursor = await self._db.execute(
                "SELECT id, checkout_path, repo_id, current_task_id "
                "FROM agents "
                "WHERE checkout_path IS NOT NULL AND checkout_path != ''"
            )
            agents = await cursor.fetchall()
            for agent_row in agents:
                agent_id = agent_row["id"]
                checkout_path = agent_row["checkout_path"]
                repo_id = agent_row["repo_id"]

                # Strategy 1: from current_task_id
                project_id = None
                if agent_row["current_task_id"]:
                    task_cursor = await self._db.execute(
                        "SELECT project_id FROM tasks WHERE id = ?",
                        (agent_row["current_task_id"],),
                    )
                    task_row = await task_cursor.fetchone()
                    if task_row:
                        project_id = task_row["project_id"]

                # Strategy 2: from most recent task result
                if not project_id:
                    result_cursor = await self._db.execute(
                        "SELECT t.project_id FROM task_results tr "
                        "JOIN tasks t ON t.id = tr.task_id "
                        "WHERE tr.agent_id = ? "
                        "ORDER BY tr.created_at DESC LIMIT 1",
                        (agent_id,),
                    )
                    result_row = await result_cursor.fetchone()
                    if result_row:
                        project_id = result_row["project_id"]

                # Strategy 3: from most recent assigned task
                if not project_id:
                    assigned_cursor = await self._db.execute(
                        "SELECT project_id FROM tasks "
                        "WHERE assigned_agent_id = ? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (agent_id,),
                    )
                    assigned_row = await assigned_cursor.fetchone()
                    if assigned_row:
                        project_id = assigned_row["project_id"]

                if not project_id:
                    logger.debug(
                        "Migration: skipping agent '%s' — cannot determine project_id",
                        agent_id,
                    )
                    continue

                await self._db.execute(
                    "INSERT OR IGNORE INTO agent_workspaces "
                    "(agent_id, project_id, workspace_path, repo_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (agent_id, project_id, checkout_path, repo_id, time.time()),
                )
                logger.info(
                    "Migration: agent '%s' workspace for project '%s' -> '%s'",
                    agent_id, project_id, checkout_path,
                )
        except Exception as e:
            logger.debug("Agent workspace migration (benign if columns removed): %s", e)

    # --- Projects ---
    # CRUD for the projects table. Each project has a credit_weight that
    # determines its fair share in scheduling, concurrency limits, optional
    # budget caps, and a Discord channel binding.

    async def create_project(self, project: Project) -> None:
        await self._db.execute(
            "INSERT INTO projects (id, name, credit_weight, max_concurrent_agents, "
            "status, total_tokens_used, budget_limit, workspace_path, "
            "discord_channel_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project.id, project.name, project.credit_weight,
             project.max_concurrent_agents, project.status.value,
             project.total_tokens_used, project.budget_limit,
             project.workspace_path, project.discord_channel_id,
             time.time()),
        )
        await self._db.commit()

    async def get_project(self, project_id: str) -> Project | None:
        cursor = await self._db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    async def list_projects(
        self, status: ProjectStatus | None = None
    ) -> list[Project]:
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM projects WHERE status = ?", (status.value,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM projects")
        rows = await cursor.fetchall()
        return [self._row_to_project(r) for r in rows]

    async def update_project(self, project_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, ProjectStatus):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(project_id)
        await self._db.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    def _row_to_project(self, row) -> Project:
        keys = row.keys()
        # Backward compat: if discord_channel_id is NULL but discord_control_channel_id
        # has a value, use that as the single channel.
        channel_id = row["discord_channel_id"] if "discord_channel_id" in keys else None
        if not channel_id and "discord_control_channel_id" in keys:
            channel_id = row["discord_control_channel_id"]
        return Project(
            id=row["id"],
            name=row["name"],
            credit_weight=row["credit_weight"],
            max_concurrent_agents=row["max_concurrent_agents"],
            status=ProjectStatus(row["status"]),
            total_tokens_used=row["total_tokens_used"],
            budget_limit=row["budget_limit"],
            workspace_path=row["workspace_path"] if "workspace_path" in keys else None,
            discord_channel_id=channel_id,
        )

    # --- Repos ---
    # Git repository configurations attached to projects. A project may have
    # multiple repos. Each repo knows its clone URL, default branch, and
    # where to check out working copies on disk.

    async def create_repo(self, repo: RepoConfig) -> None:
        await self._db.execute(
            "INSERT INTO repos (id, project_id, url, default_branch, "
            "checkout_base_path, source_type, source_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (repo.id, repo.project_id, repo.url, repo.default_branch,
             repo.checkout_base_path, repo.source_type.value, repo.source_path),
        )
        await self._db.commit()

    async def get_repo(self, repo_id: str) -> RepoConfig | None:
        cursor = await self._db.execute(
            "SELECT * FROM repos WHERE id = ?", (repo_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_repo(row)

    async def list_repos(self, project_id: str | None = None) -> list[RepoConfig]:
        if project_id:
            cursor = await self._db.execute(
                "SELECT * FROM repos WHERE project_id = ?", (project_id,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM repos")
        rows = await cursor.fetchall()
        return [self._row_to_repo(r) for r in rows]

    async def delete_repo(self, repo_id: str) -> None:
        await self._db.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        await self._db.commit()

    def _row_to_repo(self, row) -> RepoConfig:
        return RepoConfig(
            id=row["id"],
            project_id=row["project_id"],
            source_type=RepoSourceType(row["source_type"]) if row["source_type"] else RepoSourceType.CLONE,
            url=row["url"],
            source_path=row["source_path"] if "source_path" in row.keys() else "",
            checkout_base_path=row["checkout_base_path"] if "checkout_base_path" in row.keys() else "",
            default_branch=row["default_branch"],
        )

    # --- Tasks ---
    # The core work unit. Tasks flow through the state machine (DEFINED ->
    # READY -> ASSIGNED -> IN_PROGRESS -> ... -> COMPLETED). Each task
    # belongs to a project and optionally to a parent task (plan subtasks).
    # Related data lives in task_criteria, task_context, task_tools, and
    # task_results tables.

    async def create_task(self, task: Task) -> None:
        now = time.time()
        await self._db.execute(
            "INSERT INTO tasks (id, project_id, parent_task_id, repo_id, title, "
            "description, priority, status, verification_type, retry_count, "
            "max_retries, assigned_agent_id, branch_name, resume_after, "
            "requires_approval, pr_url, plan_source, is_plan_subtask, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.id, task.project_id, task.parent_task_id, task.repo_id,
             task.title, task.description, task.priority, task.status.value,
             task.verification_type.value, task.retry_count, task.max_retries,
             task.assigned_agent_id, task.branch_name, task.resume_after,
             int(task.requires_approval), task.pr_url, task.plan_source,
             int(task.is_plan_subtask), now, now),
        )
        await self._db.commit()

    async def get_task(self, task_id: str) -> Task | None:
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    async def list_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        conditions = []
        vals = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        if status:
            conditions.append("status = ?")
            vals.append(status.value)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority ASC, created_at ASC",
            vals,
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def list_active_tasks(
        self,
        project_id: str | None = None,
        exclude_statuses: set[TaskStatus] | None = None,
    ) -> list[Task]:
        """List non-terminal tasks, optionally filtered by project.

        Unlike :meth:`list_tasks`, this method performs status filtering at the
        SQL level so the database only returns actionable rows.  This is more
        efficient for cross-project overviews where the majority of historical
        tasks may be completed.

        Parameters
        ----------
        project_id:
            Optional project filter.  When *None*, tasks from **all** projects
            are returned.
        exclude_statuses:
            Set of :class:`TaskStatus` values to exclude.  Defaults to the
            three terminal statuses: COMPLETED, FAILED, BLOCKED.
        """
        if exclude_statuses is None:
            exclude_statuses = {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
            }

        conditions: list[str] = []
        vals: list = []

        if exclude_statuses:
            placeholders = ", ".join("?" for _ in exclude_statuses)
            conditions.append(f"status NOT IN ({placeholders})")
            vals.extend(s.value for s in exclude_statuses)

        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM tasks {where} ORDER BY priority ASC, created_at ASC",
            vals,
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def count_tasks_by_status(
        self,
        project_id: str | None = None,
    ) -> dict[str, int]:
        """Return a {status_value: count} mapping for quick summary stats.

        Useful for reporting how many tasks were hidden when filtering.
        """
        conditions: list[str] = []
        vals: list = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT status, COUNT(*) as cnt FROM tasks {where} GROUP BY status",
            vals,
        )
        rows = await cursor.fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    async def update_task(self, task_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, (TaskStatus, VerificationType)):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(task_id)
        await self._db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    async def transition_task(
        self,
        task_id: str,
        new_status: TaskStatus,
        *,
        context: str = "",
        **kwargs,
    ) -> None:
        """Update task status with state-machine validation.

        Fetches the current status, checks it against the formal state
        machine defined in :mod:`src.state_machine`, and logs a warning if
        the transition is not valid.  The update is **always applied**
        regardless of validation outcome (logging-only enforcement).

        This deliberate design choice keeps production running when edge
        cases produce unexpected transitions (e.g. a race between the
        orchestrator loop and a Discord command). The warnings surface in
        logs for investigation without blocking task progress.

        If *new_status* equals the current status, no transition validation
        occurs -- only the extra *kwargs* are applied (useful for updating
        metadata without changing state).

        Any extra *kwargs* (e.g. ``assigned_agent_id``, ``retry_count``,
        ``resume_after``) are forwarded to :meth:`update_task`.
        """
        task = await self.get_task(task_id)
        if task is None:
            logger.warning(
                "transition_task: task '%s' not found, cannot validate", task_id
            )
            # Still attempt the update in case of a race condition
            await self.update_task(task_id, status=new_status, **kwargs)
            return

        current_status = task.status

        if current_status == new_status:
            # Same-status "transition" — skip validation, just apply kwargs.
            if kwargs:
                await self.update_task(task_id, **kwargs)
            return

        if not is_valid_status_transition(current_status, new_status):
            ctx = f" ({context})" if context else ""
            logger.warning(
                "Invalid task status transition: %s -> %s for task '%s'%s",
                current_status.value,
                new_status.value,
                task_id,
                ctx,
            )

        await self.update_task(task_id, status=new_status, **kwargs)

    async def delete_task(self, task_id: str) -> None:
        await self._db.execute("DELETE FROM task_results WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM token_ledger WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_task_id = ?", [task_id, task_id])
        await self._db.execute("DELETE FROM task_criteria WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM task_context WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM task_tools WHERE task_id = ?", [task_id])
        await self._db.execute("DELETE FROM tasks WHERE id = ?", [task_id])
        await self._db.commit()

    async def get_task_updated_at(self, task_id: str) -> float | None:
        """Return the ``updated_at`` timestamp for a task, or *None*."""
        cursor = await self._db.execute(
            "SELECT updated_at FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row["updated_at"] if row else None

    async def get_task_created_at(self, task_id: str) -> float | None:
        """Return the ``created_at`` timestamp for a task, or *None*."""
        cursor = await self._db.execute(
            "SELECT created_at FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row["created_at"] if row else None

    async def get_subtasks(self, parent_task_id: str) -> list[Task]:
        cursor = await self._db.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ?", (parent_task_id,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row) -> Task:
        keys = row.keys()
        return Task(
            id=row["id"],
            project_id=row["project_id"],
            parent_task_id=row["parent_task_id"],
            repo_id=row["repo_id"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            status=TaskStatus(row["status"]),
            verification_type=VerificationType(row["verification_type"]),
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            assigned_agent_id=row["assigned_agent_id"],
            branch_name=row["branch_name"],
            resume_after=row["resume_after"],
            requires_approval=bool(row["requires_approval"]) if "requires_approval" in keys else False,
            pr_url=row["pr_url"] if "pr_url" in keys else None,
            plan_source=row["plan_source"] if "plan_source" in keys else None,
            is_plan_subtask=bool(row["is_plan_subtask"]) if "is_plan_subtask" in keys else False,
        )

    # --- Dependencies ---
    # Task dependency edges form a DAG. A task cannot be promoted from
    # DEFINED to READY until all of its upstream dependencies are COMPLETED.
    # The state_machine module provides cycle detection at creation time.

    async def add_dependency(self, task_id: str, depends_on: str) -> None:
        await self._db.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (task_id, depends_on),
        )
        await self._db.commit()

    async def get_dependencies(self, task_id: str) -> set[str]:
        cursor = await self._db.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return {r["depends_on_task_id"] for r in rows}

    async def get_all_dependencies(self) -> dict[str, set[str]]:
        cursor = await self._db.execute("SELECT * FROM task_dependencies")
        rows = await cursor.fetchall()
        deps: dict[str, set[str]] = {}
        for r in rows:
            deps.setdefault(r["task_id"], set()).add(r["depends_on_task_id"])
        return deps

    async def are_dependencies_met(self, task_id: str) -> bool:
        """Check whether all upstream dependencies of a task are satisfied.

        Returns True if every task that ``task_id`` depends on has reached
        COMPLETED status.  Also returns True if the task has no dependencies
        at all (vacuous truth).  This is the gate that controls the
        DEFINED -> READY promotion in the orchestrator loop.
        """
        cursor = await self._db.execute(
            "SELECT d.depends_on_task_id, t.status "
            "FROM task_dependencies d "
            "JOIN tasks t ON t.id = d.depends_on_task_id "
            "WHERE d.task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return all(r["status"] == TaskStatus.COMPLETED.value for r in rows)

    async def get_stuck_defined_tasks(self, threshold_seconds: int) -> list[Task]:
        """Return DEFINED tasks that are truly stuck — blocked by a dependency in
        a terminal failure state (BLOCKED or FAILED).

        A DEFINED task waiting on READY/IN_PROGRESS/DEFINED dependencies is normal
        and will eventually be promoted once the upstream work completes.  Only tasks
        whose dependency chain contains a BLOCKED or FAILED task are reported.
        """
        cursor = await self._db.execute(
            "SELECT DISTINCT t.* FROM tasks t "
            "JOIN task_dependencies d ON d.task_id = t.id "
            "JOIN tasks dep ON dep.id = d.depends_on_task_id "
            "WHERE t.status = ? AND dep.status IN (?, ?) "
            "ORDER BY t.created_at ASC",
            (
                TaskStatus.DEFINED.value,
                TaskStatus.BLOCKED.value,
                TaskStatus.FAILED.value,
            ),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task(r) for r in rows]

    async def get_blocking_dependencies(self, task_id: str) -> list[tuple[str, str, str]]:
        """Return (dep_task_id, dep_title, dep_status) for unmet dependencies.

        Only returns dependencies whose status is NOT COMPLETED.
        """
        cursor = await self._db.execute(
            "SELECT t.id, t.title, t.status "
            "FROM task_dependencies d "
            "JOIN tasks t ON t.id = d.depends_on_task_id "
            "WHERE d.task_id = ? AND t.status != ?",
            (task_id, TaskStatus.COMPLETED.value),
        )
        rows = await cursor.fetchall()
        return [(r["id"], r["title"], r["status"]) for r in rows]

    async def get_dependents(self, task_id: str) -> set[str]:
        """Return task IDs that directly depend on *task_id* (reverse lookup)."""
        cursor = await self._db.execute(
            "SELECT task_id FROM task_dependencies WHERE depends_on_task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return {r["task_id"] for r in rows}

    async def remove_dependency(self, task_id: str, depends_on: str) -> None:
        """Remove a single dependency edge."""
        await self._db.execute(
            "DELETE FROM task_dependencies "
            "WHERE task_id = ? AND depends_on_task_id = ?",
            (task_id, depends_on),
        )
        await self._db.commit()

    async def remove_all_dependencies_on(self, depends_on_task_id: str) -> None:
        """Remove all dependency edges pointing to a given task."""
        await self._db.execute(
            "DELETE FROM task_dependencies WHERE depends_on_task_id = ?",
            (depends_on_task_id,),
        )
        await self._db.commit()

    # --- Agents ---
    # Agent records represent running (or available) Claude Code processes.
    # The orchestrator tracks their state (IDLE, STARTING, BUSY, etc.),
    # heartbeat timestamps for liveness detection, and cumulative token usage.

    async def create_agent(self, agent: Agent) -> None:
        await self._db.execute(
            "INSERT INTO agents (id, name, agent_type, state, current_task_id, "
            "pid, last_heartbeat, total_tokens_used, "
            "session_tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent.id, agent.name, agent.agent_type,
             agent.state.value, agent.current_task_id,
             agent.pid, agent.last_heartbeat,
             agent.total_tokens_used, agent.session_tokens_used, time.time()),
        )
        await self._db.commit()

    async def get_agent(self, agent_id: str) -> Agent | None:
        cursor = await self._db.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_agent(row)

    async def list_agents(
        self, state: AgentState | None = None
    ) -> list[Agent]:
        if state:
            cursor = await self._db.execute(
                "SELECT * FROM agents WHERE state = ?", (state.value,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM agents")
        rows = await cursor.fetchall()
        return [self._row_to_agent(r) for r in rows]

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            if isinstance(value, AgentState):
                value = value.value
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(agent_id)
        await self._db.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    def _row_to_agent(self, row) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            agent_type=row["agent_type"],
            state=AgentState(row["state"]),
            current_task_id=row["current_task_id"],
            pid=row["pid"],
            last_heartbeat=row["last_heartbeat"],
            total_tokens_used=row["total_tokens_used"],
            session_tokens_used=row["session_tokens_used"],
        )

    # --- Agent Workspaces ---
    # Per-project workspace paths for agents. Replaces the old agent.checkout_path
    # (single value) and agent.repo_id fields.

    async def set_agent_workspace(
        self, agent_id: str, project_id: str, workspace_path: str,
        repo_id: str | None = None,
    ) -> None:
        """Set or update the workspace path for an agent in a specific project."""
        await self._db.execute(
            "INSERT INTO agent_workspaces (agent_id, project_id, workspace_path, "
            "repo_id, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id, project_id) DO UPDATE SET "
            "workspace_path = excluded.workspace_path, "
            "repo_id = excluded.repo_id",
            (agent_id, project_id, workspace_path, repo_id, time.time()),
        )
        await self._db.commit()

    async def get_agent_workspace(
        self, agent_id: str, project_id: str,
    ) -> AgentWorkspace | None:
        """Get the workspace for an agent in a specific project."""
        cursor = await self._db.execute(
            "SELECT * FROM agent_workspaces WHERE agent_id = ? AND project_id = ?",
            (agent_id, project_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return AgentWorkspace(
            agent_id=row["agent_id"],
            project_id=row["project_id"],
            workspace_path=row["workspace_path"],
            repo_id=row["repo_id"],
        )

    async def list_agent_workspaces(
        self, agent_id: str | None = None, project_id: str | None = None,
    ) -> list[AgentWorkspace]:
        """List agent workspaces, optionally filtered by agent or project."""
        conditions = []
        vals = []
        if agent_id:
            conditions.append("agent_id = ?")
            vals.append(agent_id)
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM agent_workspaces {where}", vals
        )
        rows = await cursor.fetchall()
        return [
            AgentWorkspace(
                agent_id=r["agent_id"],
                project_id=r["project_id"],
                workspace_path=r["workspace_path"],
                repo_id=r["repo_id"],
            )
            for r in rows
        ]

    async def delete_agent_workspaces(
        self, agent_id: str, project_id: str | None = None,
    ) -> None:
        """Delete workspace(s) for an agent, optionally scoped to a project."""
        if project_id:
            await self._db.execute(
                "DELETE FROM agent_workspaces WHERE agent_id = ? AND project_id = ?",
                (agent_id, project_id),
            )
        else:
            await self._db.execute(
                "DELETE FROM agent_workspaces WHERE agent_id = ?",
                (agent_id,),
            )
        await self._db.commit()

    # --- Token Ledger ---
    # Append-only log of token consumption. Each entry records which project,
    # agent, and task consumed how many tokens and when. The scheduler uses
    # windowed aggregates from this ledger to compute fair-share ratios.

    async def record_token_usage(
        self, project_id: str, agent_id: str, task_id: str, tokens: int
    ) -> None:
        await self._db.execute(
            "INSERT INTO token_ledger (id, project_id, agent_id, task_id, "
            "tokens_used, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), project_id, agent_id, task_id, tokens, time.time()),
        )
        await self._db.commit()

    async def get_project_token_usage(
        self, project_id: str, since: float | None = None
    ) -> int:
        if since:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) as total "
                "FROM token_ledger WHERE project_id = ? AND timestamp >= ?",
                (project_id, since),
            )
        else:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) as total "
                "FROM token_ledger WHERE project_id = ?",
                (project_id,),
            )
        row = await cursor.fetchone()
        return row["total"]

    # --- Task Results ---
    # Stores the outcome of each agent execution attempt. A task may have
    # multiple results if it was retried. Results include the success/failure
    # status, a summary, list of changed files, error details, and token cost.

    async def save_task_result(
        self, task_id: str, agent_id: str, output
    ) -> None:
        """Persist an AgentOutput to the task_results table."""
        await self._db.execute(
            "INSERT INTO task_results (id, task_id, agent_id, result, summary, "
            "files_changed, error_message, tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, agent_id, output.result.value,
             output.summary, json.dumps(output.files_changed),
             output.error_message, output.tokens_used, time.time()),
        )
        await self._db.commit()

    async def get_task_result(self, task_id: str) -> dict | None:
        """Return the most recent result for a task."""
        cursor = await self._db.execute(
            "SELECT * FROM task_results WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_task_result(row)

    async def get_task_results(self, task_id: str) -> list[dict]:
        """Return all results for a task (retry history)."""
        cursor = await self._db.execute(
            "SELECT * FROM task_results WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_task_result(r) for r in rows]

    def _row_to_task_result(self, row) -> dict:
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "result": row["result"],
            "summary": row["summary"],
            "files_changed": json.loads(row["files_changed"]),
            "error_message": row["error_message"],
            "tokens_used": row["tokens_used"],
            "created_at": row["created_at"],
        }

    # --- Delete Project (cascading) ---
    # Removes a project and all of its associated data across every table.
    # Order matters: child rows (results, dependencies, criteria, etc.) are
    # deleted before the parent task and project rows to satisfy FK constraints.

    async def delete_project(self, project_id: str) -> None:
        """Delete a project and all associated data (tasks, repos, results, ledger)."""
        # Get all task IDs for this project
        cursor = await self._db.execute(
            "SELECT id FROM tasks WHERE project_id = ?", (project_id,)
        )
        task_rows = await cursor.fetchall()
        task_ids = [r["id"] for r in task_rows]

        for tid in task_ids:
            await self._db.execute("DELETE FROM task_results WHERE task_id = ?", (tid,))
            await self._db.execute("DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_task_id = ?", (tid, tid))
            await self._db.execute("DELETE FROM task_criteria WHERE task_id = ?", (tid,))
            await self._db.execute("DELETE FROM task_context WHERE task_id = ?", (tid,))
            await self._db.execute("DELETE FROM task_tools WHERE task_id = ?", (tid,))

        await self._db.execute("DELETE FROM hook_runs WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM hooks WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM token_ledger WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM agent_workspaces WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM repos WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM events WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self._db.commit()

    # --- Events ---
    # Structured audit log. Every significant lifecycle event (task assigned,
    # completed, failed, etc.) is recorded here with optional JSON payload.
    # Used for debugging and the EventBus replay mechanism.

    async def log_event(
        self,
        event_type: str,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        payload: str | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO events (event_type, project_id, task_id, agent_id, "
            "payload, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (event_type, project_id, task_id, agent_id, payload, time.time()),
        )
        await self._db.commit()

    async def get_recent_events(self, limit: int = 50) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Hooks ---
    # Hooks are project-scoped automation rules: when a trigger fires (e.g.
    # task_completed), the hook engine gathers context, renders a prompt, and
    # optionally invokes an LLM. Hook definitions and their execution history
    # (hook_runs) are persisted here.

    async def create_hook(self, hook: Hook) -> None:
        now = time.time()
        await self._db.execute(
            "INSERT INTO hooks (id, project_id, name, enabled, trigger, "
            "context_steps, prompt_template, llm_config, cooldown_seconds, "
            "max_tokens_per_run, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hook.id, hook.project_id, hook.name, int(hook.enabled),
             hook.trigger, hook.context_steps, hook.prompt_template,
             hook.llm_config, hook.cooldown_seconds, hook.max_tokens_per_run,
             now, now),
        )
        await self._db.commit()

    async def get_hook(self, hook_id: str) -> Hook | None:
        cursor = await self._db.execute(
            "SELECT * FROM hooks WHERE id = ?", (hook_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_hook(row)

    async def list_hooks(
        self, project_id: str | None = None, enabled: bool | None = None
    ) -> list[Hook]:
        conditions = []
        vals = []
        if project_id:
            conditions.append("project_id = ?")
            vals.append(project_id)
        if enabled is not None:
            conditions.append("enabled = ?")
            vals.append(int(enabled))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT * FROM hooks {where}", vals
        )
        rows = await cursor.fetchall()
        return [self._row_to_hook(r) for r in rows]

    async def update_hook(self, hook_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            if key == "enabled":
                value = int(value)
            sets.append(f"{key} = ?")
            vals.append(value)
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(hook_id)
        await self._db.execute(
            f"UPDATE hooks SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    async def delete_hook(self, hook_id: str) -> None:
        await self._db.execute("DELETE FROM hook_runs WHERE hook_id = ?", (hook_id,))
        await self._db.execute("DELETE FROM hooks WHERE id = ?", (hook_id,))
        await self._db.commit()

    def _row_to_hook(self, row) -> Hook:
        return Hook(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            trigger=row["trigger"],
            context_steps=row["context_steps"],
            prompt_template=row["prompt_template"],
            llm_config=row["llm_config"],
            cooldown_seconds=row["cooldown_seconds"],
            max_tokens_per_run=row["max_tokens_per_run"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # --- Hook Runs ---
    # Execution history for hooks. Each run captures the trigger reason,
    # gathered context, rendered prompt, LLM response, and any actions taken.
    # Used for observability and cooldown enforcement.

    async def create_hook_run(self, run: HookRun) -> None:
        await self._db.execute(
            "INSERT INTO hook_runs (id, hook_id, project_id, trigger_reason, "
            "event_data, context_results, prompt_sent, llm_response, "
            "actions_taken, skipped_reason, tokens_used, status, started_at, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run.id, run.hook_id, run.project_id, run.trigger_reason,
             run.event_data, run.context_results, run.prompt_sent,
             run.llm_response, run.actions_taken, run.skipped_reason,
             run.tokens_used, run.status, run.started_at, run.completed_at),
        )
        await self._db.commit()

    async def update_hook_run(self, run_id: str, **kwargs) -> None:
        sets = []
        vals = []
        for key, value in kwargs.items():
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(run_id)
        await self._db.execute(
            f"UPDATE hook_runs SET {', '.join(sets)} WHERE id = ?", vals
        )
        await self._db.commit()

    async def get_last_hook_run(self, hook_id: str) -> HookRun | None:
        cursor = await self._db.execute(
            "SELECT * FROM hook_runs WHERE hook_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (hook_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_hook_run(row)

    async def list_hook_runs(
        self, hook_id: str, limit: int = 20
    ) -> list[HookRun]:
        cursor = await self._db.execute(
            "SELECT * FROM hook_runs WHERE hook_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (hook_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_hook_run(r) for r in rows]

    def _row_to_hook_run(self, row) -> HookRun:
        return HookRun(
            id=row["id"],
            hook_id=row["hook_id"],
            project_id=row["project_id"],
            trigger_reason=row["trigger_reason"],
            status=row["status"],
            event_data=row["event_data"],
            context_results=row["context_results"],
            prompt_sent=row["prompt_sent"],
            llm_response=row["llm_response"],
            actions_taken=row["actions_taken"],
            skipped_reason=row["skipped_reason"],
            tokens_used=row["tokens_used"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    # --- Atomic Operations ---
    # Multi-table writes that must succeed or fail together. These methods
    # perform all related updates within a single commit to avoid inconsistent
    # states between tasks, agents, and events.

    async def assign_task_to_agent(self, task_id: str, agent_id: str) -> None:
        """Atomically bind a task to an agent, updating both sides.

        This is the only method that should be used to start work on a task.
        In a single commit it:
        1. Transitions the task from READY to ASSIGNED and sets its
           ``assigned_agent_id``.
        2. Transitions the agent from IDLE to STARTING and sets its
           ``current_task_id``.
        3. Logs a ``task_assigned`` event for the audit trail.

        Performing all three writes in one commit prevents inconsistent
        states where a task thinks it is assigned but the agent does not
        (or vice versa).
        """
        # Validate the READY -> ASSIGNED transition
        task = await self.get_task(task_id)
        if task and not is_valid_status_transition(task.status, TaskStatus.ASSIGNED):
            logger.warning(
                "Invalid task status transition: %s -> ASSIGNED for task '%s' "
                "(assign_task_to_agent)",
                task.status.value,
                task_id,
            )

        now = time.time()
        await self._db.execute(
            "UPDATE tasks SET status = ?, assigned_agent_id = ?, updated_at = ? "
            "WHERE id = ?",
            (TaskStatus.ASSIGNED.value, agent_id, now, task_id),
        )
        await self._db.execute(
            "UPDATE agents SET state = ?, current_task_id = ? WHERE id = ?",
            (AgentState.STARTING.value, task_id, agent_id),
        )
        await self._db.execute(
            "INSERT INTO events (event_type, project_id, task_id, agent_id, "
            "timestamp) VALUES (?, (SELECT project_id FROM tasks WHERE id = ?), "
            "?, ?, ?)",
            ("task_assigned", task_id, task_id, agent_id, now),
        )
        await self._db.commit()
