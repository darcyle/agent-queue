from __future__ import annotations

import discord
from discord.ext import commands

from src.config import AppConfig
from src.orchestrator import Orchestrator


class AgentQueueBot(commands.Bot):
    def __init__(self, config: AppConfig, orchestrator: Orchestrator):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.orchestrator = orchestrator

    async def setup_hook(self) -> None:
        from src.discord.commands import setup_commands
        setup_commands(self)
        if self.config.discord.guild_id:
            guild = discord.Object(id=int(self.config.discord.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        pass
