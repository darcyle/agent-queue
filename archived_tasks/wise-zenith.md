# Add a discord command to update agent queue and restart the daemon (this assumes you are running fro

**Task ID:** `wise-zenith`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `wise-zenith/add-a-discord-command-to-update-agent-queue-and-restart-the-daemon-this-assumes-you-are-running-fro`
**Archived:** 2026-03-13 16:38:09

## Description

Add a discord command to update agent queue and restart the daemon (this assumes you are running from source)

## Result

**Summary:** Done. Here's what I implemented:

### `/update` Discord Command

**Command Handler** (`src/command_handler.py`):
- New `_cmd_update_and_restart` method that:
  1. Runs `git pull --ff-only` in the repo directory to pull latest source
  2. Runs `pip install -e .` to pick up any dependency changes
  3. Triggers the existing restart mechanism (sets `_restart_requested` flag + sends SIGTERM, which causes `os.execv` to replace the process with fresh code)
  4. Returns errors if git pull or pip install fail

**Discord Slash Command** (`src/discord/commands.py`):
- New `/update` command that:
  1. Defers the response (since pulling/installing may take a moment)
  2. Captures the before/after git commit hashes
  3. Calls the handler to pull, install, and restart
  4. Shows a rich embed with commit transition (`abc123` → `def456`), abbreviated pull output, or "Already up to date" status
  5. Reports errors via error embed if git pull or pip install fails

**Tokens Used:** 4,192
