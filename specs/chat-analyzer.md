# Chat Analyzer Specification

**Source files:** `src/chat_analyzer.py`
**Related config:** `src/config.py` (`ChatAnalyzerConfig`)
**Related models:** `src/discord/notifications.py` (`AnalyzerSuggestionView`, `format_analyzer_suggestion_embed`)
**Database table:** `chat_analyzer_suggestions`

---

## 1. Overview

The `ChatAnalyzer` is a background service that monitors conversation flow in project Discord channels and proactively suggests actions when it identifies something useful. It subscribes to `chat.message` events on the EventBus, buffers messages per channel, and periodically runs analysis using a local LLM (Ollama by default).

When the analyzer spots an opportunity — a question it can answer, a task that should be created, relevant project context, or a potential issue — it posts a suggestion as a Discord embed with Accept/Dismiss buttons.

### Design Principles

- **Minimal overhead**: Runs on a local LLM to avoid burning Claude tokens on the main API.
- **Non-intrusive**: Suggestions have accept/reject UI; dismissed ones trigger a cooldown and duplicates are never repeated.
- **Project-scoped**: Analysis happens per-project-channel with project context (recent tasks).
- **Rate-limited**: Multiple layers prevent suggestion spam (per-hour cap, cooldown after dismiss, confidence threshold, deduplication).

### Data Flow

```
Discord message (user types in project channel)
    │
    ▼
bot.py on_message() ──emit──▶ EventBus "chat.message"
    │
    ▼
ChatAnalyzer._on_message() ──buffer──▶ per-channel deque (max 50)
    │
    ▼  (every N seconds)
ChatAnalyzer._analysis_loop()
    ├── Check min message threshold
    ├── Check rate limit (max suggestions/hour)
    ├── Check dismiss cooldown
    ├── Gather project context (recent tasks)
    ├── Call local LLM with conversation + context
    ├── Parse JSON response → AnalyzerSuggestion
    ├── Check confidence threshold
    ├── Dedup via SHA-256 hash
    ├── Store in chat_analyzer_suggestions table
    └── Post Discord embed with Accept/Dismiss buttons
```

## Source Files

- `src/chat_analyzer.py` — Core service class, message buffering, analysis loop, LLM interaction
- `src/config.py` — `ChatAnalyzerConfig` dataclass
- `src/database.py` — `chat_analyzer_suggestions` table and CRUD methods
- `src/discord/bot.py` — `chat.message` event emission, `_post_analyzer_suggestion()` callback
- `src/discord/notifications.py` — `format_analyzer_suggestion_embed()`, `AnalyzerSuggestionView`
- `src/orchestrator.py` — Instantiation and lifecycle management

---

## 2. ChatAnalyzer Class

### Constructor

```python
ChatAnalyzer(db: Database, bus: EventBus, config: ChatAnalyzerConfig)
```

**Internal state:**

| Attribute | Type | Description |
|---|---|---|
| `_buffers` | `dict[int, deque[BufferedMessage]]` | Per-channel message buffer (max 50 messages each) |
| `_channel_projects` | `dict[int, str]` | Maps channel IDs to project IDs |
| `_new_message_counts` | `dict[int, int]` | Count of unanalyzed messages per channel |
| `_last_analysis` | `dict[int, float]` | Timestamp of last analysis per channel |
| `_provider` | `ChatProvider \| None` | Lazily-created LLM provider instance |
| `_analysis_task` | `asyncio.Task \| None` | Background analysis loop task |
| `_notify` | `Callable \| None` | Callback for posting plain messages to Discord |
| `_post_suggestion` | `Callable \| None` | Callback for posting suggestion embeds with buttons |

### Lifecycle

#### `initialize()`

If `config.enabled` is `True`:
1. Subscribes to `"chat.message"` events on the EventBus.
2. Creates an `asyncio.Task` running `_analysis_loop()`.
3. Logs the startup with interval and model info.

If `config.enabled` is `False`, returns immediately without subscribing or starting the loop.

#### `shutdown()`

Cancels the background analysis task if it is running. Awaits the task's cancellation to ensure clean shutdown. Sets `_analysis_task` to `None`.

### Callbacks

Two callbacks are set by the Discord bot after initialization:

- `set_notify_callback(callback)` — For posting plain messages to channels.
- `set_post_suggestion_callback(callback)` — For posting rich suggestion embeds. Connected to `bot._post_analyzer_suggestion()`.

---

## 3. Message Buffering

### Event: `chat.message`

Emitted by `bot.py` for every user message in a project channel (not bot messages). The event payload:

| Field | Type | Description |
|---|---|---|
| `channel_id` | `int` | Discord channel ID |
| `project_id` | `str` | Project that owns the channel |
| `author` | `str` | Display name of the message author |
| `content` | `str` | Message text |
| `timestamp` | `float` | Unix timestamp from `message.created_at` |
| `is_bot` | `bool` | Always `False` (bot messages are filtered before emission) |

