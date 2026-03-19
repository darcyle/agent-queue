---
auto_tasks: true
---

# Chat Analyzer Agent — Implementation Plan

## Background & Design

### Problem

When users are chatting with the agent-queue bot about their projects — asking questions, discussing bugs, reviewing task output — the orchestrator's chat agent only responds when directly addressed. There's an opportunity for a background "analyzer" to watch the conversation flow and proactively contribute when it spots something useful: a solution to a problem being discussed, a task that should be created, a relevant piece of project context the user might not know about, etc.

### Design Principles

1. **Minimal overhead** — The analyzer runs on the local LLM (Ollama) to avoid burning Claude tokens. It should run periodically, not on every message.
2. **Non-intrusive** — Suggestions are presented with accept/reject UI. The user is never forced to act on them. Dismissed suggestions don't repeat.
3. **Reuse existing infrastructure** — Leverage the hook engine pattern, EventBus, ChatProvider, Discord UI Views, and the message buffer system already in the bot.
4. **Project-scoped** — Analysis happens per-project-channel, using that project's context (notes, recent tasks, memory).

### Architecture Overview

The Chat Analyzer is implemented as a new service class (`ChatAnalyzer`) that:
- Is instantiated by the orchestrator alongside the hook engine
- Subscribes to a new `chat.message` EventBus event (emitted by the bot on every user message)
- Accumulates messages per channel in a lightweight buffer
- Runs periodic analysis on a configurable interval (default: 5 minutes) per active channel
- Uses the Ollama provider (local LLM) with a specialized system prompt
- Posts suggestions to Discord as embeds with Accept/Dismiss buttons
- Tracks suggestion history to avoid duplicates

### Message Flow

```
User message in project channel
    → Bot emits "chat.message" event on EventBus
    → ChatAnalyzer._on_message() buffers the message
    → (Every N minutes) ChatAnalyzer._analyze_channel()
        → Build context: recent messages + project notes + recent tasks
        → Send to local LLM with analyzer prompt
        → LLM returns JSON: { "should_suggest": bool, "suggestion": str, "type": str, "confidence": float }
        → If should_suggest and confidence > threshold:
            → Post embed to channel with Accept/Dismiss buttons
            → On Accept: execute suggestion (create task, post answer, etc.)
            → On Dismiss: record dismissal, don't repeat similar suggestions
```

### Suggestion Types

| Type | Description | Accept Action |
|------|-------------|---------------|
| `answer` | Direct answer to a user's question | Post the answer as a bot message |
| `task` | Suggest creating a task | Create task via CommandHandler |
| `context` | Surface relevant project note/memory | Post the context as an informational embed |
| `warning` | Flag potential issue (e.g., task about to conflict) | Acknowledge and optionally create task |

### Configuration

New `chat_analyzer` section in config.yaml:

```yaml
chat_analyzer:
  enabled: false                    # Off by default
  interval_seconds: 300             # How often to analyze (5 min)
  min_messages_to_analyze: 3        # Don't analyze until N new messages
  confidence_threshold: 0.7         # Minimum confidence to suggest
  max_suggestions_per_hour: 5       # Rate limit suggestions
  provider: "ollama"                # Which chat provider to use
  model: "llama3.2"                 # Model for analysis
  cooldown_after_dismiss: 1800      # Don't suggest again for 30min after dismiss
```

### State Tracking

Stored in SQLite (new `chat_analyzer_suggestions` table):

| Column | Type | Purpose |
|--------|------|---------|
| id | INTEGER PK | Auto-increment |
| project_id | TEXT | Which project |
| channel_id | INTEGER | Discord channel |
| suggestion_type | TEXT | answer/task/context/warning |
| suggestion_text | TEXT | The suggestion content |
| suggestion_hash | TEXT | Hash for dedup |
| status | TEXT | pending/accepted/dismissed |
| created_at | REAL | Timestamp |
| resolved_at | REAL | When user responded |
| context_snapshot | TEXT | JSON of messages that triggered it |

---

## Phase 1: Core infrastructure — ChatAnalyzer service class and config

