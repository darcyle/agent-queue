# Memsearch Integration Plan for Agent Queue

## Executive Summary

This document outlines the integration of [memsearch](https://github.com/zilliztech/memsearch) — a markdown-first semantic memory system by Zilliz — into agent-queue. Memsearch enables agents to persist, index, and retrieve contextual knowledge via hybrid vector + BM25 search over markdown files. Integrating it fills the critical gap in agent-queue: **agents currently have no semantic memory across tasks**. Notes exist but are unsearchable; task results are stored but never reused as context.

The integration will give agents automatic access to relevant historical context (past task results, project notes, design decisions) when executing new tasks, significantly improving output quality and reducing redundant work.

---

## Background: What is Memsearch?

### Core Design
- **Markdown is the source of truth** — memories are `.md` files, human-readable, git-friendly, zero vendor lock-in
- **Vector store is a derived index** — rebuildable at any time from the markdown files
- **SHA-256 content-hash dedup** — unchanged content is never re-embedded
- **Live file watcher** — auto-indexes changes via `watchdog` library
- **LLM-powered compaction** — compress old memories into summaries

### Architecture
```
Markdown files → Scanner → Chunker (heading-based split) → Dedup (SHA-256)
  → Embedding provider → Milvus upsert (dense vector + BM25 sparse)

Query → Embed query → Hybrid search (dense COSINE + BM25 sparse) → RRF reranking → Top-K results
```

### Key API Surface
```python
from memsearch import MemSearch

mem = MemSearch(
    paths=["./memory"],
    embedding_provider="openai",      # "openai", "google", "voyage", "ollama", "local"
    embedding_model=None,
    milvus_uri="~/.memsearch/milvus.db",  # Milvus Lite (embedded) or server URI
    collection="memsearch_chunks",
    max_chunk_size=1500,
    overlap_lines=2,
)

await mem.index()                           # Index all markdown files
await mem.index_file(path)                  # Index a single file
await mem.search(query, top_k=10)           # Semantic search → list[dict]
await mem.compact(source=None, ...)         # LLM-powered summarization
watcher = mem.watch(on_event=callback)      # Live file watching
mem.close()
```

### Storage Backends
| Mode | URI | Use Case |
|------|-----|----------|
| Milvus Lite | `~/.memsearch/milvus.db` (local file) | Single-user dev (Linux/macOS only) |
| Milvus Server | `http://localhost:19530` | Multi-agent, team environments |
| Zilliz Cloud | `https://...zillizcloud.com` | Production, fully managed |

### Embedding Providers
OpenAI (default, `text-embedding-3-small`), Google Gemini, Voyage AI, Ollama, Local (sentence-transformers). All behind a common `EmbeddingProvider` protocol.

---

## Current State: Agent Queue's Memory Gap

### What exists today
- **Project Notes** (`/notes/*.md`): Plain markdown files — no indexing, no search
- **Task Context** (`task_context` table): Arbitrary data blobs attached to tasks
- **Task Results** (`task_results` table): Execution summaries, files changed, tokens used
- **LLM Logging** (`llm_logger.py`): Full input/output capture to JSONL — no retrieval

### The gap
- No semantic search across historical task results
- No similarity matching for context injection
- No cross-project knowledge sharing
- Agents start every task with a blank slate (except explicitly attached context)
- Project notes exist but must be manually navigated — no relevance ranking

---

## Integration Architecture

### Design Principles
1. **Optional** — memsearch is disabled by default; zero impact when not configured
2. **Per-project isolation** — each project gets its own Milvus collection
3. **Zero orchestration overhead** — indexing is async/background; search adds <500ms to task startup
4. **Markdown-native** — leverage memsearch's markdown-first design with existing project notes
5. **No new services** — use Milvus Lite for single-node deployments; Milvus Server only for multi-node

### High-Level Data Flow
```
Task Completed
  → Orchestrator saves task summary as markdown to {workspace}/memory/{task_id}.md
  → MemSearch.index_file() indexes it (async, non-blocking)

Task Starting
  → Orchestrator calls MemSearch.search(task.description, top_k=5)
  → Relevant memories injected into TaskContext.attached_context
  → Agent receives contextual knowledge from past work

Background
  → FileWatcher monitors {workspace}/memory/ and {workspace}/notes/
  → Any markdown changes auto-indexed
```

### Component Diagram
```
┌─────────────────────────────────────────────────────────┐
│                     Orchestrator                         │
│                                                          │
│  _execute_task()                                         │
│    ├─ MemoryManager.recall(task) → relevant_context     │
│    ├─ Inject into TaskContext.attached_context           │
│    └─ Launch agent                                       │
│                                                          │
│  _handle_task_result()                                   │
│    ├─ MemoryManager.remember(task, output) → .md file   │
│    └─ Index new memory                                   │
│                                                          │
│  startup()                                               │
│    └─ MemoryManager.initialize() → index existing files │
└────────────────────┬────────────────────────────────────┘
                     │
          ┌──────────▼──────────┐
          │   MemoryManager     │  ← NEW MODULE (src/memory.py)
          │                     │
          │  - MemSearch per    │
          │    project           │
          │  - recall(task)     │
          │  - remember(task,   │
          │    output)           │
          │  - search(project,  │
          │    query)            │
          │  - compact(project) │
          │  - stats()          │
          └──────────┬──────────┘
                     │
          ┌──────────▼──────────┐
          │     memsearch       │  ← External dependency
          │   (MemSearch class) │
          │                     │
          │  - index()          │
          │  - search()         │
          │  - compact()        │
          │  - watch()          │
          └──────────┬──────────┘
                     │
          ┌──────────▼──────────┐
          │   Milvus (storage)  │
          │                     │
          │  Lite: local .db    │
          │  Server: standalone │
          │  Cloud: Zilliz      │
          └─────────────────────┘
```

---

## Detailed Integration Points

### 1. Memory Storage: What Gets Indexed

Four categories of content will be indexed as markdown files:

#### a. Task Result Summaries (automatic)
When a task completes, the orchestrator writes a structured markdown file:
```markdown
# Task: swift-falcon — Add user authentication
**Project:** my-project | **Type:** feature | **Status:** completed
**Date:** 2026-03-10 | **Tokens:** 45,230

## Summary
Implemented JWT-based authentication with refresh tokens...

## Files Changed
- src/auth/jwt.py (new)
- src/middleware/auth.py (modified)
- tests/test_auth.py (new)

## Acceptance Criteria
- [x] JWT token generation and validation
- [x] Refresh token rotation
- [x] Middleware integration

## Key Decisions
- Used PyJWT library over python-jose for simplicity
- Refresh tokens stored in Redis with 7-day TTL
```

**Location:** `{workspace}/memory/tasks/{task_id}.md`

#### b. Project Notes (existing, newly indexed)
The existing `notes/*.md` files are already written by users and agents. Memsearch indexes them automatically via file watching.

**Location:** `{workspace}/notes/*.md` (already exists)

#### c. Agent Session Transcripts (optional, compacted)
When enabled, key excerpts from agent sessions are saved and periodically compacted via memsearch's LLM summarization.

**Location:** `{workspace}/memory/sessions/{task_id}-session.md`

#### d. Manual Knowledge Base (user-created)
Users can add any `.md` files to the memory directory for project-specific knowledge (architecture docs, API specs, style guides).

**Location:** `{workspace}/memory/kb/*.md`

### 2. Memory Retrieval: When and How

#### a. Task Startup (automatic injection)
**Where:** `orchestrator.py` → `_execute_task()`, after workspace preparation, before agent launch.

```python
# In _execute_task(), after building TaskContext:
if self.memory_manager:
    memories = await self.memory_manager.recall(task, top_k=5)
    if memories:
        context_block = self._format_memories(memories)
        task_context.attached_context.append(context_block)
```

**Query strategy:** Use the task title + description as the search query. The hybrid search (vector similarity + BM25 keyword match) handles both semantic and exact-match relevance.

#### b. On-Demand Search (chat command)
**Where:** `command_handler.py` → new `_cmd_memory_search()` method.

Users (or the chat agent) can search project memory:
```
search memory in project my-project for "authentication middleware"
```

This exposes memsearch's search API through the existing command infrastructure.

#### c. Hook-Driven Recall (event-triggered)
**Where:** `hooks.py` → new context step type `memory_search`.

Hooks can include a memory search step in their context-gathering pipeline:
```yaml
hooks:
  - name: "context-enricher"
    trigger: { type: "event", event: "task_assigned" }
    context_steps:
      - type: memory_search
        query: "{{task.description}}"
        top_k: 3
    prompt_template: "Given these relevant memories: {{step_0}}..."
```

### 3. Memory Lifecycle

```
Task Created → (no memory action)
Task Assigned → recall(task) → inject context
Task In Progress → (agent works, may write to notes/)
Task Completed → remember(task, output) → index
Task Failed → remember(task, output) → index (failures are valuable context)
Periodic → compact(project) → summarize old memories
```

---

## Implementation Details

### New Module: `src/memory.py`

```python
"""Semantic memory manager for agent-queue using memsearch.

Provides per-project memory indexing and retrieval. Each project gets its own
Milvus collection and indexes markdown files from the workspace's memory/ and
notes/ directories.

Optional dependency — when memsearch is not installed or memory is not
configured, all operations are no-ops.
"""

from __future__ import annotations
import os
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

try:
    from memsearch import MemSearch
    MEMSEARCH_AVAILABLE = True
except ImportError:
    MEMSEARCH_AVAILABLE = False


@dataclass
class MemoryConfig:
    """Configuration for the memory subsystem."""
    enabled: bool = False
    embedding_provider: str = "openai"
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""  # supports ${ENV_VAR}
    milvus_uri: str = "~/.agent-queue/memsearch/milvus.db"
    milvus_token: str = ""
    max_chunk_size: int = 1500
    overlap_lines: int = 2
    auto_remember: bool = True       # auto-save task results as memories
    auto_recall: bool = True         # auto-inject memories at task start
    recall_top_k: int = 5            # number of memories to inject
    compact_enabled: bool = False    # periodic LLM compaction
    compact_interval_hours: int = 24
    index_notes: bool = True         # index project notes/ directory
    index_sessions: bool = False     # index session transcripts


class MemoryManager:
    """Manages per-project MemSearch instances and memory operations."""

    def __init__(self, config: MemoryConfig):
        self.config = config
        self._instances: dict[str, MemSearch] = {}  # project_id -> MemSearch
        self._watchers: dict[str, Any] = {}

    def _collection_name(self, project_id: str) -> str:
        """Deterministic collection name per project."""
        safe_id = project_id.replace("-", "_").replace(" ", "_")
        return f"aq_{safe_id}_memory"

    def _memory_paths(self, workspace_path: str) -> list[str]:
        """Directories to index for a workspace."""
        paths = [os.path.join(workspace_path, "memory")]
        if self.config.index_notes:
            notes_dir = os.path.join(workspace_path, "notes")
            if os.path.isdir(notes_dir):
                paths.append(notes_dir)
        return [p for p in paths if os.path.isdir(p)]

    async def get_instance(self, project_id: str, workspace_path: str) -> MemSearch | None:
        """Get or create a MemSearch instance for a project."""
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            return None

        if project_id not in self._instances:
            paths = self._memory_paths(workspace_path)
            # Ensure memory directory exists
            memory_dir = os.path.join(workspace_path, "memory", "tasks")
            os.makedirs(memory_dir, exist_ok=True)

            instance = MemSearch(
                paths=paths,
                embedding_provider=self.config.embedding_provider,
                embedding_model=self.config.embedding_model or None,
                embedding_base_url=self.config.embedding_base_url or None,
                embedding_api_key=self.config.embedding_api_key or None,
                milvus_uri=os.path.expanduser(self.config.milvus_uri),
                milvus_token=self.config.milvus_token or None,
                collection=self._collection_name(project_id),
                max_chunk_size=self.config.max_chunk_size,
                overlap_lines=self.config.overlap_lines,
            )
            self._instances[project_id] = instance
        return self._instances[project_id]

    async def recall(self, task, workspace_path: str, top_k: int | None = None) -> list[dict]:
        """Search for memories relevant to a task."""
        if not self.config.auto_recall:
            return []
        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return []
        k = top_k or self.config.recall_top_k
        query = f"{task.title} {task.description}"
        try:
            return await instance.search(query, top_k=k)
        except Exception as e:
            logger.warning(f"Memory recall failed for task {task.id}: {e}")
            return []

    async def remember(self, task, output, workspace_path: str) -> str | None:
        """Save a task result as a memory markdown file."""
        if not self.config.auto_remember:
            return None
        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return None
        # Write markdown file
        memory_path = os.path.join(workspace_path, "memory", "tasks", f"{task.id}.md")
        content = self._format_task_memory(task, output)
        os.makedirs(os.path.dirname(memory_path), exist_ok=True)
        with open(memory_path, "w") as f:
            f.write(content)
        # Index the new file
        try:
            await instance.index_file(memory_path)
        except Exception as e:
            logger.warning(f"Memory indexing failed for task {task.id}: {e}")
        return memory_path

    async def search(self, project_id: str, workspace_path: str,
                     query: str, top_k: int = 10) -> list[dict]:
        """Ad-hoc search across project memory."""
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return []
        return await instance.search(query, top_k=top_k)

    async def reindex(self, project_id: str, workspace_path: str) -> int:
        """Force full reindex of a project's memory."""
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return 0
        return await instance.index(force=True)

    async def stats(self, project_id: str, workspace_path: str) -> dict:
        """Get memory stats for a project."""
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return {"enabled": False}
        # Return collection stats from the store
        return {
            "enabled": True,
            "collection": self._collection_name(project_id),
            "milvus_uri": self.config.milvus_uri,
        }

    def _format_task_memory(self, task, output) -> str:
        """Format a task result as a memory markdown file."""
        import time
        date_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
        status = output.result.value if hasattr(output.result, 'value') else str(output.result)
        files = "\n".join(f"- {f}" for f in (output.files_changed or []))
        return f"""# Task: {task.id} — {task.title}

**Project:** {task.project_id} | **Type:** {task.task_type.value if task.task_type else 'unknown'} | **Status:** {status}
**Date:** {date_str} | **Tokens:** {output.tokens_used:,}

## Summary
{output.summary or 'No summary available.'}

## Files Changed
{files or 'No files changed.'}
"""

    async def close(self):
        """Shutdown all MemSearch instances."""
        for watcher in self._watchers.values():
            watcher.stop()
        for instance in self._instances.values():
            instance.close()
        self._instances.clear()
        self._watchers.clear()
```

### Configuration Addition: `config.py`

Add `MemoryConfig` to `AppConfig`:

```python
@dataclass
class MemoryConfig:
    enabled: bool = False
    embedding_provider: str = "openai"
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    milvus_uri: str = "~/.agent-queue/memsearch/milvus.db"
    milvus_token: str = ""
    max_chunk_size: int = 1500
    overlap_lines: int = 2
    auto_remember: bool = True
    auto_recall: bool = True
    recall_top_k: int = 5
    compact_enabled: bool = False
    compact_interval_hours: int = 24
    index_notes: bool = True
    index_sessions: bool = False
```

YAML configuration:
```yaml
memory:
  enabled: true
  embedding_provider: "openai"        # openai, google, voyage, ollama, local
  embedding_model: ""                  # empty = provider default
  embedding_api_key: "${OPENAI_API_KEY}"
  milvus_uri: "~/.agent-queue/memsearch/milvus.db"  # or http://milvus-server:19530
  auto_remember: true                  # save task results as memories
  auto_recall: true                    # inject memories at task startup
  recall_top_k: 5                      # memories per task
  compact_enabled: false               # LLM-powered memory compaction
  index_notes: true                    # index project notes/ directory
```

### Orchestrator Changes

**File:** `src/orchestrator.py`

#### Initialization
```python
class Orchestrator:
    def __init__(self, config: AppConfig, ...):
        # ... existing init ...
        self.memory_manager: MemoryManager | None = None
        if hasattr(config, 'memory') and config.memory.enabled:
            from src.memory import MemoryManager
            self.memory_manager = MemoryManager(config.memory)
```

#### Task Execution (`_execute_task`)
Insert memory recall between workspace preparation and agent launch:
```python
async def _execute_task(self, task: Task, agent: Agent):
    # ... existing workspace prep, git branch creation ...

    # Build TaskContext
    task_context = TaskContext(
        description=task.description,
        task_id=task.id,
        acceptance_criteria=criteria,
        test_commands=test_commands,
        checkout_path=workspace.workspace_path,
        branch_name=branch,
        attached_context=context_entries,
        mcp_servers=profile_mcps,
    )

    # NEW: Memory recall
    if self.memory_manager:
        memories = await self.memory_manager.recall(task, workspace.workspace_path)
        if memories:
            memory_block = self._format_memory_context(memories)
            task_context.attached_context.append(memory_block)

    # ... existing agent launch ...
```

#### Result Handling (`_handle_task_result`)
Insert memory save after successful result processing:
```python
async def _handle_task_result(self, task: Task, output: AgentOutput):
    # ... existing result handling, git commit, PR creation ...

    # NEW: Save task result as memory
    if self.memory_manager and output.result in (AgentResult.COMPLETED, AgentResult.FAILED):
        workspace = await self._get_workspace_for_task(task)
        if workspace:
            await self.memory_manager.remember(task, output, workspace.workspace_path)
```

#### Helper Method
```python
def _format_memory_context(self, memories: list[dict]) -> str:
    """Format search results as context for the agent."""
    lines = ["## Relevant Context from Project Memory\n"]
    for i, mem in enumerate(memories, 1):
        source = mem.get("source", "unknown")
        heading = mem.get("heading", "")
        content = mem.get("content", "")
        score = mem.get("score", 0)
        lines.append(f"### Memory {i} (relevance: {score:.2f})")
        lines.append(f"*Source: {source}*")
        if heading:
            lines.append(f"*Section: {heading}*\n")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)
```

### Command Handler Additions

**File:** `src/command_handler.py`

New commands:
- `_cmd_memory_search(args)` — search project memory
- `_cmd_memory_stats(args)` — show memory index statistics
- `_cmd_memory_reindex(args)` — force full reindex
- `_cmd_memory_compact(args)` — trigger LLM compaction

Tool definitions for the chat agent:
```python
{
    "name": "memory_search",
    "description": "Search project memory for relevant context. Returns semantically similar past task results, notes, and knowledge.",
    "parameters": {
        "project_id": {"type": "string", "required": True},
        "query": {"type": "string", "required": True},
        "top_k": {"type": "integer", "default": 5}
    }
}
```

### Hook Engine Enhancement

**File:** `src/hooks.py`

New context step type:
```python
async def _execute_memory_search_step(self, step_config: dict, context: dict) -> str:
    """Execute a memory_search context step."""
    project_id = context.get("project_id")
    query = self._render_template(step_config.get("query", ""), context)
    top_k = step_config.get("top_k", 3)
    if self.orchestrator.memory_manager:
        workspace = await self._get_project_workspace(project_id)
        results = await self.orchestrator.memory_manager.search(
            project_id, workspace, query, top_k
        )
        return "\n\n".join(r["content"] for r in results)
    return ""
```

---

## Storage Backend Considerations

### Milvus Lite (Recommended Default)
- **Pros:** Zero setup, embedded in process, single file storage
- **Cons:** Linux/macOS only (no Windows native), single-process access, no replication
- **Best for:** Single-node agent-queue deployments, development
- **File location:** `~/.agent-queue/memsearch/milvus.db`

### Milvus Server (Recommended for Production)
- **Pros:** Multi-process access, scalable, supports multiple agent-queue instances
- **Cons:** Requires Docker or dedicated server, more operational overhead
- **Best for:** Multi-node deployments, high-volume workloads
- **Setup:** `docker run -d --name milvus -p 19530:19530 milvusdb/milvus:latest`

### Zilliz Cloud (Enterprise)
- **Pros:** Fully managed, auto-scaling, zero ops
- **Cons:** Cost, network latency, data leaves premises
- **Best for:** Teams that want zero infrastructure management

### Recommendation
Start with Milvus Lite for simplicity. The `milvus_uri` config parameter makes switching backends a one-line config change with no code modifications.

### Data Volume Estimates
| Content Type | Per Task | 100 Tasks | 1000 Tasks |
|-------------|----------|-----------|------------|
| Task memories | ~1 KB | ~100 KB | ~1 MB |
| Chunks (avg 3/file) | 3 | 300 | 3,000 |
| Embedding storage | ~18 KB | ~1.8 MB | ~18 MB |
| Total Milvus DB | — | ~5 MB | ~50 MB |

Very manageable even for Milvus Lite's embedded mode.

---

## Configuration Options Summary

### Minimal Configuration (just enable it)
```yaml
memory:
  enabled: true
```
Uses OpenAI embeddings (requires `OPENAI_API_KEY`), Milvus Lite, auto-recall and auto-remember enabled.

### Local-Only Configuration (no API keys needed)
```yaml
memory:
  enabled: true
  embedding_provider: "local"    # sentence-transformers, runs on CPU
  milvus_uri: "~/.agent-queue/memsearch/milvus.db"
```

### Production Configuration
```yaml
memory:
  enabled: true
  embedding_provider: "openai"
  embedding_api_key: "${OPENAI_API_KEY}"
  milvus_uri: "http://milvus.internal:19530"
  recall_top_k: 5
  auto_remember: true
  auto_recall: true
  compact_enabled: true
  compact_interval_hours: 24
  index_notes: true
```

### Per-Project Override (future)
Projects could override global memory settings via project-level config:
```yaml
# In project creation or project config
projects:
  my-project:
    memory:
      enabled: true
      recall_top_k: 10          # more context for complex projects
      index_sessions: true      # also index session transcripts
```

---

## API Surface for Memory Operations

### Python API (internal)

```python
class MemoryManager:
    async def recall(task: Task, workspace_path: str, top_k: int = None) -> list[dict]
    async def remember(task: Task, output: AgentOutput, workspace_path: str) -> str | None
    async def search(project_id: str, workspace_path: str, query: str, top_k: int = 10) -> list[dict]
    async def reindex(project_id: str, workspace_path: str) -> int
    async def stats(project_id: str, workspace_path: str) -> dict
    async def close() -> None
```

### Discord Commands (user-facing)

| Command | Description |
|---------|-------------|
| `search memory in <project> for "<query>"` | Natural language search |
| `memory stats for <project>` | Show index statistics |
| `reindex memory for <project>` | Force full reindex |
| `compact memory for <project>` | Run LLM compaction |

### Chat Agent Tools

| Tool | Purpose |
|------|---------|
| `memory_search` | Search project memory by semantic query |
| `memory_stats` | Get memory index statistics for a project |
| `memory_reindex` | Force reindex of a project's memory |

---

## Testing Strategy

### Unit Tests

#### `tests/test_memory.py`
```python
class TestMemoryManager:
    """Unit tests with mocked memsearch dependency."""

    async def test_recall_returns_empty_when_disabled(self):
        """MemoryManager with enabled=False returns empty list."""

    async def test_recall_returns_empty_when_memsearch_not_installed(self):
        """Graceful degradation when memsearch package is absent."""

    async def test_remember_writes_markdown_file(self):
        """Task completion creates properly formatted markdown."""

    async def test_remember_indexes_file(self):
        """After writing markdown, index_file is called."""

    async def test_recall_uses_task_title_and_description(self):
        """Search query combines title + description."""

    async def test_collection_name_isolation(self):
        """Each project gets a unique collection name."""

    async def test_format_task_memory(self):
        """Verify markdown output format for task memories."""

    async def test_recall_handles_search_errors_gracefully(self):
        """Exceptions from memsearch.search don't propagate."""

    async def test_format_memory_context(self):
        """Memory results are formatted as readable context blocks."""
```

#### `tests/test_orchestrator_memory.py`
```python
class TestOrchestratorMemoryIntegration:
    """Test memory hooks in orchestrator lifecycle."""

    async def test_memory_injected_into_task_context(self):
        """When memories exist, they appear in attached_context."""

    async def test_no_memory_injection_when_disabled(self):
        """Memory disabled = no change to TaskContext."""

    async def test_task_result_saved_as_memory(self):
        """Completed tasks write memory markdown."""

    async def test_failed_task_result_saved_as_memory(self):
        """Failed tasks also create memory entries."""

    async def test_memory_recall_does_not_block_execution(self):
        """Memory errors don't prevent task execution."""
```

### Integration Tests

#### `tests/test_memory_integration.py`
```python
@pytest.mark.integration
class TestMemoryEndToEnd:
    """End-to-end tests requiring memsearch installed + Milvus Lite."""

    async def test_index_and_search_roundtrip(self):
        """Write a markdown file, index it, search for it."""

    async def test_remember_then_recall(self):
        """Complete a task, then verify recall finds it for a similar task."""

    async def test_multiple_projects_isolated(self):
        """Memories from project A don't appear in project B searches."""

    async def test_notes_directory_indexed(self):
        """Files in notes/ are searchable via memory."""

    async def test_reindex_after_file_deletion(self):
        """Deleted files are cleaned from the index."""
```

### Chat Eval Tests

#### `tests/chat_eval/test_cases/memory.py`
```python
MEMORY_TEST_CASES = [
    {
        "input": "search memory in test-project for 'authentication'",
        "expected_tool": "memory_search",
        "expected_args": {"project_id": "test-project", "query": "authentication"},
    },
    {
        "input": "show memory stats for test-project",
        "expected_tool": "memory_stats",
        "expected_args": {"project_id": "test-project"},
    },
]
```

### Test Infrastructure

- **Fixtures:** Provide a temp workspace with pre-populated memory/ directory
- **Mock memsearch:** For unit tests, mock the `MemSearch` class to avoid Milvus dependency
- **Milvus Lite:** For integration tests, use Milvus Lite with a temp DB file
- **Markers:** `@pytest.mark.integration` for tests requiring real memsearch

---

## Files to Create or Modify

### New Files
| File | Purpose |
|------|---------|
| `src/memory.py` | MemoryManager class — core integration module |
| `tests/test_memory.py` | Unit tests for MemoryManager |
| `tests/test_memory_integration.py` | Integration tests (requires memsearch) |
| `tests/chat_eval/test_cases/memory.py` | Chat eval test cases |

### Modified Files
| File | Changes |
|------|---------|
| `src/config.py` | Add `MemoryConfig` dataclass, add `memory` field to `AppConfig`, parse `memory` section in `load_config()` |
| `src/orchestrator.py` | Initialize `MemoryManager`, add recall in `_execute_task()`, add remember in `_handle_task_result()`, add `_format_memory_context()` helper |
| `src/command_handler.py` | Add `_cmd_memory_search()`, `_cmd_memory_stats()`, `_cmd_memory_reindex()` commands |
| `src/chat_agent.py` | Add `memory_search`, `memory_stats` tool definitions |
| `src/hooks.py` | Add `memory_search` context step type |
| `pyproject.toml` or `requirements.txt` | Add `memsearch` as optional dependency |
| `specs/config.md` | Document memory configuration section |

---

## Effort Estimate

| Phase | Effort | Description |
|-------|--------|-------------|
| Phase 1: Core Module | 2-3 hours | `src/memory.py` with MemoryManager, MemoryConfig |
| Phase 2: Config Integration | 1 hour | Add to config.py, AppConfig, load_config() |
| Phase 3: Orchestrator Hooks | 2-3 hours | recall in _execute_task, remember in _handle_task_result |
| Phase 4: Command Handler | 2 hours | memory_search, memory_stats, memory_reindex commands |
| Phase 5: Chat Agent Tools | 1 hour | Tool definitions and response formatting |
| Phase 6: Hook Engine | 1 hour | memory_search context step |
| Phase 7: Testing | 3-4 hours | Unit tests, integration tests, chat eval |
| Phase 8: Documentation | 1 hour | Config spec updates, README section |
| **Total** | **~13-16 hours** | ~2 days of focused work |

---

## Risks and Mitigations

### Risk 1: Milvus Lite Not Available on Windows
**Impact:** Medium — agent-queue runs on WSL2 but some dev setups may be native Windows.
**Mitigation:** Document WSL2 requirement for Milvus Lite. Support `milvus_uri` pointing to a remote Milvus server as alternative. Graceful fallback when memsearch import fails.

### Risk 2: Embedding API Costs
**Impact:** Low — embeddings are cheap ($0.02/1M tokens for OpenAI text-embedding-3-small). A typical task memory is ~500 tokens to embed.
**Mitigation:** SHA-256 dedup prevents re-embedding unchanged content. Local embedding provider (`sentence-transformers`) available for zero-cost operation. Budget tracking integration possible.

### Risk 3: Latency Impact on Task Startup
**Impact:** Low — typical search query takes <200ms against Milvus Lite.
**Mitigation:** Memory recall is async and has a timeout. On failure or timeout, task executes without memory context (graceful degradation). Never blocks the orchestrator loop.

### Risk 4: Memory Pollution / Irrelevant Context
**Impact:** Medium — injecting irrelevant memories could confuse agents.
**Mitigation:** Start with conservative `recall_top_k=5`. Include relevance scores in injected context. Allow users to tune per-project. Monitor agent performance with/without memory injection.

### Risk 5: Memsearch Dependency Stability
**Impact:** Low-Medium — memsearch is version 0.1.x, still evolving.
**Mitigation:** Import is conditional (`try/except ImportError`). All memsearch calls wrapped in try/except. Pin dependency version. The MemoryManager abstraction layer isolates agent-queue from memsearch internals.

### Risk 6: Storage Growth
**Impact:** Low — estimated ~50 MB per 1000 tasks.
**Mitigation:** Compaction feature summarizes old memories. Optional `index_sessions` (default off) controls the largest data source. DB file location is configurable.

### Risk 7: Collection Schema Migration
**Impact:** Low — memsearch manages its own Milvus schema.
**Mitigation:** `reindex` command allows full rebuild. Memsearch's content-hash dedup handles schema changes gracefully.

---

## Future Enhancements (Out of Scope)

1. **Cross-project memory** — shared collection for organizational knowledge
2. **Memory importance scoring** — weight recent, successful task memories higher
3. **Agent self-reflection** — agents tag memories with quality/usefulness ratings
4. **Memory-aware scheduling** — prefer assigning tasks to agents that worked on related tasks
5. **RAG over codebase** — index actual source code files (not just notes/memories)
6. **Memory dashboard** — web UI for browsing and managing project memory
7. **MCP server mode** — expose memory as an MCP tool for agents to query directly during execution

---

## Phase 1: Core Memory Module

Create `src/memory.py` with `MemoryManager` and `MemoryConfig` classes. Implement `recall()`, `remember()`, `search()`, `reindex()`, `stats()`, and `close()` methods. Handle graceful degradation when memsearch is not installed.

## Phase 2: Configuration Integration

Add `MemoryConfig` dataclass to `src/config.py`. Add `memory: MemoryConfig` field to `AppConfig`. Parse `memory:` YAML section in `load_config()`. Add `memsearch` as an optional dependency.

## Phase 3: Orchestrator Lifecycle Hooks

Modify `src/orchestrator.py` to initialize `MemoryManager` at startup. Add memory recall in `_execute_task()` to inject context into `TaskContext`. Add memory save in `_handle_task_result()` for completed and failed tasks. Add `_format_memory_context()` helper.

## Phase 4: Command Handler and Chat Agent

Add `_cmd_memory_search()`, `_cmd_memory_stats()`, `_cmd_memory_reindex()` to `src/command_handler.py`. Add corresponding tool definitions to `src/chat_agent.py`. Format memory search results for Discord display.

## Phase 5: Hook Engine and Testing

Add `memory_search` context step type to `src/hooks.py`. Write unit tests (`tests/test_memory.py`), integration tests (`tests/test_memory_integration.py`), and chat eval tests. Update specs and documentation.
