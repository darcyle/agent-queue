"""YAML configuration loading with environment variable substitution.

Loads the application config from a YAML file (default: ~/.agent-queue/config.yaml),
substitutes ${ENV_VAR} references with environment variable values, and maps
the result into typed dataclass instances. Also supports loading a .env file
from the same directory as the config file for local development.

The config is loaded once at startup and passed to all major components
(orchestrator, Discord bot, scheduler, adapters). Individual sections are
represented by dedicated dataclasses so each component can accept only the
config it needs.

See specs/config.md for the full specification of all configuration fields.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml


@dataclass
class PerProjectChannelsConfig:
    """Configuration for automatic per-project Discord channel management."""

    auto_create: bool = False
    naming_convention: str = "{project_id}"
    category_name: str = ""  # Discord category to group project channels (optional)
    private: bool = True  # Make auto-created channels private (only bot + permitted users)


@dataclass
class DiscordConfig:
    """Discord bot connection and channel routing settings."""

    bot_token: str = ""
    guild_id: str = ""
    channels: dict[str, str] = field(default_factory=lambda: {
        "channel": "agent-queue",
        "agent_questions": "agent-questions",
    })
    authorized_users: list[str] = field(default_factory=list)
    per_project_channels: PerProjectChannelsConfig = field(
        default_factory=PerProjectChannelsConfig
    )


@dataclass
class AgentsDefaultConfig:
    """Default timeouts for agent health monitoring and graceful shutdown."""

    heartbeat_interval_seconds: int = 30
    stuck_timeout_seconds: int = 0  # 0 = no timeout (was 600)
    graceful_shutdown_timeout_seconds: int = 30


@dataclass
class SchedulingConfig:
    """Controls how the scheduler distributes agent capacity across projects.

    rolling_window_hours defines the lookback period for proportional credit
    accounting. min_task_guarantee ensures every active project gets at least
    one task slot regardless of credit balance.
    """

    rolling_window_hours: int = 24
    min_task_guarantee: bool = True


@dataclass
class PauseRetryConfig:
    """Backoff and retry timing for rate-limited and token-exhausted tasks.

    Controls both the in-process exponential backoff (before a task is paused)
    and the longer pause durations (after a task enters PAUSED state and waits
    for resume_after to elapse).
    """

    rate_limit_backoff_seconds: int = 60
    token_exhaustion_retry_seconds: int = 300
    # Exponential-backoff retry knobs (in-process, before the task is paused)
    rate_limit_max_retries: int = 3
    rate_limit_max_backoff_seconds: int = 300


@dataclass
class AutoTaskConfig:
    """Configuration for auto-generating tasks from implementation plans."""

    enabled: bool = True
    plan_file_patterns: list[str] = field(default_factory=lambda: [
        ".claude/plan.md",
        "plan.md",
        "docs/plans/*.md",
        "plans/*.md",
        "docs/plan.md",
    ])
    inherit_repo: bool = True           # Subtasks inherit parent's repo_id
    inherit_approval: bool = True       # Subtasks inherit parent's requires_approval
    base_priority: int = 100            # Base priority for generated tasks
    chain_dependencies: bool = True     # Tasks depend on previous step
    max_plan_depth: int = 1             # Max nesting of plan-generated tasks
    max_steps_per_plan: int = 20        # Cap steps from a single plan
    use_llm_parser: bool = False        # Use LLM (Claude) for plan parsing
    llm_parser_model: str = ""          # Model override for plan parsing


@dataclass
class MonitoringConfig:
    """Configuration for monitoring stuck or stalled tasks."""
    stuck_task_threshold_seconds: int = 3600  # 1 hour default


@dataclass
class HookEngineConfig:
    enabled: bool = True
    max_concurrent_hooks: int = 2


@dataclass
class ChatProviderConfig:
    """LLM provider settings for the Discord chat agent (not the coding agents)."""

    provider: str = "anthropic"  # "anthropic" or "ollama"
    model: str = ""              # Empty = provider default
    base_url: str = ""           # For Ollama


@dataclass
class LLMLoggingConfig:
    """Configuration for logging LLM inputs/outputs to JSONL files."""

    enabled: bool = False
    retention_days: int = 30


@dataclass
class AppConfig:
    """Top-level application configuration aggregating all subsystem configs.

    Instantiated once by load_config() at startup and threaded through to all
    major components. Each component reads only its relevant sub-config.
    """

    workspace_dir: str = field(
        default_factory=lambda: os.path.expanduser("~/agent-queue-workspaces")
    )
    database_path: str = field(
        default_factory=lambda: os.path.expanduser("~/.agent-queue/agent-queue.db")
    )
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    agents_config: AgentsDefaultConfig = field(default_factory=AgentsDefaultConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    pause_retry: PauseRetryConfig = field(default_factory=PauseRetryConfig)
    chat_provider: ChatProviderConfig = field(default_factory=ChatProviderConfig)
    hook_engine: HookEngineConfig = field(default_factory=HookEngineConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    auto_task: AutoTaskConfig = field(default_factory=AutoTaskConfig)
    llm_logging: LLMLoggingConfig = field(default_factory=LLMLoggingConfig)
    global_token_budget_daily: int | None = None
    rate_limits: dict[str, dict[str, int]] = field(default_factory=dict)


def _substitute_env_vars(value: str) -> str:
    """Replace ${ENV_VAR} with environment variable values."""
    def replacer(match):
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            raise ValueError(f"Environment variable {var_name} not set")
        return env_val
    return re.sub(r"\$\{(\w+)\}", replacer, value)


def _process_values(obj):
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _process_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_process_values(v) for v in obj]
    return obj


def _load_env_file(config_path: str) -> None:
    """Load .env file from the same directory as the config file."""
    env_path = os.path.join(os.path.dirname(config_path), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                # Don't override existing env vars
                if key and key not in os.environ:
                    os.environ[key] = value


def load_config(path: str) -> AppConfig:
    """Load and validate application configuration from a YAML file.

    Processing order: load .env from the config file's directory (without
    overriding existing env vars), parse YAML, recursively substitute
    ${ENV_VAR} references in all string values, then map sections into
    typed dataclass instances with sensible defaults for missing fields.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    _load_env_file(path)

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    raw = _process_values(raw)

    config = AppConfig()

    if "workspace_dir" in raw:
        config.workspace_dir = raw["workspace_dir"]
    if "database_path" in raw:
        config.database_path = raw["database_path"]
    if "global_token_budget_daily" in raw:
        config.global_token_budget_daily = raw["global_token_budget_daily"]

    if "discord" in raw:
        d = raw["discord"]
        ppc = PerProjectChannelsConfig()
        if "per_project_channels" in d:
            pp = d["per_project_channels"]
            ppc = PerProjectChannelsConfig(
                auto_create=pp.get("auto_create", False),
                naming_convention=pp.get(
                    "naming_convention", "{project_id}"
                ),
                category_name=pp.get("category_name", ""),
                private=pp.get("private", True),
            )
        # Backward compat: if old config has separate control/notifications,
        # merge into single "channel" entry (prefer control since that's where
        # the bot listens for chat).
        raw_channels = d.get("channels", config.discord.channels)
        if "channel" not in raw_channels and ("control" in raw_channels or "notifications" in raw_channels):
            merged_name = raw_channels.get("control") or raw_channels.get("notifications", "agent-queue")
            raw_channels = {
                "channel": merged_name,
                "agent_questions": raw_channels.get("agent_questions", "agent-questions"),
            }
        config.discord = DiscordConfig(
            bot_token=d.get("bot_token", ""),
            guild_id=d.get("guild_id", ""),
            channels=raw_channels,
            authorized_users=d.get("authorized_users", []),
            per_project_channels=ppc,
        )

    if "agents" in raw:
        a = raw["agents"]
        config.agents_config = AgentsDefaultConfig(
            heartbeat_interval_seconds=a.get("heartbeat_interval_seconds", 30),
            stuck_timeout_seconds=a.get("stuck_timeout_seconds", 0),
            graceful_shutdown_timeout_seconds=a.get(
                "graceful_shutdown_timeout_seconds", 30
            ),
        )

    if "scheduling" in raw:
        s = raw["scheduling"]
        config.scheduling = SchedulingConfig(
            rolling_window_hours=s.get("rolling_window_hours", 24),
            min_task_guarantee=s.get("min_task_guarantee", True),
        )

    if "pause_retry" in raw:
        p = raw["pause_retry"]
        config.pause_retry = PauseRetryConfig(
            rate_limit_backoff_seconds=p.get("rate_limit_backoff_seconds", 60),
            token_exhaustion_retry_seconds=p.get(
                "token_exhaustion_retry_seconds", 300
            ),
            rate_limit_max_retries=p.get("rate_limit_max_retries", 3),
            rate_limit_max_backoff_seconds=p.get("rate_limit_max_backoff_seconds", 300),
        )

    if "chat_provider" in raw:
        cp = raw["chat_provider"]
        config.chat_provider = ChatProviderConfig(
            provider=cp.get("provider", "anthropic"),
            model=cp.get("model", ""),
            base_url=cp.get("base_url", ""),
        )

    if "hook_engine" in raw:
        h = raw["hook_engine"]
        config.hook_engine = HookEngineConfig(
            enabled=h.get("enabled", True),
            max_concurrent_hooks=h.get("max_concurrent_hooks", 2),
        )

    if "monitoring" in raw:
        m = raw["monitoring"]
        config.monitoring = MonitoringConfig(
            stuck_task_threshold_seconds=m.get(
                "stuck_task_threshold_seconds", 3600
            ),
        )

    if "auto_task" in raw:
        at = raw["auto_task"]
        config.auto_task = AutoTaskConfig(
            enabled=at.get("enabled", True),
            plan_file_patterns=at.get("plan_file_patterns", [
                ".claude/plan.md", "plan.md",
                "docs/plans/*.md", "plans/*.md", "docs/plan.md",
            ]),
            inherit_repo=at.get("inherit_repo", True),
            inherit_approval=at.get("inherit_approval", True),
            base_priority=at.get("base_priority", 100),
            chain_dependencies=at.get("chain_dependencies", True),
            max_plan_depth=at.get("max_plan_depth", 1),
            max_steps_per_plan=at.get("max_steps_per_plan", 20),
            use_llm_parser=at.get("use_llm_parser", False),
            llm_parser_model=at.get("llm_parser_model", ""),
        )

    if "llm_logging" in raw:
        ll = raw["llm_logging"]
        config.llm_logging = LLMLoggingConfig(
            enabled=ll.get("enabled", False),
            retention_days=ll.get("retention_days", 30),
        )

    if "rate_limits" in raw:
        config.rate_limits = raw["rate_limits"]

    return config
