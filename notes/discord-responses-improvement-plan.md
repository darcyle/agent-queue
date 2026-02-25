# Discord Embedded Responses Improvement Plan

## Executive Summary

This document proposes a comprehensive redesign of Discord message formatting across the `agent-queue` project. The goal is to replace inconsistent plain-text notifications with rich, consistently styled embeds, creating a visually appealing and scannable UI across all bot interactions.

### Current State

The project uses **discord.py >= 2.3.0** and currently has a **mixed formatting approach**:

| Area | Format | Count |
|------|--------|-------|
| Slash command responses | `discord.Embed` objects | ~40 instances across `commands.py` |
| Task lifecycle notifications | Plain markdown strings | 8 formatters in `notifications.py` |
| Chat agent responses | Plain text with markdown | Via `_send_long_message()` in `bot.py` |
| Error responses | Plain ephemeral text | Scattered across `commands.py` |

**Key problems:**
- Notifications (the most visible messages) use plain text, not embeds
- No centralized embed factory — colors, truncation, and field patterns are repeated inline
- Error responses are inconsistent (some use embeds, some use plain text)
- No branding or timestamp consistency across embeds
- The `_STATUS_COLORS` and `_STATUS_EMOJIS` dicts are defined inside `setup_commands()` scope, making them inaccessible to other modules

---

## 1. Discord Embed Capabilities & Constraints

### What Embeds Support

Discord embeds are structured message components that render as a colored card with:

- **Title** (max 256 chars) — can be a hyperlink
- **Description** (max 4,096 chars) — full markdown support
- **Fields** (up to 25) — name/value pairs, optionally inline (up to 3 per row)
- **Author** — name, icon, URL
- **Footer** — text and icon
- **Thumbnail** — small image (top-right corner)
- **Image** — large image (bottom of embed)
- **Color** — left-side accent bar (hex color)
- **Timestamp** — displayed in footer area, renders in user's local timezone

### Hard Limits (Discord API enforced)

| Property | Limit |
|----------|-------|
| Title | 256 characters |
| Description | 4,096 characters |
| Field name | 256 characters |
| Field value | 1,024 characters |
| Footer text | 2,048 characters |
| Author name | 256 characters |
| Fields per embed | 25 |
| Embeds per message | 10 |
| **Total characters across ALL fields in one message** | **6,000 characters** |

> ⚠️ The 6,000-character total limit is shared across ALL text properties of ALL embeds in a single message. This is the most important constraint to engineer around.

### Markdown Support Within Embeds

**Supported in description and field values:**
- `**bold**`, `*italic*`, `***bold italic***`, `~~strikethrough~~`, `__underline__`
- `` `inline code` `` and ` ```code blocks``` ` (with language hints for syntax highlighting)
- `[text](url)` hyperlinks
- `> blockquote` and `>>> multi-line blockquote`
- `- item` unordered lists
- `<t:UNIX_TIMESTAMP:R>` dynamic timestamps (renders relative, e.g., "3 hours ago")

**NOT supported in embeds:**
- `# Headers` (only work in regular messages/forum posts)
- Tables
- Inline images (must use `set_image()` / `set_thumbnail()`)

### Discord Timestamps (Highly Recommended)

Discord's dynamic timestamp syntax renders in each user's local timezone:

```
<t:1234567890:R>   → "3 hours ago" (relative)
<t:1234567890:f>   → "February 24, 2026 4:23 PM" (short datetime)
<t:1234567890:t>   → "4:23 PM" (short time)
```

These are ideal for task start times, completion times, and durations in notifications.

---

## 2. Proposed Architecture: Centralized Embed Factory

### New Module: `src/discord/embeds.py`

Create a centralized embed factory that enforces consistent styling. All embed creation should flow through this module.

