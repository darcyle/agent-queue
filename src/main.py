from __future__ import annotations

import asyncio
import os
import signal
import sys

from src.config import load_config
from src.orchestrator import Orchestrator


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.agent-queue/config.yaml")


async def run(config_path: str) -> None:
    config = load_config(config_path)

    # Ensure database directory exists
    os.makedirs(os.path.dirname(config.database_path), exist_ok=True)

    orch = Orchestrator(config)
    await orch.initialize()

    shutdown_event = asyncio.Event()

    def handle_signal():
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    try:
        # Main loop: run scheduling cycles
        while not shutdown_event.is_set():
            await orch.run_one_cycle()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
    finally:
        await orch.shutdown()


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    asyncio.run(run(config_path))


if __name__ == "__main__":
    main()
