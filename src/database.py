from __future__ import annotations

import json
import time
import uuid

import aiosqlite

from src.models import (
    Agent, AgentState, Hook, HookRun, Project, ProjectStatus, RepoConfig,
    RepoSourceType, Task, TaskStatus, VerificationType,
)

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
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # --- Projects ---

    async def create_project(self, project: Project) -> None:
        await self._db.execute(
            "INSERT INTO projects (id, name, credit_weight, max_concurrent_agents, "
            "status, total_tokens_used, budget_limit, workspace_path, "
            "discord_channel_id, discord_control_channel_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project.id, project.name, project.credit_weight,
             project.max_concurrent_agents, project.status.value,
             project.total_tokens_used, project.budget_limit,
             project.workspace_path, project.discord_channel_id,
             project.discord_control_channel_id, time.time()),
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
        return Project(
            id=row["id"],
            name=row["name"],
            credit_weight=row["credit_weight"],
            max_concurrent_agents=row["max_concurrent_agents"],
            status=ProjectStatus(row["status"]),
            total_tokens_used=row["total_tokens_used"],
            budget_limit=row["budget_limit"],
            workspace_path=row["workspace_path"] if "workspace_path" in keys else None,
            discord_channel_id=row["discord_channel_id"] if "discord_channel_id" in keys else None,
            discord_control_channel_id=row["discord_control_channel_id"] if "discord_control_channel_id" in keys else None,
        )

    # --- Repos ---

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
            default_branch=row["default_branch"],
            checkout_base_path=row["checkout_base_path"],
        )

    # --- Tasks ---

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
        cursor = await self._db.execute(
            "SELECT d.depends_on_task_id, t.status "
            "FROM task_dependencies d "
            "JOIN tasks t ON t.id = d.depends_on_task_id "
            "WHERE d.task_id = ?",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return all(r["status"] == TaskStatus.COMPLETED.value for r in rows)

    # --- Agents ---

    async def create_agent(self, agent: Agent) -> None:
        await self._db.execute(
            "INSERT INTO agents (id, name, agent_type, state, current_task_id, "
            "checkout_path, repo_id, pid, last_heartbeat, total_tokens_used, "
            "session_tokens_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent.id, agent.name, agent.agent_type,
             agent.state.value, agent.current_task_id,
             agent.checkout_path, agent.repo_id, agent.pid, agent.last_heartbeat,
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
            checkout_path=row["checkout_path"],
            repo_id=row["repo_id"] if "repo_id" in row.keys() else None,
            pid=row["pid"],
            last_heartbeat=row["last_heartbeat"],
            total_tokens_used=row["total_tokens_used"],
            session_tokens_used=row["session_tokens_used"],
        )

    # --- Token Ledger ---

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
        await self._db.execute("DELETE FROM repos WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM events WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self._db.commit()

    # --- Events ---

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

    # --- Atomic Operations ---

    # --- Hooks ---

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

    async def assign_task_to_agent(self, task_id: str, agent_id: str) -> None:
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
