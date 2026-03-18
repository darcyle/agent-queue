# Plan: Index Project Specs & Documentation in Memory System

## Problem

Agents executing tasks for agent-queue have no knowledge of how agent-queue itself works. The specs (`specs/`) and documentation (`CLAUDE.md`, `README.md`) contain the authoritative design knowledge, but none of it is indexed in the memory system. When an agent needs to understand how a subsystem works to implement a change, it has to rediscover everything from code.

## Design Constraints

1. **Zero duplication** — Specs and docs already live in the workspace repo. We must NOT copy them anywhere. Read directly from the workspace path.
2. **Always up-to-date** — Since we read from the workspace (which gets `git fetch` + `rebase` before each task), the indexed content automatically reflects the latest merged changes.
3. **Opt-in per project** — Not all projects have meaningful specs. This should be configurable.
4. **Graceful degradation** — If the workspace isn't available or specs don't exist, skip silently.

## Architecture

The solution is simple: **add workspace documentation directories to the MemSearch index paths**.

`MemSearch` already indexes all `.md` files in the directories passed to its `paths` parameter. Currently `_memory_paths()` returns:
- `~/.agent-queue/memory/{project_id}/` (task memories)
- `~/.agent-queue/notes/{project_id}/` (if `index_notes=True`)

We add:
- `{workspace}/specs/` (if `index_specs=True` and directory exists)
- `{workspace}/CLAUDE.md` (if `index_project_docs=True` and file exists)
- `{workspace}/README.md` (same)
- `{workspace}/docs/` (same, if directory exists)

Because MemSearch reads files directly from disk, there is **zero duplication**. The workspace is the single source of truth.

### Context Delivery

Spec content flows through the existing Tier 4 (semantic search results) automatically — when an agent's task description matches spec content, those chunks surface. No new tier needed since specs are reference material, not high-priority like profile or notes.

However, we should add a **Tier 1.5: Project Docs** section that always includes CLAUDE.md content (truncated) as foundational context, similar to how profile is always included. This ensures every agent knows the project basics.

### Staleness Prevention

- Workspace files are updated by `git fetch + rebase` before each task execution (orchestrator already does this)
- MemSearch re-indexes on `get_instance()` creation (first access per project per process lifetime)
- `reindex` command forces a full re-index
- New config option `reindex_on_task_start: bool = False` can force re-index before each task (expensive but guarantees freshness)

---

## Phase 1: Config & Path Registration

**Files:** `src/config.py`, `src/memory.py`

1. Add to `MemoryConfig`:
   ```python
   index_specs: bool = True          # Index workspace specs/ directory
   index_project_docs: bool = True   # Index CLAUDE.md, README.md, docs/
   project_docs_paths: list[str] = field(default_factory=lambda: ["CLAUDE.md", "README.md"])
   specs_dir: str = "specs"          # Relative path to specs directory within workspace
   ```

2. Update `_memory_paths()` in `MemoryManager` to include workspace-relative paths:
   ```python
   def _memory_paths(self, project_id: str, workspace_path: str) -> list[str]:
       paths = [self._project_memory_dir(project_id)]
       if self.config.index_notes:
           notes_dir = self._notes_dir(project_id)
           if os.path.isdir(notes_dir):
               paths.append(notes_dir)
       # Workspace specs
       if self.config.index_specs and workspace_path:
           specs = os.path.join(workspace_path, self.config.specs_dir)
           if os.path.isdir(specs):
               paths.append(specs)
       # Workspace docs directory
       if self.config.index_project_docs and workspace_path:
           docs = os.path.join(workspace_path, "docs")
           if os.path.isdir(docs):
               paths.append(docs)
       return paths
   ```

3. Handle individual doc files (CLAUDE.md, README.md) — these need special treatment since MemSearch `paths` expects directories. Two options:
   - **Option A:** Create a thin symlink directory (violates zero-duplication spirit)
   - **Option B (preferred):** After `get_instance()`, call `instance.index_file()` for each individual doc file. Add a helper `_index_project_doc_files()` that indexes `CLAUDE.md`, `README.md` etc. directly.

## Phase 2: Auto-Index on Instance Creation

**Files:** `src/memory.py`

1. Add `_index_project_doc_files()` method:
   ```python
   async def _index_project_doc_files(self, instance: Any, workspace_path: str) -> None:
       """Index individual project documentation files (CLAUDE.md, README.md, etc.)."""
       if not self.config.index_project_docs or not workspace_path:
           return
       for rel_path in self.config.project_docs_paths:
           full_path = os.path.join(workspace_path, rel_path)
           if os.path.isfile(full_path):
               try:
                   await instance.index_file(full_path)
               except Exception as e:
                   logger.debug(f"Failed to index {rel_path}: {e}")
   ```

2. Call `_index_project_doc_files()` after instance creation in `get_instance()`.

3. Track indexed file modification times to avoid re-indexing unchanged files:
   ```python
   _doc_file_mtimes: dict[str, dict[str, float]] = {}  # project_id -> {path: mtime}
   ```

## Phase 3: Context Delivery Enhancement

**Files:** `src/memory.py`, `src/models.py`

1. Add `project_docs: str = ""` field to `MemoryContext` dataclass — sits between profile and notes in priority.

2. In `build_context()`, after loading the profile (Tier 1), add Tier 1.5:
   ```python
   # Tier 1.5: Project Documentation (CLAUDE.md summary)
   if self.config.index_project_docs and workspace_path:
       claude_md = os.path.join(workspace_path, "CLAUDE.md")
       if os.path.isfile(claude_md):
           with open(claude_md) as f:
               content = f.read()
           # Truncate to reasonable size for context
           if len(content) > 3000:
               content = content[:3000] + "\n\n[truncated]"
           ctx.project_docs = content
   ```

3. Update `MemoryContext.to_context_block()` to include the new tier:
   ```markdown
   ## Project Documentation
   {project_docs content}
   ```

## Phase 4: Spec for the Feature

**Files:** `specs/memory.md` (new or append to existing)

Document the feature in the specs so it's self-referentially included in future indexing:
- Config options and defaults
- What gets indexed and from where
- Staleness model (relies on workspace git sync)
- How spec content surfaces in agent context (semantic search)