### BufferedMessage

```python
@dataclass(slots=True)
class BufferedMessage:
    author: str
    content: str
    timestamp: float
    is_bot: bool
```

### Buffer Behavior

- Each channel gets a `deque(maxlen=50)` — older messages are automatically evicted when the buffer is full.
- `_new_message_counts[channel_id]` is incremented on each message to track how many messages arrived since the last analysis.
- `_channel_projects[channel_id]` is updated on each message to maintain the channel-to-project mapping.

---

## 4. Analysis Loop

`_analysis_loop()` runs as an infinite `asyncio` task, sleeping for `config.interval_seconds` between passes.

### Analysis Pass (`_run_analysis_pass`)

For each channel with buffered messages:

1. **Minimum message check**: Skip if `new_message_count < config.min_messages_to_analyze`.
2. **Rate limit check**: Query `db.count_recent_suggestions(project_id, now - 3600)`. Skip if count `>= config.max_suggestions_per_hour`.
3. **Dismiss cooldown check**: Query `db.get_last_dismiss_time(project_id, channel_id)`. Skip if the most recent dismissal was less than `config.cooldown_after_dismiss` seconds ago.
4. If all checks pass, the channel is added to the analysis queue.

### Channel Analysis (`_analyze_channel`)

1. Lazily creates the LLM provider via `_get_provider()`.
2. Resets `_new_message_counts[channel_id]` to 0 and records `_last_analysis` time.
3. Formats buffered messages into a conversation transcript.
4. Gathers project context (recent tasks from the database).
5. Builds the analysis prompt and calls the LLM with `max_tokens=512`.
6. Parses the JSON response into an `AnalyzerSuggestion`.
7. Applies confidence threshold: skip if `confidence < config.confidence_threshold`.
8. Deduplication: computes `SHA-256(project_id:type:suggestion_text)[:16]` and checks `db.get_suggestion_hash_exists()`.
9. Stores the suggestion in `chat_analyzer_suggestions` with a context snapshot (last N messages, truncated to 200 chars each).
10. Calls `_post_suggestion` callback to post the Discord embed.

### LLM Provider

Created lazily on first use. Constructs a `ChatProviderConfig` from `ChatAnalyzerConfig` fields (`provider`, `model`, `base_url`) and creates a provider via `create_chat_provider()`. The `keep_alive` parameter is set to `"5m"` for Ollama connection reuse.

---

## 5. LLM Prompt and Response

### System Prompt

The analyzer LLM is instructed to watch for four types of suggestions and respond with JSON only:

```json
{
  "should_suggest": true,
  "suggestion": "Concise, actionable suggestion text",
  "type": "answer|task|context|warning",
  "confidence": 0.0-1.0,
  "task_title": "optional - only for type=task"
}
```

### Suggestion Types

| Type | Emoji | Color | Purpose |
|---|---|---|---|
| `answer` | 💡 | Blue (`#3498DB`) | Directly answer a question the user asked |
| `task` | 📋 | Green (`#2ECC71`) | Suggest creating a task from the discussion |
| `context` | 📎 | Purple (`#9B59B6`) | Surface relevant project context the user may not know |
| `warning` | ⚠️ | Orange (`#E67E22`) | Flag a potential issue (conflicts, known bugs, etc.) |

### Response Parsing

`_parse_response()` handles:
- Extracting text from the provider response's `text_parts`.
- Stripping markdown code block wrappers (````json ... ````).
- Parsing JSON; returns `None` on parse failure.
- Falling back to `"context"` type if the returned type is not in the valid set.

---

## 6. Database Schema

### Table: `chat_analyzer_suggestions`

```sql
CREATE TABLE IF NOT EXISTS chat_analyzer_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    suggestion_type TEXT NOT NULL,
    suggestion_text TEXT NOT NULL,
    suggestion_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    resolved_at REAL,
    context_snapshot TEXT
);
```

**Indexes:**
- `idx_chat_analyzer_project` on `(project_id, status)` — for rate limit queries.
- `idx_chat_analyzer_hash` on `(suggestion_hash)` — for deduplication lookups.

### Status Values

| Status | Description |
|---|---|
| `pending` | Suggestion posted, awaiting user action |
| `accepted` | User clicked Accept |
| `dismissed` | User clicked Dismiss |

### Database Methods

| Method | Signature | Description |
|---|---|---|
| `create_chat_analyzer_suggestion` | `(project_id, channel_id, suggestion_type, suggestion_text, suggestion_hash, context_snapshot?) -> int` | Insert a new suggestion, returns row ID |
| `resolve_chat_analyzer_suggestion` | `(suggestion_id, status) -> None` | Update status to `"accepted"` or `"dismissed"`, set `resolved_at` |
| `get_suggestion_hash_exists` | `(project_id, suggestion_hash) -> bool` | Check for duplicate suggestion |
| `count_recent_suggestions` | `(project_id, since) -> int` | Count suggestions since timestamp (rate limiting) |
| `get_last_dismiss_time` | `(project_id, channel_id) -> float \| None` | Most recent dismissal timestamp (cooldown) |

