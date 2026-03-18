---
auto_tasks: true
---

# Plan: Improved Memory Integration for Agent Queue (Revised)

## Background & Current State

### Current Memory System
The memory system (`src/memory.py`) uses **memsearch** for per-project semantic indexing:
- **Storage**: `~/.agent-queue/memory/{project_id}/tasks/{task_id}.md` — one file per completed task
- **Content**: Each memory file is a flat record: task title, metadata, summary, files changed
- **Recall**: At task start, semantic search over task memories returns top-k results injected as `attached_context`
- **Remember**: On task completion/failure, saves a new memory file and indexes it
- **Notes integration**: Notes from `~/.agent-queue/notes/{project_id}/` are optionally indexed alongside task memories (read-only; notes are never auto-generated)

### memsearch Capabilities (v0.1.16)
memsearch already provides compaction and other features we should leverage:
- **`MemSearch.compact()`** — LLM-powered chunk compression that writes summaries to daily `memory/YYYY-MM-DD.md` files. Supports OpenAI, Anthropic, and Gemini backends. Custom prompt templates and per-source filtering supported.
- **`compact_chunks()`** — lower-level function that takes chunk dicts and returns a compressed markdown summary via LLM
- **File watching** — `watch()` auto-indexes markdown file changes in real-time
- **Incremental indexing** — `index_file()` for single files, stale chunk cleanup built-in
- **No custom compaction logic needed** — agent-queue just needs a thin wrapper around memsearch's existing `compact()` method

### Key Limitations
1. **No project-level understanding** — Memory is just a bag of task summaries. No synthesized "understanding" of the project.
2. **Memories grow unboundedly** — memsearch has compaction built-in but agent-queue doesn't wire it up yet.
3. **Notes are passive** — Notes are indexed for search but never auto-generated or updated by tasks. No feedback loop.
4. **No memory revision** — Tasks never update or refine existing memories.
5. **Context is shallow** — Flat list of bullet points, no structured project context.

### Design Decisions

**Q: Is `profile.md` part of the memory state, or separate?**
**A: It IS part of the memory state.** The profile lives at `~/.agent-queue/memory/{project_id}/profile.md` — right alongside task memories. It is indexed by memsearch in the same collection. It is NOT a separate system. The profile is the crown jewel of each project's memory — it's the synthesized understanding that evolves as tasks complete.

**Q: Should we build custom memory compaction?**
**A: No — leverage memsearch.** memsearch v0.1.16 already provides `compact()` with LLM-powered summarization, daily log files, provider selection, custom prompts, and auto-reindexing. Agent-queue just needs a thin wrapper and a periodic trigger. No need for custom `_summarize_batch()` or digest directory management.

### Design Goals
- Each project should have a **living project profile** — a synthesized understanding that evolves with every task
- Profile is **part of memory state**, not a separate system
- Tasks should **revise** the memory system's understanding, not just append to it
- Notes should be **tightly integrated** — tasks can generate notes, and notes inform project understanding
- **Leverage memsearch's existing compaction** rather than building custom compaction logic

---

## Phase 1: Project Profile — A Living Project Understanding

**Goal**: Introduce a per-project `profile.md` file that captures synthesized knowledge about the project. The profile IS part of the memory state — it lives at `~/.agent-queue/memory/{project_id}/profile.md` and is indexed by memsearch just like task memories.

### Changes

**File location**: `~/.agent-queue/memory/{project_id}/profile.md`
- Structured markdown with sections: Overview, Architecture, Conventions, Key Decisions, Common Patterns, Pitfalls
- Seeded on first task completion with basic info derived from the task's context
- Indexed by memsearch alongside task memories (already in the indexed path since it's under the memory dir)
- Always injected into agent context as the FIRST thing, before search results

**`src/memory.py`**:
- Add `get_profile(project_id) -> str | None` — reads and returns the profile content
- Add `update_profile(project_id, new_content: str)` — writes updated profile, re-indexes via `instance.index_file()`
- Add `_profile_path(project_id) -> str` helper — returns `{data_dir}/memory/{project_id}/profile.md`

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
  - Reads current profile (or starts from a seed template if none exists)
  - Reads the task's summary and files changed
  - Calls LLM with a revision prompt: "Given the current project understanding and what this task accomplished, produce an updated project profile."
  - Writes the updated profile back via `update_profile()`
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
- Add `memory_revision.py` — the system/user prompts for the revision LLM call

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

## Phase 4: Memory Compaction via memsearch & Enhanced Context Delivery

**Goal**: Wire up memsearch's existing compaction and improve how memory context is delivered to agents. Consolidated from the old Phase 4 + Phase 5 since compaction is mostly done by memsearch already.

### Compaction Changes (leveraging memsearch)

memsearch's `MemSearch.compact()` already handles:
- Compressing indexed chunks into LLM-generated summaries
- Writing summaries to `memory/YYYY-MM-DD.md` daily log files
- Configurable LLM provider/model and custom prompt templates
- Auto-indexing the compact file after writing
- Per-source filtering (can compact only task memories, not profile)

**`src/memory.py`**:
- Add `compact(project_id, workspace_path) -> dict` — thin wrapper around `instance.compact()`
  - Passes through the configured LLM provider/model
  - Returns stats dict (summary length, output path)
  - Optionally filter by source to avoid compacting the profile itself

**`src/orchestrator.py`** or **`src/hooks.py`**:
- Add periodic compaction trigger — runs on configurable interval using existing `compact_interval_hours` config
- Could be a built-in hook or orchestrator periodic task

**`src/config.py`** (`MemoryConfig`):
- `compact_enabled: bool = False` already exists — just wire it up
- `compact_interval_hours: int = 24` already exists
- Add `compact_llm_provider: str = ""` — LLM for compaction (defaults to revision_provider or chat_provider)
- Add `compact_llm_model: str = ""` — model override for compaction

**`src/command_handler.py`**:
- Add `compact-memory` command — manual trigger for compaction
- Enhance `memory-stats` to show compact history

### Enhanced Context Delivery Changes

**`src/orchestrator.py`** (`_format_memory_context`):
- Restructure the context block into tiers:
  1. **Project Profile** (always included, top priority)
  2. **Relevant Notes** (matched by semantic search, high signal)
  3. **Recent Task Memories** (from last N tasks on this project, for continuity)
  4. **Semantic Search Results** (current behavior, de-duplicated against above)
- Add token budget awareness: if total context exceeds a limit, trim lower-priority tiers

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
