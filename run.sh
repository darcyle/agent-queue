#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

CONFIG_DIR="${HOME}/.agent-queue"
CONFIG_PATH="${CONFIG_DIR}/config.yaml"
LOG_PATH="${CONFIG_DIR}/daemon.log"
PID_FILE="${CONFIG_DIR}/daemon.pid"

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
        # Check if already running
        if [[ -f "$PID_FILE" ]]; then
            OLD_PID=$(cat "$PID_FILE")
            if kill -0 "$OLD_PID" 2>/dev/null; then
                echo "Daemon is already running (PID $OLD_PID)"
                echo "Use './run.sh stop' to stop it, or './run.sh restart' to restart."
                exit 1
            fi
            rm -f "$PID_FILE"
        fi

        echo "Starting agent-queue daemon..."
        # Strip Claude Code session markers so the daemon can launch its own agent sessions
        unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT 2>/dev/null
        PYTHONUNBUFFERED=1 nohup agent-queue "$CONFIG_PATH" >> "$LOG_PATH" 2>&1 &
        DAEMON_PID=$!
        echo "$DAEMON_PID" > "$PID_FILE"

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
        if [[ ! -f "$PID_FILE" ]]; then
            echo "No PID file found. Daemon may not be running."
            exit 1
        fi
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping daemon (PID $PID)..."
            kill "$PID"
            # Wait for process to exit
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
        else
            echo "Daemon is not running (stale PID file)."
        fi
        rm -f "$PID_FILE"
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