```python
"""Centralized embed factory for consistent Discord message styling.

All embeds in the bot should be created through this module to ensure
consistent colors, truncation, branding, and structure.
"""
from __future__ import annotations

import discord
from datetime import datetime, timezone
from enum import Enum

from src.models import TaskStatus


# ---------------------------------------------------------------------------
# Semantic embed types (for non-status-specific messages)
# ---------------------------------------------------------------------------

class EmbedStyle(Enum):
    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    CRITICAL = "critical"


_STYLE_COLORS: dict[EmbedStyle, int] = {
    EmbedStyle.SUCCESS:  0x2ecc71,  # Green
    EmbedStyle.ERROR:    0xe74c3c,  # Red
    EmbedStyle.WARNING:  0xf39c12,  # Orange/Amber
    EmbedStyle.INFO:     0x3498db,  # Blue
    EmbedStyle.CRITICAL: 0x992d22,  # Dark Red
}

_STYLE_ICONS: dict[EmbedStyle, str] = {
    EmbedStyle.SUCCESS:  "✅",
    EmbedStyle.ERROR:    "❌",
    EmbedStyle.WARNING:  "⚠️",
    EmbedStyle.INFO:     "ℹ️",
    EmbedStyle.CRITICAL: "🚨",
}


# ---------------------------------------------------------------------------
# Status colors and emojis (moved from commands.py for shared access)
# ---------------------------------------------------------------------------

STATUS_COLORS: dict[str, int] = {
    TaskStatus.DEFINED.value:            0x95a5a6,  # Gray
    TaskStatus.READY.value:              0x3498db,  # Blue
    TaskStatus.ASSIGNED.value:           0x9b59b6,  # Purple
    TaskStatus.IN_PROGRESS.value:        0xf39c12,  # Amber
    TaskStatus.WAITING_INPUT.value:      0x1abc9c,  # Teal
    TaskStatus.PAUSED.value:             0x7f8c8d,  # Dark Gray
    TaskStatus.VERIFYING.value:          0x2980b9,  # Dark Blue
    TaskStatus.AWAITING_APPROVAL.value:  0xe67e22,  # Orange
    TaskStatus.COMPLETED.value:          0x2ecc71,  # Green
    TaskStatus.FAILED.value:             0xe74c3c,  # Red
    TaskStatus.BLOCKED.value:            0x992d22,  # Dark Red
}

STATUS_EMOJIS: dict[str, str] = {
    TaskStatus.DEFINED.value:            "⚪",
    TaskStatus.READY.value:              "🔵",
    TaskStatus.ASSIGNED.value:           "📋",
    TaskStatus.IN_PROGRESS.value:        "🟡",
    TaskStatus.WAITING_INPUT.value:      "💬",
    TaskStatus.PAUSED.value:             "⏸️",
    TaskStatus.VERIFYING.value:          "🔍",
    TaskStatus.AWAITING_APPROVAL.value:  "⏳",
    TaskStatus.COMPLETED.value:          "🟢",
    TaskStatus.FAILED.value:             "🔴",
    TaskStatus.BLOCKED.value:            "⛔",
}

# Bot branding
_FOOTER_TEXT = "AgentQueue"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def truncate(text: str, max_len: int, suffix: str = "…") -> str:
    """Safely truncate text to fit within Discord character limits."""
    if len(text) <= max_len:
        return text
    return text[: max_len - len(suffix)] + suffix


def unix_timestamp(dt: datetime, style: str = "R") -> str:
    """Format a datetime as a Discord dynamic timestamp.

    Styles: t (short time), T (long time), d (short date), D (long date),
            f (short datetime), F (long datetime), R (relative).
    """
    return f"<t:{int(dt.timestamp())}:{style}>"


# ---------------------------------------------------------------------------
# Core embed builder
# ---------------------------------------------------------------------------

def make_embed(
    style: EmbedStyle,
    title: str,
    description: str | None = None,
    fields: list[tuple[str, str, bool]] | None = None,
    footer: str | None = None,
    timestamp: bool = True,
    color_override: int | None = None,
) -> discord.Embed:
    """Create a consistently-styled embed.

    Parameters
    ----------
    style : EmbedStyle
        Semantic type (determines color and title icon).
    title : str
        Embed title (will be truncated to 256 chars).
    description : str | None
        Optional body text (truncated to 4096 chars).
    fields : list of (name, value, inline) tuples
        Optional embed fields (max 25, each truncated to limits).
    footer : str | None
        Optional footer text override. Defaults to bot branding.
    timestamp : bool
        Whether to include a UTC timestamp (default True).
    color_override : int | None
        Override the style-based color (e.g., for status-specific colors).
    """
    icon = _STYLE_ICONS[style]
    embed = discord.Embed(
        title=truncate(f"{icon} {title}", 256),
        description=truncate(description, 4096) if description else None,
        color=color_override if color_override is not None else _STYLE_COLORS[style],
        timestamp=datetime.now(tz=timezone.utc) if timestamp else None,
    )

    if fields:
        for name, value, inline in fields[:25]:
            embed.add_field(
                name=truncate(name, 256),
                value=truncate(value, 1024) if value else "\u200b",
                inline=inline,
            )

    embed.set_footer(text=footer or _FOOTER_TEXT)
    return embed


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------

def success_embed(title: str, **kwargs) -> discord.Embed:
    return make_embed(EmbedStyle.SUCCESS, title, **kwargs)

def error_embed(title: str, **kwargs) -> discord.Embed:
    return make_embed(EmbedStyle.ERROR, title, **kwargs)

def warning_embed(title: str, **kwargs) -> discord.Embed:
    return make_embed(EmbedStyle.WARNING, title, **kwargs)

def info_embed(title: str, **kwargs) -> discord.Embed:
    return make_embed(EmbedStyle.INFO, title, **kwargs)

def critical_embed(title: str, **kwargs) -> discord.Embed:
    return make_embed(EmbedStyle.CRITICAL, title, **kwargs)


def status_embed(
    title: str,
    status: str,
    **kwargs,
) -> discord.Embed:
    """Create an embed colored by task status."""
    color = STATUS_COLORS.get(status, 0x95a5a6)
    emoji = STATUS_EMOJIS.get(status, "⚪")
    return make_embed(
        EmbedStyle.INFO,
        f"{emoji} {title}",
        color_override=color,
        **kwargs,
    )
```

