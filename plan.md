---
auto_tasks: true
---

# Plan: Improved Memory Integration for Agent Queue

## Background & Current State

### Current Memory System
The memory system (`src/memory.py`) uses **memsearch** for per-project semantic indexing:
- **Storage**: `~/.agent-queue/memory/{project_id}/tasks/{task_id}.md` — one file per completed task
- **Content**: Each memory file is a flat record: task title, metadata, summary, files changed
- **Recall**: At task start, semantic search over task memories returns top-k results injected as `attached_context`
- **Remember**: On task completion/failure, saves a new memory file and indexes it
- **Notes integration**: Notes from `~/.agent-queue/notes/{project_id}/` are optionally indexed alongside task memories (read-only; notes are never auto-generated)

### Key Limitations
1. **No project-level understanding** — Memory is just a bag of task summaries. There's no synthesized "understanding" of the project (architecture, patterns, conventions, key decisions). Each agent starts from scratch with only 5 task snippets.
2. **Memories grow unboundedly** — 147+ task memories for agent-queue alone. Semantic search helps but relevance degrades as volume grows. No compaction or summarization.
3. **Notes are passive** — Notes exist as a separate system. They're indexed for search but never auto-generated or updated by tasks. There's no feedback loop from task execution to project knowledge.
4. **No memory revision** — Tasks never update or refine existing memories. The `remember()` call only creates new files; it never consolidates or corrects prior understanding.
5. **Context is shallow** — The `attached_context` injection is a flat list of bullet points. There's no structured project context (e.g., "here's how this codebase is organized" or "these are the conventions we follow").

### Design Goals
- Each project should have a **living project profile** — a synthesized understanding that evolves with every task
- Tasks should **revise** the memory system's understanding, not just append to it
- Notes should be **tightly integrated** — tasks can generate notes, and notes inform project understanding
- Memory should help agents work more effectively by providing rich, curated project context rather than raw search results

---

## Phase 1: Project Profile — A Living Project Understanding

**Goal**: Introduce a per-project `profile.md` file that captures synthesized knowledge about the project: architecture, conventions, key patterns, recent decisions, and common pitfalls.

### Changes

**New file**: `~/.agent-queue/memory/{project_id}/profile.md`
- Structured markdown with sections: Overview, Architecture, Conventions, Key Decisions, Common Patterns, Pitfalls
- Seeded on project creation with basic info (repo URL, name, description)
- Indexed by memsearch alongside task memories

**`src/memory.py`**:
- Add `get_profile(project_id) -> str | None` — reads and returns the profile content
- Add `update_profile(project_id, new_content: str)` — writes updated profile, re-indexes
- Add `_profile_path(project_id) -> str` helper

**`src/orchestrator.py`**:
- In memory recall step, always prepend the project profile before search results
- Format: `## Project Profile\n{profile_content}\n\n## Relevant Context from Project Memory\n{search_results}`

**`src/config.py`** (`MemoryConfig`):
- Add `profile_enabled: bool = True` — toggle project profiles
- Add `profile_max_size: int = 5000` — max chars for profile (prevents unbounded growth)

**`src/command_handler.py`**:
- Add `view-profile` command — display current project profile
- Add `edit-profile` command — manually edit/replace profile content
- Add `regenerate-profile` command — force LLM regeneration from task history

---

## Phase 2: Post-Task Memory Revision via LLM

**Goal**: After each task completes, use an LLM call to revise the project profile based on what the task learned. This replaces the current append-only model with an evolve-and-refine approach.

### Changes

**`src/memory.py`**:
- Add `revise_profile(project_id, task, output, workspace_path) -> str | None`
  - Reads current profile
  - Reads the task's summary and files changed
  - Calls LLM with a prompt: "Given the current project understanding and what this task accomplished, produce an updated project profile. Add new insights, correct outdated information, and keep it concise."
  - Writes the updated profile back
  - Returns the diff/delta for logging
- Add `_build_revision_prompt(current_profile, task_summary, files_changed) -> str`