Add the foundational `ChatAnalyzer` class and configuration:

**Files to create/modify:**
- `src/chat_analyzer_service.py` (new) — The `ChatAnalyzer` class with:
  - `__init__(self, orchestrator, config)` — stores references, initializes per-channel message buffers (dict of deques), suggestion history cache, and an asyncio periodic task handle
  - `start()` / `stop()` — lifecycle methods to begin/cancel the periodic analysis loop
  - `_on_message(event_data)` — EventBus handler for `chat.message` events; appends to per-channel buffer
  - `_periodic_tick()` — runs every `interval_seconds`; iterates active channels, calls `_analyze_channel()` for any with enough new messages
  - `_analyze_channel(channel_id, project_id)` — builds context (recent messages, project info), sends to LLM, parses response
  - `_should_suggest(analysis_result, channel_id)` — checks confidence threshold, rate limits, dedup hash against recent suggestions
  - `_post_suggestion(channel_id, suggestion)` — posts Discord embed with buttons
  - `_on_accept(suggestion_id)` / `_on_dismiss(suggestion_id)` — handle button callbacks

- `src/config.py` — Add `ChatAnalyzerConfig` dataclass with the fields described in the config section above. Add `chat_analyzer` field to `AppConfig`. Add to `HOT_RELOADABLE_SECTIONS`. Add parsing in `load_config()`.

- `src/models.py` — Add `ChatAnalyzerSuggestion` dataclass (id, project_id, channel_id, suggestion_type, suggestion_text, suggestion_hash, status, created_at, resolved_at, context_snapshot).

**Key implementation details:**
- The per-channel buffer is a simple `dict[int, deque[dict]]` where each dict has `author`, `content`, `timestamp`, `is_bot`
- The periodic tick uses `asyncio.create_task` with a while-loop and `asyncio.sleep(interval)`, similar to how the orchestrator loop works
- The LLM call uses the existing `ChatProvider` interface (Ollama provider) — create a dedicated instance, not the bot's chat agent


## Phase 2: Database schema and EventBus integration

Wire the analyzer into the existing system:

**Files to modify:**
- `src/database.py` — Add `chat_analyzer_suggestions` table creation in `_ensure_tables()`. Add CRUD methods:
  - `insert_suggestion(suggestion)` → returns id
  - `get_recent_suggestions(channel_id, hours=24)` → list for dedup
  - `update_suggestion_status(suggestion_id, status, resolved_at)`
  - `get_suggestion(suggestion_id)` → single record

- `src/discord/bot.py` — Emit `chat.message` event on the EventBus in `on_message()` after buffering. Event data: `{"channel_id": int, "project_id": str|None, "author": str, "content": str, "is_bot": bool, "timestamp": float}`. Only emit for project channels (not DMs or global channel without project context).

- `src/orchestrator.py` — Instantiate `ChatAnalyzer` in `__init__` (if config enabled). Call `analyzer.start()` in the startup sequence and `analyzer.stop()` in shutdown. Pass the orchestrator reference so the analyzer can access the database, event bus, and command handler.

**Key implementation details:**
- The EventBus event is lightweight — just metadata, not the full message object
- The analyzer subscribes to `chat.message` specifically (not wildcard) to avoid processing task events
- Database methods follow the existing pattern in database.py (raw SQL with parameterized queries)


## Phase 3: LLM analysis prompt and decision logic

Implement the actual analysis intelligence:

**Files to modify:**
- `src/chat_analyzer_service.py` — Implement `_analyze_channel()` fully:
  - Build a context payload: last N messages from buffer, project summary (from DB), recent task statuses, active project notes (via command handler)
  - Construct a system prompt that instructs the LLM to analyze the conversation and return structured JSON
  - Parse the LLM response as JSON with fields: `should_suggest`, `suggestion_type`, `suggestion_text`, `confidence`, `reasoning`
  - Handle malformed responses gracefully (log and skip)

- `src/chat_analyzer_service.py` — Implement `_should_suggest()`:
  - Check `confidence >= threshold`
  - Check rate limit (count suggestions in last hour for this channel)
  - Compute suggestion hash (hash of type + first 100 chars of text) and check against recent suggestions
  - Check cooldown if last suggestion for this channel was dismissed