### Design Principles

1. **Single source of truth** — All colors, emojis, and truncation logic in one module
2. **Automatic safety** — Every text property is auto-truncated to Discord limits
3. **Consistent branding** — Footer and timestamp on every embed by default
4. **Status-aware** — `status_embed()` automatically maps task status to color + emoji
5. **Testable** — Pure functions, no Discord connection required to construct embeds

---

## 3. Converting Notifications to Embeds

### Strategy: Hybrid Approach (Recommended)

Keep the existing string formatters in `notifications.py` for logging and testing, and add parallel `*_embed()` functions that return `discord.Embed` objects. The bot layer (`bot.py`) selects the embed version when sending to Discord.

### Updated Notification Signature

Update the notify callback in `bot.py` to accept either strings or embeds:

```python
# In bot.py — updated _send_message signature
async def _send_message(
    self,
    text: str,
    project_id: str | None = None,
    *,
    embed: discord.Embed | None = None,
) -> None:
    """Send a message to the appropriate channel.

    If embed is provided, sends the embed. Falls back to text.
    """
    channel = self._resolve_channel(project_id)
    if embed:
        await channel.send(embed=embed)
    else:
        await self._send_long_message(channel, text)
```

### Embed Versions of Each Notification

#### `format_task_completed_embed()`

```python
def format_task_completed_embed(
    task: Task, agent: Agent, output: AgentOutput
) -> discord.Embed:
    embed = success_embed(
        title=f"Task Completed: {task.title}",
        fields=[
            ("Task ID", f"`{task.id}`", True),
            ("Project", f"`{task.project_id}`", True),
            ("Agent", agent.name, True),
            ("Tokens Used", f"{output.tokens_used:,}", True),
        ],
    )
    if output.summary:
        embed.add_field(
            name="Summary",
            value=truncate(output.summary, 1024),
            inline=False,
        )
    if output.files_changed:
        files = ", ".join(f"`{f}`" for f in output.files_changed[:15])
        embed.add_field(
            name="Files Changed",
            value=truncate(files, 1024),
            inline=False,
        )
    return embed
```

