# Discord Embedded Responses: Research & Implementation Plan

> **Date:** February 24, 2026
> **Branch:** `keen-beacon/research-and-implement-embedded-responses-in-discord`
> **Objective:** Establish a consistent, visually appealing message format across all Discord bot interactions using rich embeds, interactive components, and a centralized embed factory.

---

## Executive Summary

This document presents comprehensive research on Discord embed best practices, an audit of the current `agent-queue` message formatting, and a phased implementation plan to unify the bot's visual language. The goal is to replace the current mix of plain-text notifications, inline embeds, and ad-hoc formatting with a **centralized embed factory** that ensures consistency, safety (auto-truncation), and visual appeal across all interactions.

### Current State

The project uses **discord.py >= 2.3.0** and currently has a **mixed formatting approach**:

| Area | Format | Count | Module |
|------|--------|-------|--------|
| Slash command responses | `discord.Embed` objects | ~40 instances | `commands.py` |
| Task lifecycle notifications | Plain markdown strings | 8 formatters | `notifications.py` |
| Chat agent responses | Plain text with markdown | Via `_send_long_message()` | `bot.py` |
| Error responses | Plain ephemeral text | Scattered | `commands.py` |

### Key Problems Identified

1. **Notifications use plain text** — The most visible messages (task completions, failures, PR creations) lack visual structure
2. **No centralized embed factory** — Colors, truncation, and field patterns are repeated inline across ~40 embed constructions
3. **Inconsistent error responses** — Some use embeds, some use plain text; ephemeral behavior varies
4. **No branding/timestamp consistency** — Embeds don't share a common footer or timestamp format
5. **Shared constants are scoped incorrectly** — `_STATUS_COLORS` and `_STATUS_EMOJIS` are defined inside `setup_commands()` scope, making them inaccessible to other modules

---

## Part 1: Research — Discord Embed Capabilities & Best Practices

### 1.1 Embed Anatomy

A Discord embed is a structured card-style message component rendered with a colored accent bar on the left side. The `discord.py` library provides the `discord.Embed` class for constructing them.

An embed supports the following structural elements:

| Element | Max Length | Description |
|---------|-----------|-------------|
| **Title** | 256 chars | Can be a hyperlink; supports markdown |
| **Description** | 4,096 chars | Full markdown support |
| **Fields** | Up to 25 per embed | Name/value pairs, optionally inline (up to 3 per row) |
| **Author** | 256 chars (name) | Name, icon URL, and link URL |
| **Footer** | 2,048 chars | Text and optional icon |
| **Thumbnail** | N/A | Small image, top-right corner |
| **Image** | N/A | Large image at bottom of embed |
| **Color** | N/A | Left-side accent bar (integer hex color) |
| **Timestamp** | N/A | Displayed in footer area, renders in user's local timezone |

### 1.2 Hard Limits (Discord API Enforced)

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
| **Total characters across ALL fields in one embed** | **6,000 characters** |
| Regular message content | 2,000 characters |
| Action rows per message | 5 |
| Buttons per action row | 5 |
| Select menu options | 25 |

> ⚠️ The **6,000-character total limit** is shared across ALL text properties (title + description + all field names + all field values + footer text + author name) in a single embed. This is the most critical constraint to engineer around — exceeding it causes the API call to fail silently or throw an error.

### 1.3 Markdown Support Within Embeds

**Supported in description and field values:**

| Syntax | Renders As |
|--------|-----------|
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `***bold italic***` | ***bold italic*** |
| `~~strikethrough~~` | ~~strikethrough~~ |
| `__underline__` | underline |
| `` `inline code` `` | `inline code` |
| ` ```language\ncode``` ` | Syntax-highlighted code block |
| `[text](url)` | Hyperlink |
| `> quote` | Single-line blockquote |
| `>>> quote` | Multi-line blockquote |
| `- item` | Unordered list |
| `<t:UNIX:R>` | Dynamic relative timestamp |

