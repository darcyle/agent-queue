#!/usr/bin/env python3
"""Interactive setup wizard for agent-queue."""

from __future__ import annotations

import asyncio
import getpass
import os
import subprocess
import sys
from pathlib import Path

# ANSI colors
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
DIM = "\033[2m"
RESET = "\033[0m"


def banner():
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════╗
║        agent-queue setup wizard      ║
╚══════════════════════════════════════╝{RESET}
""")


def step_header(num: int, title: str):
    print(f"\n{BOLD}{CYAN}── Step {num}: {title} ──{RESET}\n")


def success(msg: str):
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str):
    print(f"  {YELLOW}!{RESET} {msg}")


def error(msg: str):
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str):
    print(f"  {DIM}{msg}{RESET}")


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"  {label}{suffix}: ").strip()
    return value or default


def prompt_yes_no(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"  {label} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def prompt_secret(label: str, existing: str = "") -> str:
    if existing:
        masked = existing[:4] + "..." + existing[-4:] if len(existing) > 12 else "****"
        value = input(f"  {label} [{masked}]: ").strip()
        return value if value else existing
    return getpass.getpass(f"  {label}: ").strip()


# ── Load existing config ────────────────────────────────────────────────────


def _load_existing_config() -> dict:
    """Load existing config.yaml and .env into a dict for pre-filling prompts."""
    existing: dict = {}
    config_dir = Path(os.path.expanduser("~/.agent-queue"))

    # Load .env
    env_path = config_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()

    # Load config.yaml (simple key-value parsing, no PyYAML dependency)
    config_path = config_dir / "config.yaml"
    if config_path.exists():
        try:
            import yaml

            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            existing["_yaml"] = data
        except ImportError:
            # Fall back to simple line parsing
            data: dict = {}
            current_section: str | None = None
            current_subsection: str | None = None
            for line in config_path.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                if indent == 0 and ":" in stripped:
                    key, _, val = stripped.partition(":")
                    val = val.strip()
                    if val:
                        data[key.strip()] = val.strip('"').strip("'")
                    else:
                        current_section = key.strip()
                        current_subsection = None
                        data.setdefault(current_section, {})
                elif indent > 0 and current_section and ":" in stripped:
                    key, _, val = stripped.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if val:
                        if isinstance(data[current_section], dict):
                            data[current_section][key] = val.strip('"').strip("'")
                    else:
                        current_subsection = key
                        if isinstance(data[current_section], dict):
                            data[current_section].setdefault(current_subsection, {})
                    if current_subsection and val and isinstance(data[current_section], dict):
                        subsec = data[current_section].get(current_subsection)
                        if isinstance(subsec, dict):
                            subsec[key] = val.strip('"').strip("'")
            existing["_yaml"] = data

    return existing


# ── Step 1: Directories ──────────────────────────────────────────────────────


def step_directories(existing: dict) -> tuple[str, str]:
    step_header(1, "Workspace & Database")

    yaml_cfg = existing.get("_yaml", {})
    default_workspace = yaml_cfg.get("workspace_dir", os.path.expanduser("~/agent-queue-workspaces"))
    default_db = yaml_cfg.get("database_path", os.path.expanduser("~/.agent-queue/agent-queue.db"))

    workspace = prompt("Workspace directory", default_workspace)
    workspace = os.path.expanduser(workspace)

    db_path = prompt("Database path", default_db)
    db_path = os.path.expanduser(db_path)

    for d in [workspace, os.path.dirname(db_path)]:
        os.makedirs(d, exist_ok=True)
        success(f"Directory ready: {d}")

    return workspace, db_path


# ── Step 2: Discord ──────────────────────────────────────────────────────────


def step_discord(existing: dict) -> dict:
    step_header(2, "Discord Bot")

    yaml_cfg = existing.get("_yaml", {})
    discord_cfg = yaml_cfg.get("discord", {})
    existing_channels = discord_cfg.get("channels", {})

    print(f"""  To create a Discord bot:
  1. Go to {BOLD}https://discord.com/developers/applications{RESET}
  2. Click {BOLD}New Application{RESET}, give it a name, and create it
  3. Go to the {BOLD}Bot{RESET} tab and click {BOLD}Reset Token{RESET} to get your bot token
  4. Under {BOLD}Privileged Gateway Intents{RESET}, enable {BOLD}Message Content Intent{RESET}
  5. Go to {BOLD}OAuth2 > URL Generator{RESET}:
     - Scopes: {BOLD}bot{RESET}, {BOLD}applications.commands{RESET}
     - Bot Permissions: {BOLD}Send Messages{RESET}, {BOLD}Read Message History{RESET}, {BOLD}Use Slash Commands{RESET}
  6. Copy the generated URL, open it in your browser, and add the bot to your server
