from __future__ import annotations

import asyncio
import json
import os
import traceback

import discord
from discord.ext import commands

from src.chat_agent import ChatAgent
from src.config import AppConfig
from src.orchestrator import Orchestrator


MAX_HISTORY_MESSAGES = 50  # Max messages to fetch from Discord
COMPACT_THRESHOLD = 20     # Compact older messages beyond this count
RECENT_KEEP = 14           # Keep this many recent messages as-is after compaction


class AgentQueueBot(commands.Bot):
    def __init__(self, config: AppConfig, orchestrator: Orchestrator):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.orchestrator = orchestrator
        self.agent = ChatAgent(orchestrator, config)
        self._control_channel: discord.TextChannel | None = None
        self._notifications_channel: discord.TextChannel | None = None
        self._processed_messages: set[int] = set()
        self._channel_summaries: dict[int, tuple[int, str]] = {}  # channel_id -> (up_to_message_id, summary)
        self._channel_locks: dict[int, asyncio.Lock] = {}  # prevent concurrent LLM calls per channel
        self._restart_requested = False
        self._boot_time: float | None = None
        self._notes_threads: dict[int, str] = {}  # thread_id -> project_id
        self._notes_threads_path = os.path.join(
            os.path.dirname(config.database_path), "notes_threads.json"
        )
        self._load_notes_threads()

    def _load_notes_threads(self) -> None:
        try:
            if os.path.isfile(self._notes_threads_path):
                with open(self._notes_threads_path) as f:
                    raw = json.load(f)
                # Keys are stored as strings in JSON; convert back to int
                self._notes_threads = {int(k): v for k, v in raw.items()}
        except Exception as e:
            print(f"Warning: could not load notes threads: {e}")

    def _save_notes_threads(self) -> None:
        try:
            with open(self._notes_threads_path, "w") as f:
                json.dump(self._notes_threads, f)
        except Exception as e:
            print(f"Warning: could not save notes threads: {e}")

    def register_notes_thread(self, thread_id: int, project_id: str) -> None:
        self._notes_threads[thread_id] = project_id
        self._save_notes_threads()

    async def setup_hook(self) -> None:
        from src.discord.commands import setup_commands
        setup_commands(self)
        if self.config.discord.guild_id:
            guild = discord.Object(id=int(self.config.discord.guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        print(f"Discord bot connected as {self.user} (guild: {self.config.discord.guild_id})")
        self._boot_time = discord.utils.utcnow().timestamp()

        # Cache channels
        if self.config.discord.guild_id:
            guild = self.get_guild(int(self.config.discord.guild_id))
            if guild:
                control_name = self.config.discord.channels.get("control", "control")
                notifications_name = self.config.discord.channels.get("notifications", "notifications")
                for ch in guild.text_channels:
                    if ch.name == control_name:
                        self._control_channel = ch
                    if ch.name == notifications_name:
                        self._notifications_channel = ch

                if self._notifications_channel:
                    print(f"Notifications channel: #{self._notifications_channel.name}")
                    self.orchestrator.set_notify_callback(self._send_notification)
                else:
                    print(f"Warning: notifications channel '{notifications_name}' not found")

                if self._control_channel:
                    print(f"Control channel: #{self._control_channel.name}")
                    self.orchestrator.set_control_callback(self._send_control_message)
                else:
                    print(f"Warning: control channel '{control_name}' not found")

                # Wire up thread creation for task streaming
                if self._notifications_channel:
                    self.orchestrator.set_create_thread_callback(self._create_task_thread)

        # Initialize LLM client via ChatAgent
        try:
            if self.agent.initialize():
                print(f"Chat agent ready (model: {self.agent.model})")
            else:
                print("Warning: No LLM credentials found — set ANTHROPIC_API_KEY or run `claude login`")
        except Exception as e:
            print(f"Warning: Could not initialize LLM client: {e}")

    @staticmethod
    async def _send_long_message(
        channel: discord.abc.Messageable,
        text: str,
        *,
        reply_to: discord.Message | None = None,
        filename: str = "response.md",
    ) -> None:
        """Send a message, handling Discord's 2000-char limit.

        Short messages are sent normally. Long messages are split at line
        boundaries when possible, falling back to a file attachment for
        very long content (>6000 chars).
        """
        if len(text) <= 2000:
            if reply_to:
                await reply_to.reply(text)
            else:
                await channel.send(text)
            return

        # Very long content → attach as file with a short preview
        if len(text) > 6000:
            # Find a reasonable preview (first paragraph or first 300 chars)
            preview_end = text.find("\n\n", 0, 500)
            if preview_end == -1:
                preview_end = min(300, len(text))
            preview = text[:preview_end].rstrip()

            file = discord.File(
                fp=__import__("io").BytesIO(text.encode("utf-8")),
                filename=filename,
            )
            msg = f"{preview}\n\n*Full response attached ({len(text):,} chars)*"
            if reply_to:
                await reply_to.reply(msg, file=file)
            else:
                await channel.send(msg, file=file)
            return

        # Medium-length content → split into multiple messages at line boundaries
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            candidate = current + ("\n" if current else "") + line
            if len(candidate) > 2000:
                if current:
                    chunks.append(current)
                # If a single line exceeds 2000, hard-split it
                while len(line) > 2000:
                    chunks.append(line[:2000])
                    line = line[2000:]
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            if i == 0 and reply_to:
                await reply_to.reply(chunk)
            else:
                await channel.send(chunk)

    async def _send_notification(self, text: str) -> None:
        """Send a message to the notifications channel."""
        if self._notifications_channel:
            await self._send_long_message(self._notifications_channel, text)

    async def _send_control_message(self, text: str) -> None:
        """Send a message to the control channel."""
        if self._control_channel:
            await self._send_long_message(self._control_channel, text)

    async def _create_task_thread(self, thread_name: str, initial_message: str):
        """Create a Discord thread for streaming agent output.

        Returns a tuple of two async callbacks:
          (send_to_thread, notify_main_channel)

        send_to_thread      — streams content into the thread.
        notify_main_channel — posts a brief message to the notifications channel
                              as a reply to the thread-root message, so the
                              notification is visually linked to the thread.

        Returns None if the notifications channel is unavailable.
        """
        if not self._notifications_channel:
            print("Cannot create thread: no notifications channel")
            return None

        print(f"Creating thread: {thread_name}")
        # Create the thread-root message in the notifications channel, then open
        # a thread on it so all streaming output stays inside the thread.
        msg = await self._notifications_channel.send(
            f"**Agent working:** {thread_name}"
        )
        thread = await msg.create_thread(name=thread_name)
        await thread.send(initial_message)
        print(f"Thread created: {thread.id}")

        async def send_to_thread(text: str) -> None:
            try:
                await self._send_long_message(thread, text)
            except Exception as e:
                print(f"Thread send error: {e}")

        async def notify_main_channel(text: str) -> None:
            """Reply to the thread-root message with a brief notification."""
            try:
                await msg.reply(text)
            except Exception as e:
                print(f"Main channel notify error: {e}")
                # Fallback: plain message in the notifications channel
                try:
                    await self._notifications_channel.send(text)
                except Exception as e2:
                    print(f"Fallback notify error: {e2}")

        return send_to_thread, notify_main_channel

    async def on_message(self, message: discord.Message) -> None:
        # Ignore own messages
        if message.author == self.user:
            return

        # Dedup guard — prevent processing the same message twice
        if message.id in self._processed_messages:
            return
        self._processed_messages.add(message.id)
        # Keep the set from growing unbounded
        if len(self._processed_messages) > 200:
            self._processed_messages = set(list(self._processed_messages)[-100:])

        # Skip messages created before the bot started (prevents reprocessing after restart)
        if self._boot_time and message.created_at.timestamp() < self._boot_time:
            return

        # Only respond in the control channel, when mentioned, or in a notes thread
        is_control = (
            self._control_channel
            and message.channel.id == self._control_channel.id
        )
        is_mentioned = self.user in message.mentions
        notes_project_id = self._notes_threads.get(message.channel.id)
        is_notes_thread = notes_project_id is not None

        if not is_control and not is_mentioned and not is_notes_thread:
            return

        # Strip the bot mention from the message text
        text = message.content
        if self.user:
            text = text.replace(f"<@{self.user.id}>", "").strip()

        if not text:
            await message.reply("How can I help? Ask me about status, projects, or tasks.")
            return

        # Serialize LLM processing per channel to avoid duplicate/concurrent responses
        lock = self._channel_locks.setdefault(message.channel.id, asyncio.Lock())
        async with lock:
            async with message.channel.typing():
                try:
                    if not self.agent.is_ready:
                        await message.reply(
                            "LLM not configured — I can only respond to slash commands. "
                            "Set `ANTHROPIC_API_KEY` or run `claude login`."
                        )
                        return

                    # Prepend project context for notes threads
                    user_text = text
                    if is_notes_thread and not is_control:
                        user_text = (
                            f"[Context: this is the notes thread for project "
                            f"`{notes_project_id}`. Default to using notes tools "
                            f"(list_notes/write_note/delete_note/read_file) with "
                            f"project_id='{notes_project_id}'.]\n{text}"
                        )

                    # Build history from Discord channel
                    history = await self._build_message_history(message.channel, before=message)

                    try:
                        response = await self.agent.chat(
                            user_text, message.author.display_name, history=history
                        )
                    except Exception as e:
                        import anthropic
                        if isinstance(e, anthropic.AuthenticationError):
                            # Token may have been refreshed — reload and retry once
                            print(f"Auth error — reloading credentials: {e}")
                            if self.agent.reload_credentials():
                                response = await self.agent.chat(
                                    user_text, message.author.display_name, history=history
                                )
                            else:
                                response = (
                                    "Authentication failed. Run `claude login` "
                                    "or set `ANTHROPIC_API_KEY`."
                                )
                        else:
                            raise

                    await self._send_long_message(
                        message.channel, response, reply_to=message
                    )
                except Exception as e:
                    print(f"LLM error: {e}\n{traceback.format_exc()}")
                    await message.reply(f"**LLM error:** {e}")

    async def _build_message_history(
        self, channel: discord.TextChannel, before: discord.Message
    ) -> list[dict]:
        """Fetch recent channel messages and build LLM message history.

        When history exceeds COMPACT_THRESHOLD, older messages are summarized
        into a compact description so the LLM retains context without consuming
        excessive tokens.
        """
        raw: list[discord.Message] = []
        async for msg in channel.history(
            limit=MAX_HISTORY_MESSAGES, before=before
        ):
            raw.append(msg)
        raw.reverse()  # oldest first

        if not raw:
            return []

        # Split into older (to compact) and recent (to keep verbatim)
        if len(raw) > COMPACT_THRESHOLD:
            older = raw[:-RECENT_KEEP]
            recent = raw[-RECENT_KEEP:]
        else:
            older = []
            recent = raw

        messages: list[dict] = []

        # Compact older messages into a summary
        if older:
            summary = await self._get_or_create_summary(channel.id, older)
            if summary:
                messages.append({
                    "role": "user",
                    "content": f"[CONVERSATION SUMMARY — earlier messages]\n{summary}",
                })
                # Need an assistant ack so message list alternates properly
                messages.append({
                    "role": "assistant",
                    "content": "Understood, I have the conversation context.",
                })

        # Add recent messages verbatim
        for msg in recent:
            if msg.author == self.user:
                # Bot's own messages become assistant turns
                messages.append({"role": "assistant", "content": msg.content})
            else:
                messages.append({
                    "role": "user",
                    "content": f"[from {msg.author.display_name}]: {msg.content}",
                })

        # Merge consecutive same-role messages (Anthropic API requirement)
        merged: list[dict] = []
        for m in messages:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n" + m["content"]
            else:
                merged.append(m)

        return merged

    async def _get_or_create_summary(
        self, channel_id: int, older_messages: list[discord.Message]
    ) -> str | None:
        """Return a compact summary of older messages, caching per channel."""
        if not older_messages:
            return None
        if not self.agent.is_ready:
            return None

        last_id = older_messages[-1].id

        # Return cached summary if it covers these messages
        cached = self._channel_summaries.get(channel_id)
        if cached and cached[0] >= last_id:
            return cached[1]

        # Build a transcript to summarize
        lines = []
        for msg in older_messages:
            author = "AgentQueue" if msg.author == self.user else msg.author.display_name
            lines.append(f"{author}: {msg.content}")
        transcript = "\n".join(lines)

        summary = await self.agent.summarize(transcript)
        if summary:
            self._channel_summaries[channel_id] = (last_id, summary)
        return summary