**NOT supported in embeds:**
- `# Headers` — only work in regular messages and forum posts, NOT inside embeds
- Tables (markdown tables do not render)
- Inline images (must use `set_image()` / `set_thumbnail()`)
- `@mentions` work but are generally discouraged in embeds for cleanliness

> **Note for this project:** The current `TaskReportView.build_content()` in `commands.py` (line 205) uses `### {emoji} {display} ({count})` headers. This works because `build_content()` returns plain message content, NOT embed content. If these task reports are moved into embeds, the header syntax will need to be replaced with **bold text** formatting.

### 1.4 Discord Dynamic Timestamps

Discord's dynamic timestamp syntax renders in each user's local timezone and auto-updates for relative styles:

| Style Code | Format | Example Output |
|------------|--------|----------------|
| `t` | Short time | `4:23 PM` |
| `T` | Long time | `4:23:00 PM` |
| `d` | Short date | `02/24/2026` |
| `D` | Long date | `February 24, 2026` |
| `f` | Short datetime | `February 24, 2026 4:23 PM` |
| `F` | Long datetime | `Monday, February 24, 2026 4:23 PM` |
| `R` | Relative | `3 hours ago` (updates automatically) |

**Recommended usage in this project:**
- Task completion/failure notifications: `<t:UNIX:R>` (relative) — "completed 3 hours ago"
- Task detail views: `<t:UNIX:f>` (short datetime) — precise start/end times
- Budget warnings: `<t:UNIX:R>` for when the budget was last checked

### 1.5 Interactive Components Reference

The project already uses `discord.ui.View`, `discord.ui.Button`, and `discord.ui.Select` in `TaskReportView`. Here is the complete component taxonomy:

#### Component Constraints

| Component | Container | Max Per Container | Notes |
|-----------|-----------|-------------------|-------|
| Buttons | Action Row | 5 | Cannot mix with select menus in same row |
| Select Menus | Action Row | 1 | Cannot mix with buttons in same row |
| Action Rows | Message | 5 | Each row is independent |
| Select Options | Select Menu | 25 | Hard limit |
| `custom_id` | Button/Select | 100 chars | Must be unique per view |

#### Button Style Reference

| Style | Name | Color | Best For |
|-------|------|-------|----------|
| `discord.ButtonStyle.primary` | Blurple | Blurple | Main action (confirm, approve) |
| `discord.ButtonStyle.secondary` | Gray | Gray | Toggle, info, secondary action |
| `discord.ButtonStyle.success` | Green | Green | Positive action (complete, accept) |
| `discord.ButtonStyle.danger` | Red | Red | Destructive action (delete, reject) |
| `discord.ButtonStyle.link` | Link | Gray w/icon | External URL (no callback) |

### 1.6 Libraries & Dependencies Assessment

#### No Additional Dependencies Required

The project already has everything needed:

- **`discord.py >= 2.3.0`** — Full embed support via `discord.Embed`, interactive components via `discord.ui`
- **Python `datetime`** — For timestamps
- **Python `enum`** — For embed style types

#### Optional Enhancements (Future Consideration)

| Library | Purpose | Recommendation |
|---------|---------|----------------|
| `discord-ext-pages` | Paginated embeds with built-in navigation | Useful if task lists grow beyond 6,000-char embed limit |
| Custom `EmbedPaginator` | Multi-page embed navigation | Can be built with `discord.ui.View` + Previous/Next buttons (more control) |

The built-in `discord.ui.View` (already used for `TaskReportView`) is sufficient for interactive components. No new dependencies are recommended.

---

## Part 2: Research — Industry Patterns & Anti-Patterns

### 2.1 Industry Patterns from Popular Bots

#### Task/Project Management Bots (Linear, Jira, GitHub integrations)
- **Inline field triplets** — Three inline fields per row for compact metadata (ID, Status, Assignee)
- **Hyperlinked titles** — Embed title links to the web resource (PR URL, task URL)
- **Code blocks for technical content** — Diffs, error messages, log excerpts in triple backticks
- **Status change visualization** — `Old Status → New Status` with emoji on both sides
- **Thread-based streaming** — One thread per long-running operation with streamed updates (already implemented)
- **Actionable footers** — "Run `/command` to..." suggestions at the bottom