""")

    existing_token = existing.get("DISCORD_BOT_TOKEN", "")
    bot_token = prompt_secret("Bot token", existing_token)
    if not bot_token:
        error("Bot token is required")
        sys.exit(1)

    print(f"""
  To find your Guild (server) ID:
  1. Open Discord Settings > {BOLD}Advanced{RESET} > enable {BOLD}Developer Mode{RESET}
  2. Right-click your server name and click {BOLD}Copy Server ID{RESET}
""")

    existing_guild = discord_cfg.get("guild_id", "")
    guild_id = prompt("Guild ID", existing_guild)
    if not guild_id:
        error("Guild ID is required")
        sys.exit(1)

    # Channel names (before connectivity test)
    channels = {
        "control": existing_channels.get("control", "control"),
        "notifications": existing_channels.get("notifications", "notifications"),
        "agent_questions": existing_channels.get("agent_questions", "agent-questions"),
    }
    if prompt_yes_no("Customize channel names?", default=False):
        channels["control"] = prompt("Control channel", channels["control"])
        channels["notifications"] = prompt("Notifications channel", channels["notifications"])
        channels["agent_questions"] = prompt(
            "Agent questions channel", channels["agent_questions"]
        )

    # Test connectivity with retry loop (including channel verification)
    print()
    info("Testing Discord connectivity...")
    discord_ok = False
    while True:
        discord_ok = _test_discord(bot_token, guild_id, channels)
        if discord_ok:
            break
        print()
        warn("Discord connection failed. Debugging tips:")
        info("  - Verify the bot token is correct (reset it at discord.com/developers)")
        info("  - Check that the bot has been invited to the server")
        info("  - Ensure Message Content Intent is enabled under Privileged Gateway Intents")
        info(f"  - Confirm guild ID {guild_id} matches your server")
        info("  - Check that these channels exist in your server:")
        for name, chan in channels.items():
            info(f"      #{chan}  ({name})")
        print()
        if not prompt_yes_no("Retry Discord connection?", default=True):
            break

    # Authorized users
    authorized_users: list[str] = []
    if prompt_yes_no("Restrict commands to specific Discord user IDs?", default=False):
        print("  Enter user IDs one per line (empty line to finish):")
        while True:
            uid = input("    > ").strip()
            if not uid:
                break
            authorized_users.append(uid)

    return {
        "bot_token": bot_token,
        "guild_id": guild_id,
        "channels": channels,
        "authorized_users": authorized_users,
        "connected": discord_ok,
    }


def _test_discord(token: str, guild_id: str, channels: dict | None = None) -> bool:
    """Test Discord bot connectivity and verify channels exist in the guild."""
    try:
        import discord

        result: dict = {"ok": False, "missing_channels": []}

        async def _check():
            intents = discord.Intents.default()
            intents.message_content = True
            client = discord.Client(intents=intents)

            @client.event
            async def on_ready():
                guild = client.get_guild(int(guild_id))
                if guild:
                    success(f"Connected to Discord — guild: {guild.name}")
                    result["ok"] = True

                    # Verify channels exist
                    if channels:
                        guild_channel_names = {ch.name for ch in guild.text_channels}
                        for key, channel_name in channels.items():
                            if channel_name not in guild_channel_names:
                                result["missing_channels"].append(channel_name)

                        if result["missing_channels"]:
                            result["ok"] = False
                            for missing in result["missing_channels"]:
                                error(f"Channel not found: #{missing}")
                            warn("Create these channels in your Discord server or update the names")
                        else:
                            success("All configured channels found")
                else:
                    error(f"Bot connected but cannot see guild {guild_id}")
                    warn("Make sure the bot has been invited to the server")
                await client.close()

            try:
                await asyncio.wait_for(client.start(token), timeout=15)
            except asyncio.TimeoutError:
                error("Discord connection timed out")
            except discord.LoginFailure:
                error("Invalid bot token")
            except Exception as e:
                error(f"Discord error: {e}")

        asyncio.run(_check())
        return result["ok"]
    except ImportError:
        warn("discord.py not installed — skipping connectivity test")
        return False


# ── Step 3: Agent Configuration ──────────────────────────────────────────────


def step_agents(existing: dict) -> dict:
    step_header(3, "Agent Configuration")

    yaml_cfg = existing.get("_yaml", {})

    print("  Available agent types:")
    print(f"    {BOLD}[1] Claude Code{RESET}  (supported)")
    print(f"    {DIM}[2] Codex        (not yet implemented){RESET}")
    print(f"    {DIM}[3] Cursor       (not yet implemented){RESET}")
    print(f"    {DIM}[4] Aider        (not yet implemented){RESET}")
    print()

    agents: dict = {"claude": None}

    # Claude setup — check for existing key in environment or .env
    api_key = os.environ.get("ANTHROPIC_API_KEY", "") or existing.get("ANTHROPIC_API_KEY", "")
    from_env = bool(os.environ.get("ANTHROPIC_API_KEY"))
    claude_ok = False

    if api_key:
        # Test connectivity immediately if key is already available
        print()
        info("Testing Claude API connectivity with existing key...")
        claude_ok = _test_claude(api_key)

        if claude_ok:
            success("Using existing API key")
        else:
            # Retry loop for existing key failure
            while True:
                print()
                warn("Claude API connection failed. Debugging tips:")
                info("  - Verify the API key is valid at console.anthropic.com")
                info("  - Check your account has available credits")
                info("  - Ensure no network/proxy issues")
                print()
                if not prompt_yes_no("Retry Claude API connection?", default=True):
                    break
                claude_ok = _test_claude(api_key)
                if claude_ok:
                    break

            if not claude_ok:
                warn("Existing key failed — enter a new key or press Enter to continue anyway")
                new_key = prompt_secret("Anthropic API key")
                if new_key:
                    api_key = new_key
                    from_env = False
                    info("Testing new key...")
                    claude_ok = _test_claude(api_key)
    else:
        warn("ANTHROPIC_API_KEY not set in environment")
        api_key = prompt_secret("Anthropic API key")
        if not api_key:
            error("API key is required for Claude agents")
            sys.exit(1)
        from_env = False

        # Test with retry loop
        print()
        info("Testing Claude API connectivity...")
        claude_ok = _test_claude(api_key)
        while not claude_ok:
            print()
            warn("Claude API connection failed. Debugging tips:")
            info("  - Verify the API key is valid at console.anthropic.com")
            info("  - Check your account has available credits")
            info("  - Ensure no network/proxy issues")
            print()
            if not prompt_yes_no("Retry Claude API connection?", default=True):
                break
            claude_ok = _test_claude(api_key)

    default_model = yaml_cfg.get("model", "claude-sonnet-4-20250514")
    model = prompt("Model", default_model)

    agents["claude"] = {
        "api_key": api_key,
        "model": model,
        "connected": claude_ok,
        "from_env": from_env,
    }

    return agents


def _test_claude(api_key: str) -> bool:
    """Test Claude API connectivity with a minimal request."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=16,
            messages=[{"role": "user", "content": "Say ok"}],
        )
        if resp.content:
            success("Claude API connected successfully")
            return True
        error("Claude API returned empty response")
        return False
    except ImportError:
        warn("anthropic SDK not installed — skipping connectivity test")
        return False
    except Exception as e:
        error(f"Claude API error: {e}")
        return False


