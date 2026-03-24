---
auto_tasks: true
---

# Consolidate Discord Task Notifications Into Single Post With Thread

## Background

### Current Behavior (3 separate messages per task)

Looking at the screenshot, a single task (`smart-torrent`) produces three separate messages in the channel:

1. **Task Added embed** (green, "Task Added") — posted by the `/add-task` slash command response (`src/discord/commands.py:2878-2887`). Shows ID, Project, Status (READY), and Description.
2. **Task Started embed** (amber, "Task Started") — posted by `_execute_task()` in `src/orchestrator.py:3205` via `_notify_channel()`. Shows Task ID, Project, Agent, Status (IN_PROGRESS), Workspace, Branch, plus a Stop button.
3. **"Agent working" message + thread** — posted by `_create_task_thread()` in `src/discord/bot.py:841-844`. A plain text message ("**Agent working:** task-id | title") with a thread created underneath it.

When the task completes, the Task Started message is **deleted** (`orchestrator.py:3835-3841`) and a Task Completed embed is posted as a reply to the thread-root message (via `_notify_brief`/`thread_main_notify`).

This means **three messages are posted to the channel for every task lifecycle**, cluttering the channel significantly when multiple tasks are in flight.

### Desired Behavior (1 message, edited through lifecycle)

1. **Task Added** — Post a single "Task Added" embed in the main channel. Create a thread below it (thread stays empty initially — no "Agent working" preamble message inside the thread).
2. **Task Started** — **Edit** the same message to show the Task Started embed (with Stop button). No new message.
3. **Task Completed/Failed/Blocked** — **Edit** the same message to show the final status embed. No new message.

This means **one Discord message per task lifecycle**, keeping the channel clean.

### Key Constraints

- The `/add-task` slash command uses `interaction.response.send_message()` which returns an `InteractionMessage`. We can retrieve it via `await interaction.original_response()` for later editing.
- Tasks created via chat (Supervisor `create_task` tool) don't currently post a "Task Added" notification to the channel — the LLM response serves as confirmation. We need to handle these too.
- The orchestrator is decoupled from Discord via callbacks (`_notify`/`_create_thread`). We need a new callback mechanism to **edit** an existing message by task_id.
- Thread creation currently posts an "Agent working" message and creates the thread on it. Instead, we'll create the thread directly on the Task Added embed message.
- The `_task_started_messages` dict in orchestrator.py currently stores messages for later deletion. This will be replaced by a lifecycle message tracking system.

### Key Files

| File | Role in Change |
|------|---------------|
| `src/discord/notifications.py` | Add `format_task_added_embed()`, modify views |
| `src/discord/commands.py` | Refactor `/add-task` to post lifecycle message |
| `src/discord/bot.py` | Add lifecycle message tracking, refactor `_create_task_thread()` |
| `src/orchestrator.py` | Use edit-in-place for started/completed/failed instead of post+delete |

---

## Phase 1: Add `format_task_added_embed()` and lifecycle message tracking infrastructure

**Goal:** Create the Task Added embed formatter and add infrastructure to track task_id to Discord message for later editing.

### Changes:

**`src/discord/notifications.py`:**
- Add `format_task_added_embed(task_id: str, project_id: str, title: str, description: str | None = None) -> discord.Embed` function. Uses `status_embed()` with `TaskStatus.READY.value` to get the blue color. Fields: Task ID, Project, Status (READY), and optionally Description (truncated).

**`src/discord/bot.py`:**
- Add `_task_lifecycle_messages: dict[str, discord.Message] = {}` to `__init__` — maps task_id to the single channel message that tracks its lifecycle.
- Add method `async def store_task_lifecycle_message(self, task_id: str, message: discord.Message) -> None` — stores the message in the dict.
- Add method `async def edit_task_lifecycle_message(self, task_id: str, *, embed: discord.Embed, view: discord.ui.View | None = None) -> bool` — looks up the stored message and calls `await message.edit(embed=embed, view=view)`. Returns True on success, False on failure (message deleted, permissions, etc.). On failure, removes the entry from the dict.
- Add method `def get_task_lifecycle_message(self, task_id: str) -> discord.Message | None` — returns the stored message for thread creation purposes.

**`src/orchestrator.py`:**
- Add new callback types:
  - `StoreLifecycleCallback = Callable[[str, Any], Awaitable[None]]` (task_id, message)
  - `EditLifecycleCallback = Callable[[str, Any, Any | None], Awaitable[bool]]` (task_id, embed, view)
  - `GetLifecycleCallback = Callable[[str], Any | None]` (task_id to message)
- Add `set_lifecycle_callbacks(store, edit, get)` method that stores all three.
- Wire these in `bot.py`'s `on_ready()` alongside the existing callbacks:
  ```python
  self.orchestrator.set_lifecycle_callbacks(
      store=self.store_task_lifecycle_message,
      edit=self.edit_task_lifecycle_message,
      get=self.get_task_lifecycle_message,
  )
  ```

---

## Phase 2: Refactor `/add-task` to post the lifecycle message with thread

**Goal:** The `/add-task` command posts the Task Added embed as the lifecycle message and creates a thread on it.