#### General Purpose Bots (MEE6, Carl-bot, Dyno)
- **Author line for bot identity** — `embed.set_author(name="BotName", icon_url=avatar_url)`
- **Footer for metadata** — Timestamp + bot version or bot name
- **Paginated embeds for long lists** — Previous/Next button navigation
- **Collapsible sections via button toggles** — Already implemented in `TaskReportView`
- **Ephemeral error responses** — Never clutter the channel with error messages

### 2.2 Color Best Practices

- Use a **consistent semantic color palette** — green for success, red for errors, amber for warnings, blue for informational
- Map task/status-specific colors consistently
- Never use random or aesthetic-only colors — they should always convey meaning
- **Consider colorblind accessibility:** pair colors with emojis/icons so meaning is never color-dependent alone

### 2.3 Anti-Patterns to Avoid

Based on analysis of popular Discord bots and UX research:

1. **Walls of text in embeds** — If content exceeds ~1,000 chars in a single field, use a file attachment with a preview embed instead
2. **Too many fields** — More than 8-10 fields per embed becomes overwhelming; group related data or use description text
3. **Inconsistent colors** — Using random colors or different shades of green for different success types
4. **Missing ephemeral on errors** — Visible errors clutter channels and embarrass users who made typos
5. **No fallback for oversized content** — Always have a truncation + "full details via `/command`" strategy
6. **Mixing embeds and plain text for the same message category** — e.g., some task completions as embeds, others as plain text
7. **Ignoring mobile rendering** — Inline fields stack vertically on mobile; keep inline field values short (under ~20 chars)
8. **Empty embed fields** — Discord rejects truly empty field values; use zero-width space `\u200b` as a placeholder

### 2.4 Information Hierarchy in Embeds

For scanability in busy Discord channels:

```
Color bar   → Immediate visual status (green = good, red = bad)
Title       → What happened (with emoji prefix)
Inline fields → Key metadata at a glance (ID, project, agent)
Full-width fields → Details (summary, error, files changed)
Footer      → Branding + timestamp
```

### 2.5 Ephemeral vs. Public Response Strategy

| Message Type | Visibility | Rationale |
|-------------|------------|-----------|
| Error responses | Ephemeral | Only the triggering user sees them; avoids clutter |
| Success/creation responses | Public | Team visibility for operations that affect shared state |
| Inspection/detail responses | Ephemeral | Avoids flooding the channel with read-only data |
| Notifications | Public | They are broadcast alerts for the whole team |
| Chat agent responses | Public | Conversational, team-visible |

---

## Part 3: Proposed Architecture — Centralized Embed Factory

### 3.1 New Module: `src/discord/embeds.py`

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


def check_embed_size(embed: discord.Embed) -> bool:
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

### 3.2 Design Principles

1. **Single source of truth** — All colors, emojis, and truncation logic in one module
2. **Automatic safety** — Every text property is auto-truncated to Discord limits
3. **Consistent branding** — Footer and timestamp on every embed by default
4. **Status-aware** — `status_embed()` automatically maps task status to color + emoji
5. **Testable** — Pure functions, no Discord connection required to construct embeds

---

## Part 4: Converting Notifications to Embeds

### 4.1 Strategy: Hybrid Approach

Keep the existing string formatters in `notifications.py` for logging and testing, and add parallel `*_embed()` functions that return `discord.Embed` objects. The bot layer (`bot.py`) selects the embed version when sending to Discord.

### 4.2 Updated Notification Callback

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

### 4.3 Embed Versions of Each Notification

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

#### `format_stuck_defined_task_embed()`

