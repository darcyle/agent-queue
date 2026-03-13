# If something restarts agent queue, make the command to restart agent queue require a reason, and log

**Task ID:** `solid-current`
**Project:** `agent-queue`
**Status:** COMPLETED
**Priority:** 100
**Branch:** `solid-current/if-something-restarts-agent-queue-make-the-command-to-restart-agent-queue-require-a-reason-and-log`
**Archived:** 2026-03-13 16:38:10

## Description

If something restarts agent queue, make the command to restart agent queue require a reason, and log the reason to the main command channel before restarting. If the reason is a discord command, it should automatically specify user {user_name} requested a restart...

## Result

**Summary:** Done. Here's a summary of the changes:

### Changes Made

**1. `/restart` Discord command** (`src/discord/commands.py`)
- Now requires a `reason` parameter (string, mandatory)
- Automatically prefixes the reason with `"User {display_name} requested a restart: {reason}"`
- Includes the reason in the warning embed shown to the user

**2. `/update` Discord command** (`src/discord/commands.py`)
- Added optional `reason` parameter (auto-filled if omitted)
- Automatically prefixes with `"User {display_name} requested an update"` (+ user's reason if provided)

**3. Command handler** (`src/command_handler.py`)
- `_cmd_restart_daemon`: Extracts `reason` from args and logs `"🔄 **Daemon restart initiated** — {reason}"` to the main notification channel via `orchestrator._notify_channel` before triggering SIGTERM
- `_cmd_update_and_restart`: Same pattern — logs `"🔄 **Daemon update & restart initiated** — {reason}"` before restarting

**4. Chat agent tool** (`src/chat_agent.py`)
- `restart_daemon` tool schema now has a required `reason` property, so the LLM must always provide a reason when requesting a restart

**Tokens Used:** 9,826