# ── Step 4: Scheduling & Budget ──────────────────────────────────────────────


def step_scheduling(existing: dict) -> dict:
    step_header(4, "Scheduling & Budget")

    yaml_cfg = existing.get("_yaml", {})
    existing_sched = yaml_cfg.get("scheduling", {})
    existing_pr = yaml_cfg.get("pause_retry", {})

    config: dict = {
        "global_token_budget_daily": yaml_cfg.get("global_token_budget_daily"),
        "scheduling": {
            "rolling_window_hours": int(existing_sched.get("rolling_window_hours", 24)),
            "min_task_guarantee": True,
        },
        "pause_retry": {
            "rate_limit_backoff_seconds": int(existing_pr.get("rate_limit_backoff_seconds", 60)),
            "token_exhaustion_retry_seconds": int(
                existing_pr.get("token_exhaustion_retry_seconds", 300)
            ),
        },
    }

    if prompt_yes_no("Set a daily token budget?", default=bool(config["global_token_budget_daily"])):
        default_budget = str(config["global_token_budget_daily"] or "")
        budget = prompt("Daily token budget (e.g. 1000000)", default_budget)
        if budget.isdigit():
            config["global_token_budget_daily"] = int(budget)

    if prompt_yes_no("Customize scheduling/retry defaults?", default=False):
        val = prompt(
            "Rolling window hours", str(config["scheduling"]["rolling_window_hours"])
        )
        config["scheduling"]["rolling_window_hours"] = int(val) if val.isdigit() else 24

        val = prompt(
            "Rate-limit backoff seconds",
            str(config["pause_retry"]["rate_limit_backoff_seconds"]),
        )
        config["pause_retry"]["rate_limit_backoff_seconds"] = int(val) if val.isdigit() else 60

        val = prompt(
            "Token-exhaustion retry seconds",
            str(config["pause_retry"]["token_exhaustion_retry_seconds"]),
        )
        config["pause_retry"]["token_exhaustion_retry_seconds"] = (
            int(val) if val.isdigit() else 300
        )
    else:
        info(
            f"Using defaults: {config['scheduling']['rolling_window_hours']}h window, "
            f"{config['pause_retry']['rate_limit_backoff_seconds']}s rate-limit backoff, "
            f"{config['pause_retry']['token_exhaustion_retry_seconds']}s token retry"
        )

    return config


