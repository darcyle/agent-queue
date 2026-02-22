from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    DEFINED = "DEFINED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_INPUT = "WAITING_INPUT"
    PAUSED = "PAUSED"
    VERIFYING = "VERIFYING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskEvent(Enum):
    DEPS_MET = "DEPS_MET"
    ASSIGNED = "ASSIGNED"
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    TOKENS_EXHAUSTED = "TOKENS_EXHAUSTED"
    AGENT_QUESTION = "AGENT_QUESTION"
    HUMAN_REPLIED = "HUMAN_REPLIED"
    INPUT_TIMEOUT = "INPUT_TIMEOUT"
    RESUME_TIMER = "RESUME_TIMER"
    VERIFY_PASSED = "VERIFY_PASSED"
    VERIFY_FAILED = "VERIFY_FAILED"
    PR_CREATED = "PR_CREATED"
    PR_MERGED = "PR_MERGED"
    RETRY = "RETRY"
    MAX_RETRIES = "MAX_RETRIES"


class AgentState(Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    BUSY = "BUSY"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class AgentResult(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED_TOKENS = "paused_tokens"
    PAUSED_RATE_LIMIT = "paused_rate_limit"


class ProjectStatus(Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class VerificationType(Enum):
    AUTO_TEST = "auto_test"
    QA_AGENT = "qa_agent"
    HUMAN = "human"


class RepoSourceType(Enum):
    CLONE = "clone"
    LINK = "link"
    INIT = "init"


@dataclass
class RepoConfig:
    id: str
    project_id: str
    source_type: RepoSourceType
    url: str = ""
    source_path: str = ""
    default_branch: str = "main"
    checkout_base_path: str = ""


@dataclass
class Project:
    id: str
    name: str
    credit_weight: float = 1.0
    max_concurrent_agents: int = 2
    status: ProjectStatus = ProjectStatus.ACTIVE
    total_tokens_used: int = 0
    budget_limit: int | None = None
    workspace_path: str | None = None


@dataclass
class Task:
    id: str
    project_id: str
    title: str
    description: str
    priority: int = 100
    status: TaskStatus = TaskStatus.DEFINED
    verification_type: VerificationType = VerificationType.AUTO_TEST
    retry_count: int = 0
    max_retries: int = 3
    parent_task_id: str | None = None
    repo_id: str | None = None
    assigned_agent_id: str | None = None
    branch_name: str | None = None
    resume_after: float | None = None  # unix timestamp
    requires_approval: bool = False
    pr_url: str | None = None


@dataclass
class Agent:
    id: str
    name: str
    agent_type: str  # "claude", "codex", "cursor", "aider"
    state: AgentState = AgentState.IDLE
    current_task_id: str | None = None
    checkout_path: str | None = None
    repo_id: str | None = None
    pid: int | None = None
    last_heartbeat: float | None = None
    total_tokens_used: int = 0
    session_tokens_used: int = 0


@dataclass
class TaskContext:
    description: str
    acceptance_criteria: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    checkout_path: str = ""
    branch_name: str = ""
    attached_context: list[str] = field(default_factory=list)
    mcp_servers: list[dict] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


@dataclass
class AgentOutput:
    result: AgentResult
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    tokens_used: int = 0
    error_message: str | None = None


@dataclass
class Hook:
    id: str
    project_id: str
    name: str
    enabled: bool = True
    trigger: str = '{}'          # JSON: {"type": "periodic", "interval_seconds": 7200}
    context_steps: str = '[]'    # JSON array of step configs
    prompt_template: str = ''    # Template with {{step_0}}, {{event}} placeholders
    llm_config: str | None = None  # JSON: {"provider": "anthropic", "model": "..."}
    cooldown_seconds: int = 3600
    max_tokens_per_run: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class HookRun:
    id: str
    hook_id: str
    project_id: str
    trigger_reason: str          # "periodic", "cron", "event:task_completed", "manual"
    status: str = 'running'      # running, completed, failed, skipped
    event_data: str | None = None
    context_results: str | None = None
    prompt_sent: str | None = None
    llm_response: str | None = None
    actions_taken: str | None = None
    skipped_reason: str | None = None
    tokens_used: int = 0
    started_at: float = 0.0
    completed_at: float | None = None