**System prompt design:**
```
You are a project assistant analyzer. You observe chat conversations about software projects and decide if you can help.

Given the recent conversation and project context, analyze whether you have something genuinely useful to contribute.

Rules:
- Only suggest if you're confident the suggestion adds real value
- Don't suggest things the user is already doing or has already solved
- Don't repeat information that was already shared in the conversation
- Prefer actionable suggestions (create a task, here's the fix) over vague advice
- If the conversation is casual/social or you're unsure, respond with should_suggest: false

Respond with JSON only:
{
  "should_suggest": true/false,
  "suggestion_type": "answer|task|context|warning",
  "suggestion_text": "Your suggestion here",
  "confidence": 0.0-1.0,
  "reasoning": "Why this is helpful (internal, not shown to user)"
}
```

## Phase 4: Discord UI — suggestion embeds with Accept/Dismiss buttons

Build the user-facing suggestion presentation:

**Files to create/modify:**
- `src/discord/notifications.py` — Add `ChatAnalyzerSuggestionView(discord.ui.View)`:
  - Accept button (green checkmark) — calls `ChatAnalyzer._on_accept(suggestion_id)`
  - Dismiss button (grey X) — calls `ChatAnalyzer._on_dismiss(suggestion_id)`
  - Auto-timeout after 1 hour (suggestion expires)
  - On accept: depending on type, either post the answer, create a task, or surface context
  - On dismiss: update DB status, apply cooldown
  - Add `format_suggestion_embed(suggestion)` — rich embed with suggestion type icon, text, and confidence indicator

- `src/chat_analyzer_service.py` — Implement `_post_suggestion()`:
  - Create embed via `format_suggestion_embed()`
  - Attach `ChatAnalyzerSuggestionView`
  - Send to the project channel via bot reference
  - Insert suggestion record into DB with status "pending"

- `src/chat_analyzer_service.py` — Implement `_on_accept()`:
  - For `answer` type: post the suggestion text as a regular bot message
  - For `task` type: call `CommandHandler._cmd_create_task()` with parsed task details
  - For `context` type: post an informational embed with the relevant context
  - For `warning` type: acknowledge and optionally offer to create a task
  - Update DB status to "accepted"

- `src/chat_analyzer_service.py` — Implement `_on_dismiss()`:
  - Update DB status to "dismissed"
  - Record dismissal timestamp for cooldown logic
  - Edit the original message to show "Suggestion dismissed" (grey out)

**Key implementation details:**
- Follow the exact pattern used by `PlanApprovalView` and `TaskApprovalView` for button handling
- Store `suggestion_id` in button custom_id for persistence across bot restarts
- Use `discord.Embed` with color coding: green for answer, blue for task, yellow for context, red for warning


## Phase 5: Integration testing and chat command controls

Add user-facing controls and tests:

**Files to modify:**
- `src/command_handler.py` — Add commands:
  - `_cmd_analyzer_status()` — show if analyzer is enabled, stats (suggestions made, accepted, dismissed)
  - `_cmd_analyzer_toggle()` — enable/disable the analyzer at runtime
  - `_cmd_analyzer_history()` — show recent suggestions and their statuses

- `src/chat_agent.py` — Add tool definitions for the three new commands so the LLM can invoke them via natural language

- `tests/` — Add test files:
  - `tests/test_chat_analyzer.py` — Unit tests for:
    - Message buffering and dedup
    - Confidence threshold filtering
    - Rate limiting logic
    - Suggestion hash computation
    - LLM response parsing (valid JSON, malformed, edge cases)
  - `tests/chat_eval/` — Add eval test cases for the new analyzer tools

**Key implementation details:**
- Tests should mock the ChatProvider to return canned JSON responses
- Test the full flow: message → buffer → analyze → suggest → accept/dismiss
- Verify rate limiting works correctly (max_suggestions_per_hour)
- Verify cooldown after dismiss prevents re-suggestion
