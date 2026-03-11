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

import copy
import logging
import os
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """Raised when the application configuration fails validation checks.

    Contains a list of all validation errors found, not just the first one,
    so operators can fix all issues in one pass.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        msg = "Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        super().__init__(msg)


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
    rebase_between_subtasks: bool = False  # Rebase onto main between subtasks
    mid_chain_rebase: bool = True       # Rebase onto main between subtasks to reduce drift
    mid_chain_rebase_push: bool = False # Push rebased branch to remote between subtasks
    max_plan_depth: int = 1             # Max nesting of plan-generated tasks
    max_steps_per_plan: int = 5         # Cap phases from a single plan
    use_llm_parser: bool = False        # Use LLM (Claude) for plan parsing
    llm_parser_model: str = ""          # Model override for plan parsing


@dataclass
class ArchiveConfig:
    """Configuration for automatic archiving of terminal tasks.

    When enabled, the orchestrator automatically archives tasks that have
    been in a terminal status (COMPLETED, FAILED, BLOCKED) for longer than
    ``after_hours``.  This keeps the active task list clean without
    requiring manual ``/archive-tasks`` commands.
    """

    enabled: bool = True
    after_hours: float = 24.0  # Archive terminal tasks older than N hours
    statuses: list[str] = field(
        default_factory=lambda: ["COMPLETED", "FAILED", "BLOCKED"]
    )


@dataclass
class MonitoringConfig:
    """Configuration for monitoring stuck or stalled tasks."""
    stuck_task_threshold_seconds: int = 3600  # 1 hour default


@dataclass
class MemoryConfig:
    """Configuration for the semantic memory subsystem (memsearch).

    All fields have safe defaults — the subsystem is disabled unless
    ``enabled`` is explicitly set to ``True`` in the YAML config.
    See notes/memsearch-integration.md for full documentation.
    """

    enabled: bool = False
    embedding_provider: str = "openai"  # openai, google, voyage, ollama, local
    embedding_model: str = ""  # empty = provider default
    embedding_base_url: str = ""  # for Ollama or custom endpoints
    embedding_api_key: str = ""  # supports ${ENV_VAR} substitution
    milvus_uri: str = "~/.agent-queue/memsearch/milvus.db"  # file path or server URI
    milvus_token: str = ""
    max_chunk_size: int = 1500
    overlap_lines: int = 2
    auto_remember: bool = True  # auto-save task results as memories
    auto_recall: bool = True  # auto-inject memories at task start
    recall_top_k: int = 5  # number of memories to inject
    compact_enabled: bool = False  # periodic LLM compaction
    compact_interval_hours: int = 24
    index_notes: bool = True  # index project notes/ directory
    index_sessions: bool = False  # index session transcripts


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
    keep_alive: str = "1h"       # Ollama: how long to keep model loaded after last request


@dataclass
class LLMLoggingConfig:
    """Configuration for logging LLM inputs/outputs to JSONL files."""

    enabled: bool = False
    retention_days: int = 30


@dataclass
class AgentProfileConfig:
    """Configuration for an agent profile loaded from YAML.

    Profiles from YAML are synced to the database at startup. Profiles can
    also be created dynamically via Discord commands.
    """

    id: str = ""
    name: str = ""
    description: str = ""
    model: str = ""
    permission_mode: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    system_prompt_suffix: str = ""
    install: dict = field(default_factory=dict)


@dataclass
class HealthCheckConfig:
    """Configuration for the HTTP health check server.

    When enabled, the daemon exposes ``/health`` and ``/ready`` endpoints
    on the configured port for external monitoring and load balancer probes.
    """

    enabled: bool = False
    port: int = 8080


@dataclass
class AppConfig:
    """Top-level application configuration aggregating all subsystem configs.

    Instantiated once by load_config() at startup and threaded through to all
    major components. Each component reads only its relevant sub-config.

    The ``env`` field selects the environment profile (dev, staging, production).
    When set, ``load_config`` will look for an override file named
    ``config.{env}.yaml`` in the same directory as the main config file and
    deep-merge it over the base config.

    The ``validate()`` method performs fail-fast checks on critical settings.
    The ``reload_non_critical()`` method returns a fresh config with only
    non-critical settings updated from disk for hot-reloading.
    """

    workspace_dir: str = field(
        default_factory=lambda: os.path.expanduser("~/agent-queue-workspaces")
    )
    database_path: str = field(
        default_factory=lambda: os.path.expanduser("~/.agent-queue/agent-queue.db")
    )
    env: str = "production"
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    agents_config: AgentsDefaultConfig = field(default_factory=AgentsDefaultConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    pause_retry: PauseRetryConfig = field(default_factory=PauseRetryConfig)
    chat_provider: ChatProviderConfig = field(default_factory=ChatProviderConfig)
    hook_engine: HookEngineConfig = field(default_factory=HookEngineConfig)
    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    auto_task: AutoTaskConfig = field(default_factory=AutoTaskConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    llm_logging: LLMLoggingConfig = field(default_factory=LLMLoggingConfig)
    agent_profiles: list[AgentProfileConfig] = field(default_factory=list)
    global_token_budget_daily: int | None = None
    rate_limits: dict[str, dict[str, int]] = field(default_factory=dict)
    _config_path: str = field(default="", repr=False)

    def validate(self) -> None:
        """Validate critical configuration settings.

        Raises ConfigValidationError with all errors found (not just the first)
        so operators can fix everything in one pass.
        """
        errors: list[str] = []

        # Critical: paths
        if not self.workspace_dir:
            errors.append("workspace_dir is required")
        if not self.database_path:
            errors.append("database_path is required")

        # Scheduling sanity
        if self.scheduling.rolling_window_hours <= 0:
            errors.append("scheduling.rolling_window_hours must be > 0")

        # Pause/retry sanity
        if self.pause_retry.rate_limit_backoff_seconds <= 0:
            errors.append("pause_retry.rate_limit_backoff_seconds must be > 0")

        # Auto-task bounds
        if self.auto_task.max_plan_depth < 1:
            errors.append("auto_task.max_plan_depth must be >= 1")
        if self.auto_task.max_steps_per_plan < 1:
            errors.append("auto_task.max_steps_per_plan must be >= 1")

        # Archive sanity
        if self.archive.enabled and self.archive.after_hours <= 0:
            errors.append("archive.after_hours must be > 0 when archive is enabled")

        # Chat provider validation
        valid_providers = {"anthropic", "ollama"}
        if self.chat_provider.provider and self.chat_provider.provider not in valid_providers:
            errors.append(
                f"chat_provider.provider must be one of {valid_providers}, "
                f"got '{self.chat_provider.provider}'"
            )

        # Memory embedding provider validation
        if self.memory.enabled:
            valid_embedding = {"openai", "google", "voyage", "ollama", "local"}
            if self.memory.embedding_provider not in valid_embedding:
                errors.append(
                    f"memory.embedding_provider must be one of {valid_embedding}, "
                    f"got '{self.memory.embedding_provider}'"
                )

        # Health check port range
        if self.health_check.enabled:
            if not (1 <= self.health_check.port <= 65535):
                errors.append(
                    f"health_check.port must be between 1 and 65535, "
                    f"got {self.health_check.port}"
                )

        # Monitoring threshold
        if self.monitoring.stuck_task_threshold_seconds < 0:
            errors.append("monitoring.stuck_task_threshold_seconds must be >= 0")

        # Agent profile IDs must be non-empty
        for profile in self.agent_profiles:
            if not profile.id:
                errors.append(
                    f"agent_profiles: profile with name '{profile.name}' has an empty id"
                )

        # Rate limits structure validation
        for scope, limits in self.rate_limits.items():
            if not isinstance(limits, dict):
                errors.append(
                    f"rate_limits.{scope}: expected a dict, got {type(limits).__name__}"
                )

        if errors:
            raise ConfigValidationError(errors)

    def reload_non_critical(self) -> "AppConfig":
        """Return a new AppConfig with non-critical settings refreshed from disk.

        Non-critical settings (safe to change at runtime without restart):
        - scheduling, pause_retry, auto_task, archive, monitoring
        - hook_engine, llm_logging

        Critical settings (NOT reloaded — require restart):
        - discord, database_path, workspace_dir, chat_provider, memory,
          health_check

        Returns a new AppConfig instance; the caller is responsible for
        swapping references.  If the config file cannot be read or parsed,
        the current config is returned unchanged and the error is logged.
        """
        if not self._config_path or not os.path.exists(self._config_path):
            return self

        try:
            fresh = load_config(self._config_path)
        except Exception as e:
            logger.warning("Config hot-reload failed, keeping current config: %s", e)
            return self

        # Create a copy of current config and update only non-critical sections
        updated = copy.deepcopy(self)
        updated.scheduling = fresh.scheduling
        updated.pause_retry = fresh.pause_retry
        updated.auto_task = fresh.auto_task
        updated.archive = fresh.archive
        updated.monitoring = fresh.monitoring
        updated.hook_engine = fresh.hook_engine
        updated.llm_logging = fresh.llm_logging

        return updated


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


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins on conflict).

    Used for environment-specific config overlays: values in the overlay
    file take precedence, but keys only present in the base are preserved.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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

    Processing order:
      1. Load ``.env`` from the config file's directory (without overriding
         existing env vars)
      2. Parse the base YAML file
      3. Determine the environment profile (``AGENT_QUEUE_ENV`` env var,
         or ``env`` field in config, default ``"production"``)
      4. If an overlay file ``config.{env}.yaml`` exists in the same
         directory, deep-merge it over the base config
      5. Recursively substitute ``${ENV_VAR}`` references in all strings
      6. Map sections into typed dataclass instances
      7. Run ``validate()`` to catch misconfiguration early
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    _load_env_file(path)

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    # Determine environment profile for overlay loading
    env = os.environ.get("AGENT_QUEUE_ENV", raw.get("env", "production"))

    # Load environment-specific overlay (e.g. config.dev.yaml)
    config_dir = os.path.dirname(path)
    base_name = os.path.basename(path)
    name_part, ext = os.path.splitext(base_name)
    overlay_path = os.path.join(config_dir, f"{name_part}.{env}{ext}")
    if os.path.exists(overlay_path):
        with open(overlay_path) as f:
            overlay = yaml.safe_load(f) or {}
        raw = _deep_merge(raw, overlay)

    raw = _process_values(raw)

    config = AppConfig()
    config._config_path = path
    config.env = env

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
            keep_alive=cp.get("keep_alive", "1h"),
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

    if "archive" in raw:
        ar = raw["archive"]
        config.archive = ArchiveConfig(
            enabled=ar.get("enabled", True),
            after_hours=float(ar.get("after_hours", 24.0)),
            statuses=ar.get("statuses", ["COMPLETED", "FAILED", "BLOCKED"]),
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
            rebase_between_subtasks=at.get("rebase_between_subtasks", False),
            mid_chain_rebase=at.get("mid_chain_rebase", True),
            mid_chain_rebase_push=at.get("mid_chain_rebase_push", False),
            max_plan_depth=at.get("max_plan_depth", 1),
            max_steps_per_plan=at.get("max_steps_per_plan", 5),
            use_llm_parser=at.get("use_llm_parser", False),
            llm_parser_model=at.get("llm_parser_model", ""),
        )

    if "memory" in raw:
        mem = raw["memory"]
        config.memory = MemoryConfig(
            enabled=mem.get("enabled", False),
            embedding_provider=mem.get("embedding_provider", "openai"),
            embedding_model=mem.get("embedding_model", ""),
            embedding_base_url=mem.get("embedding_base_url", ""),
            embedding_api_key=mem.get("embedding_api_key", ""),
            milvus_uri=mem.get("milvus_uri", "~/.agent-queue/memsearch/milvus.db"),
            milvus_token=mem.get("milvus_token", ""),
            max_chunk_size=mem.get("max_chunk_size", 1500),
            overlap_lines=mem.get("overlap_lines", 2),
            auto_remember=mem.get("auto_remember", True),
            auto_recall=mem.get("auto_recall", True),
            recall_top_k=mem.get("recall_top_k", 5),
            compact_enabled=mem.get("compact_enabled", False),
            compact_interval_hours=mem.get("compact_interval_hours", 24),
            index_notes=mem.get("index_notes", True),
            index_sessions=mem.get("index_sessions", False),
        )

    if "llm_logging" in raw:
        ll = raw["llm_logging"]
        config.llm_logging = LLMLoggingConfig(
            enabled=ll.get("enabled", False),
            retention_days=ll.get("retention_days", 30),
        )

    if "agent_profiles" in raw:
        profiles = []
        for pid, pdata in raw["agent_profiles"].items():
            if not isinstance(pdata, dict):
                continue
            profiles.append(AgentProfileConfig(
                id=pid,
                name=pdata.get("name", pid),
                description=pdata.get("description", ""),
                model=pdata.get("model", ""),
                permission_mode=pdata.get("permission_mode", ""),
                allowed_tools=pdata.get("allowed_tools", []),
                mcp_servers=pdata.get("mcp_servers", {}),
                system_prompt_suffix=pdata.get("system_prompt_suffix", ""),
                install=pdata.get("install", {}),
            ))
        config.agent_profiles = profiles

    if "health_check" in raw:
        hc = raw["health_check"]
        config.health_check = HealthCheckConfig(
            enabled=hc.get("enabled", False),
            port=hc.get("port", 8080),
        )

    if "rate_limits" in raw:
        config.rate_limits = raw["rate_limits"]

    # Fail fast on misconfiguration — surface all errors at once.
    config.validate()

    return config
