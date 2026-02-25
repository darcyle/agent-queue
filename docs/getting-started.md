# Getting Started

## Prerequisites

- Python 3.12 or later
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- Claude Code CLI or API access

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/ElectricJack/agent-queue.git
cd agent-queue
```

### 2. Install Dependencies

```bash
pip install -e ".[dev]"
```

### 3. Run the Setup Wizard

The interactive setup wizard will walk you through configuration:

```bash
python setup_wizard.py
```

This creates a configuration file at `~/.agent-queue/config.yaml` with your Discord bot token, guild ID, and project settings.

### 4. Start the Daemon

```bash
agent-queue
```

Or with a custom config path:

```bash
agent-queue /path/to/config.yaml
```

## Configuration

Agent Queue uses a YAML configuration file. The setup wizard creates this for you, but you can also edit it manually. See the [Configuration Spec](specs/config.md) for full details.

Key configuration sections:

- **discord** — Bot token, guild ID, channel names
- **agents_default** — Heartbeat intervals, timeouts
- **scheduling** — Rolling window, token budgets
- **projects** — Your project definitions with workspace paths and repo settings

## First Steps

Once the daemon is running and connected to Discord:

1. **Add a project** — Tell the bot about your project in the Discord channel
2. **Create a task** — Describe what you want done in natural language
3. **Watch it work** — The bot creates a thread and streams agent progress
4. **Review the PR** — When the task completes, review the generated pull request