---

## 7. Discord Integration

### Event Emission (`bot.py`)

In `on_message()`, before any other message processing, the bot emits `chat.message` for every non-bot message in a project channel:

```python
await self.orchestrator.bus.emit("chat.message", {
    "channel_id": channel_id,
    "project_id": project_id_for_event,
    "author": message.author.display_name,
    "content": message.content,
    "timestamp": message.created_at.timestamp(),
    "is_bot": False,
})
```

### Suggestion Embeds (`notifications.py`)

`format_analyzer_suggestion_embed()` creates a `discord.Embed` with:
- Title: `"{emoji} Suggestion: {Type}"` (e.g., "💡 Suggestion: Answer")
- Description: the suggestion text (truncated to Discord's description limit)
- Color: type-specific (see Suggestion Types table above)
- Footer: `"Chat Analyzer • {project_id} • Confidence: {confidence%}"`

### Suggestion Buttons (`AnalyzerSuggestionView`)

A `discord.ui.View` with a 1-hour timeout containing two buttons:

#### Accept Button (✅ green)

Records acceptance in the database, then executes the suggestion based on type:

| Type | Action |
|---|---|
| `answer` | Posts the suggestion text as a bot message in the channel |
| `task` | Creates a task via `CommandHandler.execute("create_task", ...)` using `task_title` (or first 80 chars of suggestion) as the title |
| `context` | Posts the context as a bot message in the channel |
| `warning` | Posts the warning as a bot message in the channel |

After execution, all buttons are disabled and the embed is updated.

#### Dismiss Button (❌ grey)

Records dismissal in the database (triggers cooldown for future suggestions in that channel). Disables all buttons and updates the embed.

---

## 8. Orchestrator Integration

### Initialization (`orchestrator.py`)

During `initialize()`, after hook engine setup:

```python
if self.config.chat_analyzer.enabled:
    from src.chat_analyzer import ChatAnalyzer
    self.chat_analyzer = ChatAnalyzer(
        self.db, self.bus, self.config.chat_analyzer
    )
    await self.chat_analyzer.initialize()
```

The `chat_analyzer` attribute is set to `None` by default and only populated when the config has `enabled: true`.

### Bot Callback Registration

In `bot.py`'s `on_ready()`, if `orchestrator.chat_analyzer` exists, the bot registers `_post_analyzer_suggestion` as the suggestion callback so the analyzer can post embeds to Discord.

---

## 9. Rate Limiting and Spam Prevention

The analyzer implements five layers of protection against suggestion spam:

| Layer | Mechanism | Default |
|---|---|---|
| **Minimum messages** | Won't analyze until N new messages arrive | 3 messages |
| **Analysis interval** | Time between analysis passes | 300 seconds (5 min) |
| **Rate limit** | Max suggestions per project per hour | 5 per hour |
| **Dismiss cooldown** | Pause suggestions in a channel after dismissal | 1800 seconds (30 min) |
| **Confidence threshold** | Minimum LLM confidence score to post | 0.70 |
| **Deduplication** | SHA-256 hash prevents identical suggestions from being posted twice | — |

---

## 10. Configuration Reference

`ChatAnalyzerConfig` (from `src/config.py`):

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Master switch — analyzer does nothing when disabled |
| `interval_seconds` | `int` | `300` | Seconds between analysis passes |
| `min_messages_to_analyze` | `int` | `3` | Minimum new messages before analyzing a channel |
| `confidence_threshold` | `float` | `0.7` | Minimum confidence score to post a suggestion |
| `max_suggestions_per_hour` | `int` | `5` | Per-project rate limit on suggestions |
| `provider` | `str` | `"ollama"` | Chat provider for analysis LLM |
| `model` | `str` | `"llama3.2"` | Model name for the analysis provider |
| `base_url` | `str` | `"http://localhost:11434/v1"` | LLM provider endpoint |
| `cooldown_after_dismiss` | `int` | `1800` | Seconds to pause suggestions in a channel after a dismissal |

YAML config path: `chat_analyzer` top-level key.

```yaml
chat_analyzer:
  enabled: true
  interval_seconds: 300
  min_messages_to_analyze: 3
  confidence_threshold: 0.7
  max_suggestions_per_hour: 5
  provider: ollama
  model: llama3.2
  base_url: http://localhost:11434/v1
  cooldown_after_dismiss: 1800
```

Validation rules (from `ChatAnalyzerConfig.validate()`):
- `interval_seconds` must be `>= 30`
- `confidence_threshold` must be in `[0.0, 1.0]`
- `max_suggestions_per_hour` must be `>= 1`
