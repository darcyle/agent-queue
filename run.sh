#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_DIR="${HOME}/.agent-queue"
CONFIG_PATH="${CONFIG_DIR}/config.yaml"
LOG_PATH="${CONFIG_DIR}/daemon.log"
PID_FILE="${CONFIG_DIR}/daemon.pid"
LOCK_FILE="${CONFIG_DIR}/daemon.lock"

# --- Check setup has been run ---
if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Error: Config not found at $CONFIG_PATH"
    echo "Run ./setup.sh first."
    exit 1
fi

if [[ ! -d ".venv" ]]; then
    echo "Error: Virtual environment not found."
    echo "Run ./setup.sh first."
    exit 1
fi

# --- Activate venv ---
source .venv/bin/activate

# --- Handle commands ---
case "${1:-start}" in
    start)
        # Acquire an exclusive lock to prevent two simultaneous starts
        if ! mkdir "$LOCK_FILE" 2>/dev/null; then
            echo "Error: another './run.sh start' is already in progress."
            echo "(If not, remove the stale lock with: rm -rf $LOCK_FILE)"
            exit 1
        fi
        trap 'rm -rf "$LOCK_FILE" 2>/dev/null' EXIT

        # Check PID file first
        if [[ -f "$PID_FILE" ]]; then
            OLD_PID=$(cat "$PID_FILE")
            if kill -0 "$OLD_PID" 2>/dev/null; then
                echo "Daemon is already running (PID $OLD_PID)"
                echo "Use './run.sh stop' to stop it, or './run.sh restart' to restart."
                exit 1
            fi
            rm -f "$PID_FILE"
        fi

        # Secondary check: catch instances running without a PID file
        if pgrep -f "agent-queue.*${CONFIG_PATH}" > /dev/null 2>&1; then
            STRAY_PID=$(pgrep -f "agent-queue.*${CONFIG_PATH}" | head -1)
            echo "Daemon is already running (PID $STRAY_PID, no PID file found)."
            echo "Use './run.sh stop' to stop it, or './run.sh restart' to restart."
            exit 1
        fi

        echo "Starting agent-queue daemon..."
        # Strip Claude Code session markers so the daemon can launch its own agent sessions
        unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null
        # Prevent git/gh from ever prompting for credentials interactively.
        # Without this, expired tokens cause git to write 'Username for ...'
        # directly to /dev/tty, flooding the terminal and freezing WSL.
        export GIT_TERMINAL_PROMPT=0
        export GIT_ASKPASS=/bin/false
        export GH_PROMPT_DISABLED=1
        PYTHONUNBUFFERED=1 nohup agent-queue "$CONFIG_PATH" >> "$LOG_PATH" 2>&1 &
        DAEMON_PID=$!
        echo "$DAEMON_PID" > "$PID_FILE"

        # Release lock — PID file now guards subsequent starts
        rm -rf "$LOCK_FILE" 2>/dev/null
        trap - EXIT

        # Verify it actually started
        sleep 2
        if kill -0 "$DAEMON_PID" 2>/dev/null; then
            echo "Daemon started (PID $DAEMON_PID)"
            echo "Logs: tail -f $LOG_PATH"
        else
            echo "Error: Daemon exited immediately. Check logs:"
            tail -20 "$LOG_PATH"
            rm -f "$PID_FILE"
            exit 1
        fi
        ;;

    stop)
        # Resolve PID: prefer PID file, fall back to pgrep
        if [[ -f "$PID_FILE" ]]; then
            PID=$(cat "$PID_FILE")
            if ! kill -0 "$PID" 2>/dev/null; then
                echo "Daemon is not running (stale PID file)."
                rm -f "$PID_FILE"
                exit 0
            fi
        else
            PID=$(pgrep -f "agent-queue.*${CONFIG_PATH}" | head -1)
            if [[ -z "$PID" ]]; then
                echo "Daemon is not running."
                exit 0
            fi
            echo "Found running daemon (PID $PID, no PID file)."
        fi

        echo "Stopping daemon (PID $PID)..."
        kill "$PID"
        for i in {1..10}; do
            if ! kill -0 "$PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 "$PID" 2>/dev/null; then
            echo "Daemon didn't stop gracefully, sending SIGKILL..."
            kill -9 "$PID"
        fi
        echo "Daemon stopped."
        rm -f "$PID_FILE"
        rm -rf "$LOCK_FILE" 2>/dev/null
        ;;

    restart)
        "$0" stop 2>/dev/null || true
        "$0" start
        ;;

    status)
        if [[ -f "$PID_FILE" ]]; then
            PID=$(cat "$PID_FILE")
            if kill -0 "$PID" 2>/dev/null; then
                echo "Daemon is running (PID $PID)"
            else
                echo "Daemon is not running (stale PID file)"
                rm -f "$PID_FILE"
            fi
        else
            echo "Daemon is not running"
        fi
        ;;

    logs)
        if [[ -f "$LOG_PATH" ]]; then
            tail -f "$LOG_PATH"
        else
            echo "No log file found at $LOG_PATH"
        fi
        ;;

    *)
        echo "Usage: ./run.sh {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