**`src/orchestrator.py`**:
- After `remember()` in task completion, call `revise_profile()`
- Non-fatal: wrap in try/except, log warnings on failure
- Only revise for COMPLETED tasks (not FAILED — failed tasks still get `remember()` but don't revise the profile since they may contain incorrect understanding)

**`src/config.py`** (`MemoryConfig`):
- Add `revision_enabled: bool = True` — toggle post-task revision
- Add `revision_provider: str = ""` — optional separate LLM provider for revision (defaults to main chat_provider)
- Add `revision_model: str = ""` — optional model override (e.g., use a cheaper model for revision)

**`src/prompts/`**:
- Add `memory_revision.py` or `memory_revision.txt` — the system/user prompts for the revision LLM call

### Revision Prompt Design
The prompt should instruct the LLM to:
1. Preserve existing accurate information
2. Add new architectural insights discovered by the task
3. Update conventions or patterns if the task established new ones
4. Note key decisions with rationale
5. Remove outdated information contradicted by recent work
6. Stay within the size limit

---

## Phase 3: Notes Integration — Bidirectional Flow

**Goal**: Create a bidirectional relationship between notes and memory. Tasks can auto-generate notes for significant discoveries, and notes are promoted into the project profile during revision.

### Changes

**`src/memory.py`**:
- Add `generate_task_notes(project_id, task, output) -> list[str]`
  - After task completion, assess if the task produced noteworthy insights (new API patterns, architectural decisions, gotchas discovered)
  - Uses LLM to decide if a note should be created and what it should contain
  - Returns list of note paths created
- Modify `revise_profile()` to also consider recent notes when updating the profile
  - Read notes from `~/.agent-queue/notes/{project_id}/`
  - Include them as additional context in the revision prompt

**`src/orchestrator.py`**:
- After `remember()` + `revise_profile()`, call `generate_task_notes()` if enabled
- Emit `note.created` events for any auto-generated notes (hooks can react)

**`src/command_handler.py`**:
- Modify `write-note` to trigger a profile revision (notes represent explicit user knowledge that should be absorbed)
- Add `promote-note` command — explicitly incorporate a note's content into the project profile

**`src/config.py`** (`MemoryConfig`):
- Add `auto_generate_notes: bool = False` — toggle auto-note generation (off by default, can be noisy)
- Add `notes_inform_profile: bool = True` — whether notes are included in profile revision context

### Note Categories
Auto-generated notes should be tagged with a category prefix in the filename:
- `architecture-*.md` — structural insights about the codebase
- `convention-*.md` — coding patterns and style decisions
- `decision-*.md` — key technical decisions with rationale
- `pitfall-*.md` — gotchas and things to avoid

---

## Phase 4: Memory Compaction & Lifecycle

**Goal**: Prevent unbounded memory growth by periodically compacting old task memories into summary documents, and aging out stale information.

### Changes

**`src/memory.py`**:
- Add `compact(project_id, workspace_path) -> dict`
  - Groups task memories by age: recent (< 7 days), medium (7-30 days), old (> 30 days)
  - Recent: kept as-is (full detail)
  - Medium: LLM-summarizes into weekly digest files (`~/.agent-queue/memory/{project_id}/digests/week-{date}.md`)
  - Old: Individual task files deleted after digesting; only digests remain
  - Returns stats: tasks compacted, digests created, files removed
- Add `_summarize_batch(task_memories: list[str]) -> str` — LLM call to create a digest

**`src/orchestrator.py`** or **`src/hooks.py`**:
- Add periodic compaction — runs on a configurable interval (default: daily)
- Could be implemented as a built-in hook or a direct orchestrator periodic task

**`src/config.py`** (`MemoryConfig`):
- `compact_enabled: bool = False` already exists — activate it
- `compact_interval_hours: int = 24` already exists
- Add `compact_recent_days: int = 7` — threshold for "recent" memories
- Add `compact_archive_days: int = 30` — threshold for archiving to digest

**`src/command_handler.py`**:
- Add `compact-memory` command — manual trigger for compaction
- Add `memory-stats` enhancement — show breakdown by age tier, digest count

---

## Phase 5: Enhanced Context Delivery

**Goal**: Improve how memory context is delivered to agents. Replace the flat bullet-point injection with structured, prioritized context.

### Changes

**`src/orchestrator.py`** (`_format_memory_context`):
- Restructure the context block into tiers:
  1. **Project Profile** (always included, top priority)
  2. **Relevant Notes** (matched by semantic search, high signal)
  3. **Recent Task Memories** (from last 5 tasks on this project, for continuity)
  4. **Semantic Search Results** (current behavior, but de-duplicated against above)
- Add token budget awareness: if total context exceeds a limit, trim lower-priority tiers

**`src/adapters/claude.py`**:
- Change `attached_context` rendering from bullet list to proper markdown sections
- Currently: `- {ctx}` per item → Change to: render each context block as its own section

**`src/memory.py`**:
- Add `build_context(project_id, task, workspace_path) -> MemoryContext`
  - Returns a structured object with `profile`, `notes`, `recent_tasks`, `search_results`
  - Each field is a string ready for injection
  - Handles all the prioritization and dedup logic

**`src/models.py`**:
- Add `MemoryContext` dataclass with typed fields for each tier

**`src/config.py`** (`MemoryConfig`):
- Add `context_max_tokens: int = 4000` — soft budget for total memory context
- Add `context_include_recent: int = 3` — number of recent same-project tasks to include