# ── Step 5: Write Config ─────────────────────────────────────────────────────


def step_write_config(
    workspace: str,
    db_path: str,
    discord_cfg: dict,
    agents_cfg: dict,
    sched_cfg: dict,
) -> Path:
    step_header(5, "Write Configuration")

    config_dir = Path(os.path.expanduser("~/.agent-queue"))
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    env_path = config_dir / ".env"

    # Build .env
    env_lines = [
        f"DISCORD_BOT_TOKEN={discord_cfg['bot_token']}",
    ]
    claude_cfg = agents_cfg.get("claude")
    if claude_cfg and not claude_cfg["from_env"]:
        env_lines.append(f"ANTHROPIC_API_KEY={claude_cfg['api_key']}")

    env_path.write_text("\n".join(env_lines) + "\n")
    os.chmod(env_path, 0o600)
    success(f"Secrets written to {env_path} (mode 600)")

    # Build YAML
    channels = discord_cfg["channels"]
    yaml_lines = [
        f"workspace_dir: {workspace}",
        f"database_path: {db_path}",
        "",
        "discord:",
        "  bot_token: ${DISCORD_BOT_TOKEN}",
        f"  guild_id: \"{discord_cfg['guild_id']}\"",
        "  channels:",
        f"    control: {channels['control']}",
        f"    notifications: {channels['notifications']}",
        f"    agent_questions: {channels['agent_questions']}",
    ]

    if discord_cfg["authorized_users"]:
        yaml_lines.append("  authorized_users:")
        for uid in discord_cfg["authorized_users"]:
            yaml_lines.append(f'    - "{uid}"')

    yaml_lines.append("")

    if sched_cfg.get("global_token_budget_daily"):
        yaml_lines.append(f"global_token_budget_daily: {sched_cfg['global_token_budget_daily']}")
        yaml_lines.append("")

    sched = sched_cfg["scheduling"]
    yaml_lines += [
        "scheduling:",
        f"  rolling_window_hours: {sched['rolling_window_hours']}",
        f"  min_task_guarantee: true",
        "",
    ]

    pr = sched_cfg["pause_retry"]
    yaml_lines += [
        "pause_retry:",
        f"  rate_limit_backoff_seconds: {pr['rate_limit_backoff_seconds']}",
        f"  token_exhaustion_retry_seconds: {pr['token_exhaustion_retry_seconds']}",
    ]

    config_path.write_text("\n".join(yaml_lines) + "\n")
    success(f"Config written to {config_path}")

    # Offer to add .env sourcing to shell profile
    if env_lines:
        _offer_shell_env(env_path)

    return config_path