```python
def format_stuck_defined_task_embed(
    task: Task,
    blocking_deps: list[tuple[str, str, str]],
    stuck_hours: float,
) -> discord.Embed:
    fields = [
        ("Task ID", f"`{task.id}`", True),
        ("Project", f"`{task.project_id}`", True),
        ("Stuck Duration", f"**{stuck_hours:.1f} hours**", True),
    ]
    if blocking_deps:
        dep_list = "\n".join(
            f"• `{dep_id}` — {dep_title} ({dep_status})"
            for dep_id, dep_title, dep_status in blocking_deps[:5]
        )
        if len(blocking_deps) > 5:
            dep_list += f"\n… and {len(blocking_deps) - 5} more"
        fields.append(("Blocked By", truncate(dep_list, 1024), False))
    else:
        fields.append(("Note", "_No unmet dependencies found — may be a promotion logic bug._", False))
    fields.append((
        "Actions",
        "• `/skip-task <blocker-id>` to skip a blocker\n"
        "• `/restart-task <blocker-id>` to retry it",
        False,
    ))
    return warning_embed(title=f"Stuck Task: {task.title}", fields=fields)
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

## Part 5: Standardizing Slash Command Responses

### 5.1 Current Inconsistencies

1. **Successful operations** — Mix of embeds and plain text confirmations
2. **Error responses** — Almost all are plain `f"Error: {result['error']}"` strings
3. **Ephemeral handling** — Inconsistent; some errors are ephemeral, some aren't

### 5.2 Proposed Standard Patterns

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

## Part 6: Additional Interactive Component Patterns

### 6.1 Confirmation Views for Destructive Operations

```python
class ConfirmView(discord.ui.View):
    def __init__(self, timeout=60):
        super().__init__(timeout=timeout)
        self.confirmed = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.confirmed = True
        self.stop()
        await interaction.response.edit_message(content="Confirmed.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.confirmed = False
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)
```

### 6.2 Actionable Error Embeds with Retry Button

```python
class RetryView(discord.ui.View):
    def __init__(self, task_id: str, timeout=300):
        super().__init__(timeout=timeout)
        self.task_id = task_id

    @discord.ui.button(label="Retry Task", style=discord.ButtonStyle.primary, emoji="🔄")
    async def retry(self, interaction, button):
        result = await handler.execute("restart_task", {"task_id": self.task_id})
        if "error" in result:
            await interaction.response.send_message(
                embed=error_embed("Retry Failed", description=result["error"]),
                ephemeral=True,
            )
        else:
            await interaction.response.edit_message(
                embed=success_embed("Task Restarted"),
                view=None,
            )
```

### 6.3 Paginated Embed Views for Long Lists

```python
class PaginatedView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], timeout=300):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.current_page = 0

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction, button):
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(embed=self.pages[self.current_page])

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        await interaction.response.edit_message(embed=self.pages[self.current_page])
```

---

## Part 7: Embed Size Safety Engineering

The 6,000-character total limit is the most critical constraint. Beyond the `check_embed_size()` function, here's a robust fallback strategy:

```python
import io

def safe_send_embed(
    embed: discord.Embed,
    fallback_text: str,
) -> tuple[discord.Embed | None, str | None, discord.File | None]:
    """Ensure embed fits within Discord limits, falling back gracefully.

    Returns (embed, text, file) — at most one of embed/text will be non-None.
    """
    if check_embed_size(embed):
        return embed, None, None

    # Try trimming description first
    if embed.description and len(embed.description) > 500:
        embed.description = truncate(embed.description, 500)
        if check_embed_size(embed):
            return embed, None, None

    # If still too large, fall back to a mini embed + file attachment
    file = discord.File(
        fp=io.BytesIO(fallback_text.encode("utf-8")),
        filename="details.md",
    )
    mini_embed = discord.Embed(
        title=embed.title,
        description="Full details attached as file.",
        color=embed.color,
    )
    return mini_embed, None, file
