# Observation A: No `agent_questions` Per-Project Equivalent

## Status: Documented — Low Priority / Future Consideration

## Current State (Verified)

The `agent_questions` channel is configured **globally only** in `DiscordConfig.channels` (`src/config.py:17`):

```python
channels: dict[str, str] = field(default_factory=lambda: {
    "control": "control",
    "notifications": "notifications",
    "agent_questions": "agent-questions",  # Global only
})
```

The `Project` model (`src/models.py:85-95`) has per-project fields for notifications and control channels, but **no** `discord_agent_questions_channel_id`:

```python
discord_channel_id: str | None = None          # Per-project notifications
discord_control_channel_id: str | None = None   # Per-project control channel
# No agent_questions equivalent
```

## Additional Findings

1. **The `agent_questions` channel is defined but never actually used in source code.** Agent questions (`TaskEvent.AGENT_QUESTION`) are routed through the standard notifications channel via `format_agent_question()` in `src/discord/notifications.py`.

2. **The setup wizard configures the channel name** (`setup_wizard.py:268, 289-290`) but the bot never looks up or sends messages to an `agent_questions`-specific channel.

3. **Per-project control channels already exist** and provide a natural place for project-scoped agent conversations, making a separate per-project agent questions channel less necessary.

## Impact

When agents from different projects ask questions, they all arrive in the same notifications channel. For systems with many active projects this could be confusing, but in practice:
- Agent questions are relatively rare
- Per-project control channels already scope conversations by project
- The global notifications channel includes project context in its message formatting

## Recommendation

**Low priority.** No code changes needed now. If this becomes a pain point in the future, the fix would be:

1. Add `discord_agent_questions_channel_id: str | None = None` to the `Project` model
2. Add a migration: `ALTER TABLE projects ADD COLUMN discord_agent_questions_channel_id TEXT`
3. Create a `_get_agent_questions_channel(project_id)` method in `bot.py` (following the existing `_get_notification_channel` pattern)
4. Route `AGENT_QUESTION` events through this new method instead of the general notifications path
5. Actually use the `agent_questions` channel config (which is currently defined but unused)