#### `format_task_failed_embed()`

```python
def format_task_failed_embed(
    task: Task, agent: Agent, output: AgentOutput
) -> discord.Embed:
    error_type, suggestion = classify_error(output.error_message)
    embed = error_embed(
        title=f"Task Failed: {task.title}",
        fields=[
            ("Task ID", f"`{task.id}`", True),
            ("Project", f"`{task.project_id}`", True),
            ("Agent", agent.name, True),
            ("Retries", f"{task.retry_count}/{task.max_retries}", True),
            ("Error Type", f"**{error_type}**", False),
        ],
    )
    if output.error_message:
        snippet = truncate(output.error_message, 900)
        embed.add_field(
            name="Error Detail",
            value=f"```\n{snippet}\n```",
            inline=False,
        )
    embed.add_field(
        name="💡 Suggestion",
        value=suggestion,
        inline=False,
    )
    embed.add_field(
        name="Next Step",
        value=f"Use `/agent-error {task.id}` for the full error log.",
        inline=False,
    )
    return embed
```

#### `format_task_blocked_embed()`

```python
def format_task_blocked_embed(
    task: Task, last_error: str | None = None
) -> discord.Embed:
    fields = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Status", f"Max retries ({task.max_retries}) exhausted", False),
    ]
    if last_error:
        error_type, suggestion = classify_error(last_error)
        fields.append(("Last Error Type", f"**{error_type}**", False))
        fields.append(("💡 Suggestion", suggestion, False))
    fields.append((
        "Action Required",
        f"Use `/agent-error {task.id}` to inspect the last error.",
        False,
    ))
    return critical_embed(title=f"Task Blocked: {task.title}", fields=fields)
```

#### `format_pr_created_embed()`

```python
def format_pr_created_embed(task: Task, pr_url: str) -> discord.Embed:
    return info_embed(
        title=f"PR Created: {task.title}",
        fields=[
            ("Task ID", f"`{task.id}`", True),
            ("Project", f"`{task.project_id}`", True),
            ("Status", "⏳ AWAITING_APPROVAL", True),
            ("Pull Request", f"[Review and Merge]({pr_url})", False),
        ],
    )
```

#### `format_agent_question_embed()`

```python
def format_agent_question_embed(
    task: Task, agent: Agent, question: str
) -> discord.Embed:
    return warning_embed(
        title=f"Agent Needs Input: {task.title}",
        fields=[
            ("Task ID", f"`{task.id}`", True),
            ("Project", f"`{task.project_id}`", True),
            ("Agent", agent.name, True),
            ("Question", f"> {truncate(question, 1000)}", False),
        ],
    )
```

#### `format_chain_stuck_embed()`

```python
def format_chain_stuck_embed(
    blocked_task: Task, stuck_tasks: list[Task]
) -> discord.Embed:
    stuck_list = "\n".join(
        f"• `{t.id}` — {t.title} ({t.status.value})"
        for t in stuck_tasks[:10]
    )
    if len(stuck_tasks) > 10:
        stuck_list += f"\n… and {len(stuck_tasks) - 10} more"
    return critical_embed(
        title=f"Dependency Chain Stuck",
        description=f"`{blocked_task.id}` — {blocked_task.title} is **BLOCKED**",
        fields=[
            ("Project", f"`{blocked_task.project_id}`", True),
            ("Affected Tasks", str(len(stuck_tasks)), True),
            ("Downstream Tasks", truncate(stuck_list, 1024), False),
            (
                "Actions",
                f"• `/skip-task {blocked_task.id}` to skip and unblock\n"
                f"• `/restart-task {blocked_task.id}` to retry",
                False,
            ),
        ],
    )
```

#### `format_budget_warning_embed()`