```

---

## Part 8: Visual Design Specification

### 8.1 Color Palette

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

### 8.2 Embed Structure Template

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

### 8.3 Inline Field Layout Rules

- **3-column rows:** Use for short metadata (ID, project, agent, status)
- **2-column rows:** Use for paired data (old → new status, used/limit)
- **Full-width:** Use for descriptions, error messages, file lists, code blocks
- **Spacer field:** `("\u200b", "\u200b", True)` to force a new row when needed

---

## Part 9: Message Flow After Implementation

```
Orchestrator
├── format_task_completed_embed() ──┐
├── format_task_failed_embed() ─────┤
├── format_task_blocked_embed() ────┤ discord.Embed objects
├── format_pr_created_embed() ──────┤
├── format_chain_stuck_embed() ─────┤
├── format_stuck_defined_task_embed()┤
└── format_budget_warning_embed() ──┘
         │
         ▼
    _notify_channel(text, project_id, embed=embed)
         │
         ▼
    AgentQueueBot._send_message(text, project_id, embed=embed)
         │
         ├── embed? → channel.send(embed=embed)
         └── text?  → _send_long_message(channel, text)

Slash Commands
    └── success_embed() / error_embed() / status_embed()
         │
         ▼
    interaction.response.send_message(embed=..., ephemeral=...)
```

---

## Part 10: Phased Implementation Plan

### Phase 1: Foundation (Low Risk)

**Create `src/discord/embeds.py`**
- Move `_STATUS_COLORS` and `_STATUS_EMOJIS` from `commands.py` to this module
- Implement `make_embed()`, convenience builders, `truncate()`, `unix_timestamp()`, `check_embed_size()`
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

### Phase 5: Interactive Components (Medium Risk)

**Add actionable embeds**
- `RetryView` on failed task notifications
- `ConfirmView` for destructive slash commands
- `PaginatedView` for long task lists exceeding embed limits

**Estimated effort:** ~3-4 hours

### Phase 6: Polish & Testing

- Add unit tests for `embeds.py` (pure functions, easy to test)
- Visual QA in a test Discord server
- Verify all embeds stay under 6,000-char total limit
- Test mobile rendering (inline fields may stack vertically)

**Estimated effort:** ~2-3 hours

---

## Part 11: Risk Assessment & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 6,000-char total limit exceeded | Embed fails to send | `check_embed_size()` guard + `safe_send_embed()` fallback |
| Mobile rendering differences | Inline fields may stack | Test on mobile; keep inline field values short |
| Breaking existing tests | Test failures | Keep string formatters alongside embed formatters |
| Notification callback signature change | Runtime errors | Use `**kwargs` for backward compat; phase the migration |
| Embed rate limits | Messages throttled | Discord rate limits are per-channel, not embed-specific; no additional risk |
| Over-embedding | Visual clutter | Keep chat agent responses as plain text; only structured data uses embeds |

---

## Summary of Recommendations

1. **Create `src/discord/embeds.py`** — Centralized embed factory with consistent styling, auto-truncation, and branding
2. **Add embed notification formatters** — Parallel `*_embed()` functions in `notifications.py` alongside existing string formatters
3. **Update `bot.py` callback** — Support `embed` kwarg in `_send_message()` for rich notifications
4. **Standardize slash command responses** — Replace inline `discord.Embed()` calls with factory functions; use `error_embed()` for all errors
5. **Use Discord timestamps** — `<t:UNIX:R>` for all time-related fields
6. **Add embed size guard** — Prevent exceeding the 6,000-char total limit with `safe_send_embed()` fallback
7. **Keep chat agent responses as plain text** — Conversational responses don't benefit from embeds
8. **Add interactive components** — Retry buttons on failures, confirm dialogs on destructive actions, pagination for long lists
9. **No new dependencies** — Everything can be done with `discord.py >= 2.3.0`'s built-in `discord.Embed` and `discord.ui`

### Expected Outcome

- **Consistent visual language** across all bot interactions
- **Easier scanning** in busy channels (colored bars, icons, structured fields)
- **Better error visibility** (red embeds stand out vs. plain text)
- **Actionable notifications** (retry buttons, PR links, command hints)
- **Maintainable code** (single source of truth for colors, styles, limits)
- **Backward compatible** (string formatters preserved for logging/testing)
- **Safe by default** (auto-truncation prevents silent Discord API failures)
