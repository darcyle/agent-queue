"""Agent Queue daemon entry point — process lifecycle and signal handling.

Startup sequence:
  1. Load config from YAML (default: ~/.agent-queue/config.yaml)
  2. Initialize the Orchestrator (database, event bus, scheduler)
  3. Start the Discord bot (connects to gateway, registers commands)
  4. Run the scheduler loop (~5s cycle: promote tasks, assign agents, execute)
  5. Wait for SIGTERM/SIGINT to trigger graceful shutdown

On shutdown, the bot and orchestrator are closed cleanly. If a restart was
requested (via the /restart Discord command), the process replaces itself
using os.execv() — this ensures a fresh Python interpreter with updated code
while the systemd/supervisor unit sees a continuous process.

See specs/main.md for the full specification.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys

from src.adapters import AdapterFactory
from src.config import load_config
from src.discord.bot import AgentQueueBot
from src.orchestrator import Orchestrator


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")


async def run(config_path: str) -> bool:
    """Run the daemon. Returns True if a restart was requested."""
    config = load_config(config_path)

    # Ensure database directory exists
    os.makedirs(os.path.dirname(config.database_path), exist_ok=True)

    adapter_factory = AdapterFactory()
    orch = Orchestrator(config, adapter_factory=adapter_factory)
    await orch.initialize()

    # Start Discord bot
    bot = AgentQueueBot(config, orch)

    shutdown_event = asyncio.Event()

    def handle_signal():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    async def run_bot():
        try:
            await bot.start(config.discord.bot_token)
        except asyncio.CancelledError:
            pass

    async def run_scheduler():
        # Wait for the bot to be ready before scheduling
        await bot.wait_until_ready()
        while not shutdown_event.is_set():
            await orch.run_one_cycle()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    bot_task = asyncio.create_task(run_bot())
    scheduler_task = asyncio.create_task(run_scheduler())

    try:
        # Wait until shutdown is signaled
        await shutdown_event.wait()
    finally:
        # Shut down bot and orchestrator
        restart = bot._restart_requested
        await bot.close()
        bot_task.cancel()
        scheduler_task.cancel()
        await orch.shutdown()

    return restart


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    restart = asyncio.run(run(config_path))
    if restart:
        print("Restart requested — exec'ing new process...")
        # Resolve the entry point to an absolute path for execv
        exe = shutil.which(sys.argv[0]) or os.path.abspath(sys.argv[0])
        os.execv(exe, sys.argv)


if __name__ == "__main__":
    main()