```python
def format_budget_warning_embed(
    project_name: str, usage: int, limit: int
) -> discord.Embed:
    pct = (usage / limit * 100) if limit > 0 else 0
    # Color shifts from amber to red as budget approaches limit
    color = 0xf39c12 if pct < 90 else 0xe74c3c
    return warning_embed(
        title=f"Budget Warning: {project_name}",
        description=f"Token usage at **{pct:.0f}%**",
        fields=[
            ("Used", f"{usage:,} tokens", True),
            ("Limit", f"{limit:,} tokens", True),
            ("Remaining", f"{max(0, limit - usage):,} tokens", True),
        ],
        color_override=color,
    )
```

---

## 4. Standardizing Slash Command Responses

### Current Inconsistencies

1. **Successful operations** — Mix of embeds and plain text confirmations
2. **Error responses** — Almost all are plain `f"Error: {result['error']}"` strings
3. **Ephemeral handling** — Inconsistent; some errors are ephemeral, some aren't

### Proposed Standard Patterns

#### Success Responses → Always use `success_embed()`

```python
# Before (inconsistent)
await interaction.response.send_message(f"✅ Project updated: weight={weight}")

# After (consistent)
await interaction.response.send_message(embed=success_embed(
    title="Project Updated",
    fields=[("Weight", str(weight), True)],
))
```

#### Error Responses → Always use `error_embed()`, always ephemeral

```python
# Before (plain text)
await interaction.response.send_message(
    f"Error: {result['error']}", ephemeral=True
)

# After (styled embed)
await interaction.response.send_message(
    embed=error_embed(title="Command Failed", description=result['error']),
    ephemeral=True,
)
```

#### Status-Specific Responses → Use `status_embed()`

```python
# For task status changes, use status-colored embeds
embed = status_embed(
    title="Task Status Updated",
    status=new_status,
    fields=[
        ("Task", f"`{task_id}` — {title}", False),
        ("Change", f"{old_emoji} **{old}** → {new_emoji} **{new}**", False),
    ],
)
```

---

## 5. Implementation Plan

### Phase 1: Foundation (Low Risk)

**Create `src/discord/embeds.py`**
- Move `_STATUS_COLORS` and `_STATUS_EMOJIS` from `commands.py` to this module
- Implement `make_embed()`, convenience builders, `truncate()`, `unix_timestamp()`
- Update `commands.py` to import from `embeds.py` instead of defining locally

**Estimated effort:** ~2 hours

### Phase 2: Notification Embeds (Medium Risk)

**Add embed formatters to `notifications.py`**
- Add `*_embed()` variants for all 8 notification types
- Import embed utilities from `embeds.py`
- Keep existing string formatters for backward compatibility

**Update `bot.py` notify callback**
- Modify `_send_message()` to accept optional `embed` kwarg
- Update orchestrator callback wiring to pass embeds alongside text

**Estimated effort:** ~3-4 hours

### Phase 3: Slash Command Consistency (Medium Risk)

**Standardize all ~50 slash commands**
- Replace inline `discord.Embed()` calls with factory functions
- Convert plain-text error responses to `error_embed()` calls
- Ensure all errors are ephemeral
- Group by command file section for systematic conversion

**Estimated effort:** ~4-5 hours

### Phase 4: Chat Agent Responses (Low Risk)

