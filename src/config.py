from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml


@dataclass
class DiscordConfig:
    bot_token: str = ""
    guild_id: str = ""
    channels: dict[str, str] = field(default_factory=lambda: {
        "control": "control",
        "notifications": "notifications",
        "agent_questions": "agent-questions",
    })
    authorized_users: list[str] = field(default_factory=list)


@dataclass
class NLParserConfig:
    model: str = "claude-haiku"
    max_tokens: int = 500


@dataclass
class AgentsDefaultConfig:
    heartbeat_interval_seconds: int = 30
    stuck_timeout_seconds: int = 600
    graceful_shutdown_timeout_seconds: int = 30


@dataclass
class SchedulingConfig:
    rolling_window_hours: int = 24
    min_task_guarantee: bool = True


@dataclass
class PauseRetryConfig:
    rate_limit_backoff_seconds: int = 60
    token_exhaustion_retry_seconds: int = 300
    # Exponential-backoff retry knobs (in-process, before the task is paused)
    rate_limit_max_retries: int = 3
    rate_limit_max_backoff_seconds: int = 300


@dataclass
class ChatProviderConfig:
    provider: str = "anthropic"  # "anthropic" or "ollama"
    model: str = ""              # Empty = provider default
    base_url: str = ""           # For Ollama


@dataclass
class AppConfig:
    workspace_dir: str = field(
        default_factory=lambda: os.path.expanduser("~/agent-queue-workspaces")
    )
    database_path: str = field(
        default_factory=lambda: os.path.expanduser("~/.agent-queue/agent-queue.db")
    )
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    nl_parser: NLParserConfig = field(default_factory=NLParserConfig)
    agents_config: AgentsDefaultConfig = field(default_factory=AgentsDefaultConfig)
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    pause_retry: PauseRetryConfig = field(default_factory=PauseRetryConfig)
    chat_provider: ChatProviderConfig = field(default_factory=ChatProviderConfig)
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
        config.discord = DiscordConfig(
            bot_token=d.get("bot_token", ""),
            guild_id=d.get("guild_id", ""),
            channels=d.get("channels", config.discord.channels),
            authorized_users=d.get("authorized_users", []),
        )

    if "nl_parser" in raw:
        n = raw["nl_parser"]
        config.nl_parser = NLParserConfig(
            model=n.get("model", "claude-haiku"),
            max_tokens=n.get("max_tokens", 500),
        )

    if "agents" in raw:
        a = raw["agents"]
        config.agents_config = AgentsDefaultConfig(
            heartbeat_interval_seconds=a.get("heartbeat_interval_seconds", 30),
            stuck_timeout_seconds=a.get("stuck_timeout_seconds", 600),
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

    if "rate_limits" in raw:
        config.rate_limits = raw["rate_limits"]

    return config