def _offer_shell_env(env_path: Path):
    """Offer to add .env sourcing to the user's shell profile."""
    source_line = f'set -a; source "{env_path}"; set +a'

    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        profile = Path.home() / ".zshrc"
    else:
        profile = Path.home() / ".bashrc"

    # Check if already present
    if profile.exists() and source_line in profile.read_text():
        info(f"Shell profile already sources {env_path}")
        return

    print()
    if prompt_yes_no(f"Add env sourcing to {profile.name}?", default=True):
        with open(profile, "a") as f:
            f.write(f"\n# agent-queue secrets\n{source_line}\n")
        success(f"Added to {profile}")
        warn(f"Run: source {profile}  (or open a new terminal)")
    else:
        info(f"You'll need to source {env_path} before running agent-queue")


# ── Step 6: Test Connectivity ─────────────────────────────────────────────────


def step_test_connectivity(discord_cfg: dict, agents_cfg: dict):
    step_header(6, "Connectivity Summary")

    if discord_cfg.get("connected"):
        success("Discord: connected")
    else:
        error("Discord: not verified")

    claude_cfg = agents_cfg.get("claude")
    if claude_cfg and claude_cfg.get("connected"):
        success("Claude API: connected")
    elif claude_cfg:
        error("Claude API: not verified")


# ── Step 7: Launch Daemon ─────────────────────────────────────────────────────


def step_launch(config_path: Path):
    step_header(7, "Launch Daemon")

    cmd = f"agent-queue {config_path}"

    if prompt_yes_no("Start the daemon now?", default=False):
        print()
        info(f"Starting: {cmd}")
        proc = subprocess.Popen(
            ["agent-queue", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        success(f"Daemon started (PID {proc.pid})")
        info(f"Stop with: kill {proc.pid}")
    else:
        print()
        info("To start later, run:")
        print(f"    {BOLD}{cmd}{RESET}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    banner()

    # Load existing config for pre-filling defaults
    existing = _load_existing_config()
    if existing.get("_yaml"):
        info("Found existing configuration — values will be pre-filled as defaults")
        print()

    workspace, db_path = step_directories(existing)
    discord_cfg = step_discord(existing)
    agents_cfg = step_agents(existing)
    sched_cfg = step_scheduling(existing)

    config_path = step_write_config(workspace, db_path, discord_cfg, agents_cfg, sched_cfg)

    step_test_connectivity(discord_cfg, agents_cfg)
    step_launch(config_path)

    print(f"\n{GREEN}{BOLD}Setup complete!{RESET}\n")


if __name__ == "__main__":
    main()