**Enhance chat agent formatting**
- Chat agent responses should remain plain text (they're conversational)
- However, tool execution results embedded in chat could use embeds
- This is optional and should be evaluated after Phases 1-3

**Estimated effort:** ~1-2 hours (if pursued)

### Phase 5: Polish & Testing

- Add unit tests for `embeds.py` (pure functions, easy to test)
- Visual QA in a test Discord server
- Verify all embeds stay under 6,000-char total limit
- Test mobile rendering (inline fields may stack vertically)

**Estimated effort:** ~2-3 hours

---

## 6. Visual Design Specification

### Color Palette

| Context | Color | Hex | Usage |
|---------|-------|-----|-------|
| Success | Green | `#2ecc71` | Task completed, resource created, operation succeeded |
| Error | Red | `#e74c3c` | Task failed, command error, API failure |
| Warning | Amber | `#f39c12` | Budget warning, approaching limits, agent question |
| Info | Blue | `#3498db` | Status display, list results, neutral information |
| Critical | Dark Red | `#992d22` | Task blocked, chain stuck, requires intervention |
| In Progress | Amber | `#f39c12` | Agent currently working |
| Assigned | Purple | `#9b59b6` | Task assigned to agent |
| Waiting | Teal | `#1abc9c` | Waiting for human input |
| Paused | Dark Gray | `#7f8c8d` | Manually paused |

### Embed Structure Template

```
┌──────────────────────────────────────────┐
│ 🟢 [Color bar]                           │
│                                          │
│ ✅ Task Completed: Fix JWT bug           │  ← Title (icon + text)
│                                          │
│ Task ID     Project      Agent           │  ← Inline fields (row 1)
│ `task-89`   `my-app`     claude-1        │
│                                          │
│ Tokens Used                              │  ← Inline field (row 2)
│ 18,420                                   │
│                                          │
│ Summary                                  │  ← Full-width field
│ Updated auth.py to refresh JWT tokens... │
│                                          │
│ Files Changed                            │  ← Full-width field
│ `auth.py`, `tests/test_auth.py`          │
│                                          │
│ AgentQueue • Today at 4:23 PM            │  ← Footer + timestamp
└──────────────────────────────────────────┘
```

### Inline Field Layout Rules

- **3-column rows:** Use for short metadata (ID, project, agent, status)
- **2-column rows:** Use for paired data (old → new status, used/limit)
- **Full-width:** Use for descriptions, error messages, file lists, code blocks
- **Spacer field:** `("\u200b", "\u200b", True)` to force a new row when needed

---

## 7. Libraries and Dependencies

### No Additional Dependencies Required

The project already has everything needed:

- **`discord.py >= 2.3.0`** — Full embed support via `discord.Embed`
- **Python `datetime`** — For timestamps
- **Python `enum`** — For embed style types

### Optional Enhancements (Future)

| Library | Purpose | Notes |
|---------|---------|-------|
| `discord-ext-pages` | Paginated embeds | For long task lists that exceed embed limits |
| Custom `EmbedPaginator` | Multi-page embed navigation | Could be built with `discord.ui.View` + buttons |

The built-in `discord.ui.View` (already used in `commands.py` for `TaskReportView`) is sufficient for interactive components. No additional libraries are recommended at this time.

---

## 8. Risk Assessment & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 6,000-char total limit exceeded | Embed fails to send | Add `_check_embed_size()` guard in factory; fall back to text |
| Mobile rendering differences | Inline fields may stack | Test on mobile; keep inline fields short |
| Breaking existing tests | Test failures | Keep string formatters alongside embed formatters |
| Notification callback signature change | Runtime errors | Use `**kwargs` for backward compat; phase the migration |
| Embed rate limits | Messages throttled | Discord rate limits are per-channel, not embed-specific; no additional risk |

### Total Character Guard

```python
def _check_embed_size(embed: discord.Embed) -> bool:
    """Check if an embed is within the 6,000-character total limit."""
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name) + len(field.value)
    return total <= 6000
```

---

## 9. Summary of Recommendations

1. **Create `src/discord/embeds.py`** — Centralized embed factory with consistent styling, auto-truncation, and branding
2. **Add embed notification formatters** — Parallel `*_embed()` functions in `notifications.py` alongside existing string formatters
3. **Update `bot.py` callback** — Support `embed` kwarg in `_send_message()` for rich notifications
4. **Standardize slash command responses** — Replace inline `discord.Embed()` calls with factory functions; use `error_embed()` for all errors
5. **Use Discord timestamps** — `<t:UNIX:R>` for all time-related fields
6. **Add embed size guard** — Prevent exceeding the 6,000-char total limit
7. **Keep chat agent responses as plain text** — Conversational responses don't benefit from embeds
8. **No new dependencies** — Everything can be done with discord.py's built-in `discord.Embed`

### Expected Outcome

- **Consistent visual language** across all bot interactions
- **Easier scanning** in busy channels (colored bars, icons, structured fields)
- **Better error visibility** (red embeds stand out vs. plain text)
- **Maintainable code** (single source of truth for colors, styles, limits)
- **Backward compatible** (string formatters preserved for logging/testing)