### Changes:

**`src/discord/commands.py`** — In `add_task_command()`:
- Use the interaction response as the lifecycle message. After `await interaction.response.send_message(embed=embed)`, call `msg = await interaction.original_response()` to get the Message object.
- Call `bot.store_task_lifecycle_message(task_id, msg)` to register it.
- Create a thread on the lifecycle message: `thread = await msg.create_thread(name=f"{task_id} | {title}"[:100])`. Store the thread mapping in `bot._task_threads[thread.id] = task_id` and `bot._task_thread_objects[task_id] = thread`. The thread stays **empty** — no initial message posted inside it.

This approach reuses the interaction response as the lifecycle message, avoiding a second message in the channel.

---

## Phase 3: Refactor `_create_task_thread()` and `_execute_task()` for edit-in-place

**Goal:** When a task starts, edit the lifecycle message to Task Started instead of posting a new message. Thread creation reuses the lifecycle message instead of posting "Agent working".

### Changes:

**`src/discord/bot.py`** — In `_create_task_thread()`:
- At the start, check if a lifecycle message already exists for this `task_id` via `self.get_task_lifecycle_message(task_id)`.
- **If it exists:** Use it as the thread root. Check if a thread already exists (from Phase 2 — `_task_thread_objects.get(task_id)`). If a thread already exists, reuse it. If not, create one on the lifecycle message. Skip posting the "Agent working" message. Skip posting `initial_message` into the thread. Return the `(send_to_thread, notify_main_channel)` callbacks as before, where `notify_main_channel` edits the lifecycle message embed rather than replying.
- **If it doesn't exist:** Fall back to current behavior (post "Agent working" message, create thread on it), but also store this message as the lifecycle message via `store_task_lifecycle_message()`.

**`src/orchestrator.py`** — In `_execute_task()` (around line 3199-3213):
- Before posting a new Task Started notification via `_notify_channel()`, try to **edit** the lifecycle message: `success = await self._edit_lifecycle(task.id, embed=format_task_started_embed(...), view=TaskStartedView(...))`.
- If editing succeeds, skip the `_notify_channel()` call. The started embed is now shown on the existing message.
- If editing fails (no lifecycle message — task created via chat, auto-generated subtask, etc.), fall back to current behavior: post a new Task Started message via `_notify_channel()`.
- Either way, store a reference in `self._task_started_messages[task.id]` for backward compatibility with the stop button logic.

---

## Phase 4: Edit lifecycle message on completion/failure instead of delete + post

**Goal:** Task completion, failure, blocked, and stopped states edit the lifecycle message in place.

### Changes:

**`src/orchestrator.py`** — Result handling section (after `_execute_task` returns):

**Completion path** (~line 3574-3606):
- Before calling `_notify_brief()`, try to edit the lifecycle message with `format_task_completed_embed(task, agent, output)` and `TaskCompletedView`.
- If edit succeeds, skip posting a new completion embed to the channel via `_notify_brief`.
- Still post the detailed summary to the thread (via `thread_send`).
- Remove the `_task_started_messages.pop()` + `started_msg.delete()` cleanup (line 3835-3841) — we no longer delete, we edit in place.

**Failure path:**
- Same pattern — edit lifecycle message with `format_task_failed_embed()` + `TaskFailedView`.

**Blocked path:**
- Edit lifecycle message with `format_task_blocked_embed()`.

**`stop_task()`** (~line 499-510):
- Instead of posting "Task Stopped" as a new message and deleting the started message, edit the lifecycle message with a stopped/blocked embed.
- Remove the `_task_started_messages.pop()` + `started_msg.delete()` block.

**Cleanup:**
- Remove `_task_started_messages` dict entirely — all paths now use lifecycle message callbacks.
- Add cleanup to remove entries from `_task_lifecycle_messages` after terminal states (COMPLETED, FAILED, BLOCKED) to prevent memory leaks. After editing to a terminal state, remove the entry from the dict.

---

## Phase 5: Handle non-slash-command task creation paths

**Goal:** Tasks created via chat (Supervisor), auto-generated plan subtasks, and dependency-defined tasks also get lifecycle messages.

### Changes:

**`src/orchestrator.py`** — In `_execute_task()`:
- After the lifecycle edit attempt for Task Started, if no lifecycle message existed, and we fell back to posting a new Task Started message via `_notify_channel()`, **store that message** as the lifecycle message via `_store_lifecycle(task.id, started_msg)`. This way, the Task Started message becomes the lifecycle message for subsequent edits (completion/failure).

This handles all paths uniformly:
- **`/add-task` slash command:** Lifecycle message created at Task Added time (Phase 2), edited to Task Started, edited to Completed.
- **Chat-created tasks:** Lifecycle message created at Task Started time (this phase), edited to completion/failure.
- **Auto-generated plan subtasks:** Same as chat-created.
- **DEFINED to READY promotion:** Same as chat-created.

The only difference is that chat-created tasks won't show a "Task Added" to "Task Started" transition — they'll appear directly as "Task Started". This is acceptable since the chat response already confirms task creation.
