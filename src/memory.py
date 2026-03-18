"""Semantic memory manager for agent-queue using memsearch.

Provides per-project memory indexing and retrieval. Each project gets its own
Milvus collection and indexes markdown files from the workspace's memory/ and
notes/ directories.

Features (phases from the memory improvement plan):
- **Project Profile** — A living ``profile.md`` per project that captures
  synthesized knowledge (architecture, conventions, decisions, patterns).
- **Post-Task Revision** — After each completed task, an LLM call revises
  the project profile based on what the task learned.
- **Notes Integration** — Tasks can auto-generate categorized notes, and
  notes feed back into profile revision.
- **Memory Compaction** — Thin wrapper around memsearch's built-in
  ``compact()`` for periodic LLM-powered summarization.
- **Enhanced Context Delivery** — Tiered, prioritized context injection:
  profile > notes > recent tasks > semantic search results.

Optional dependency — when memsearch is not installed or memory is not
configured, all operations are no-ops. All memsearch calls are wrapped in
try/except for resilience: a memory subsystem failure never blocks task
execution.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
from typing import Any

from src.config import MemoryConfig
from src.models import MemoryContext

logger = logging.getLogger(__name__)

try:
    from memsearch import MemSearch

    MEMSEARCH_AVAILABLE = True
except ImportError:
    MemSearch = None  # type: ignore[assignment,misc]
    MEMSEARCH_AVAILABLE = False


class MemoryManager:
    """Manages per-project MemSearch instances and memory operations.

    Each project gets its own Milvus collection (``aq_{project_id}_memory``)
    so memories are fully isolated between projects. Instances are created
    lazily on first access.

    Memory files are stored centrally under ``{data_dir}/memory/{project_id}/``
    to keep all persistent data under ``~/.agent-queue``.

    When memsearch is not installed or the config has ``enabled=False``,
    every public method degrades gracefully (returns empty lists, None, etc.)
    without raising exceptions.
    """

    def __init__(self, config: MemoryConfig, storage_root: str = "") -> None:
        self.config = config
        self._storage_root = os.path.expanduser(
            storage_root or "~/.agent-queue"
        )
        self._instances: dict[str, Any] = {}  # project_id -> MemSearch
        self._watchers: dict[str, Any] = {}
        self._last_compact: dict[str, float] = {}  # project_id -> timestamp

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collection_name(self, project_id: str) -> str:
        """Deterministic, Milvus-safe collection name per project."""
        safe_id = project_id.replace("-", "_").replace(" ", "_")
        return f"aq_{safe_id}_memory"

    def _project_memory_dir(self, project_id: str) -> str:
        """Central memory storage directory for a project.

        Returns ``{data_dir}/memory/{project_id}/``.
        """
        return os.path.join(self._storage_root, "memory", project_id)

    def _profile_path(self, project_id: str) -> str:
        """Path to the project profile file.

        Returns ``{data_dir}/memory/{project_id}/profile.md``.
        """
        return os.path.join(self._project_memory_dir(project_id), "profile.md")

    def _notes_dir(self, project_id: str) -> str:
        """Path to the project notes directory."""
        return os.path.join(self._storage_root, "notes", project_id)

    def _memory_paths(self, project_id: str, workspace_path: str) -> list[str]:
        """Directories to index for a project.

        Always includes the central ``{data_dir}/memory/{project_id}/``
        directory (created if absent). Optionally includes the project's
        ``notes/`` directory when ``index_notes`` is enabled.
        """
        paths = [self._project_memory_dir(project_id)]
        if self.config.index_notes:
            notes_dir = self._notes_dir(project_id)
            if os.path.isdir(notes_dir):
                paths.append(notes_dir)
        return paths

    def _resolve_milvus_uri(self) -> str:
        """Expand ``~`` in Milvus Lite file paths and ensure parent dir exists."""
        uri = os.path.expanduser(self.config.milvus_uri)
        # For file-based Milvus Lite URIs, create the parent directory
        if not uri.startswith("http"):
            os.makedirs(os.path.dirname(uri), exist_ok=True)
        return uri

    # ------------------------------------------------------------------
    # Instance management
    # ------------------------------------------------------------------

    async def get_instance(self, project_id: str, workspace_path: str) -> Any | None:
        """Get or create a MemSearch instance for a project.

        Returns ``None`` when memsearch is unavailable or the subsystem is
        disabled — callers should treat ``None`` as "memory not available".
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            return None

        if project_id in self._instances:
            return self._instances[project_id]

        try:
            # Ensure the central memory/tasks directory exists for remember()
            memory_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
            os.makedirs(memory_dir, exist_ok=True)

            paths = self._memory_paths(project_id, workspace_path)

            instance = MemSearch(
                paths=paths,
                embedding_provider=self.config.embedding_provider,
                embedding_model=self.config.embedding_model or None,
                embedding_base_url=self.config.embedding_base_url or None,
                embedding_api_key=self.config.embedding_api_key or None,
                milvus_uri=self._resolve_milvus_uri(),
                milvus_token=self.config.milvus_token or None,
                collection=self._collection_name(project_id),
                max_chunk_size=self.config.max_chunk_size,
                overlap_lines=self.config.overlap_lines,
            )
            self._instances[project_id] = instance
            return instance
        except Exception as e:
            logger.error(f"Failed to create MemSearch instance for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 1: Project Profile
    # ------------------------------------------------------------------

    async def get_profile(self, project_id: str) -> str | None:
        """Read and return the project profile content.

        Returns ``None`` if profiles are disabled or no profile exists yet.
        """
        if not self.config.profile_enabled:
            return None

        path = self._profile_path(project_id)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read profile for project {project_id}: {e}")
            return None

    async def update_profile(
        self, project_id: str, new_content: str, workspace_path: str = ""
    ) -> str | None:
        """Write updated profile content and re-index it.

        Truncates to ``profile_max_size`` if the content exceeds the limit.
        Returns the file path on success, ``None`` otherwise.
        """
        if not self.config.profile_enabled:
            return None

        # Enforce size limit
        max_size = self.config.profile_max_size
        if len(new_content) > max_size:
            # Truncate at last complete line within budget
            truncated = new_content[:max_size]
            last_newline = truncated.rfind("\n")
            if last_newline > 0:
                new_content = truncated[:last_newline] + "\n"
            else:
                new_content = truncated

        path = self._profile_path(project_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(new_content)
        except Exception as e:
            logger.warning(f"Failed to write profile for project {project_id}: {e}")
            return None

        # Re-index the profile file if we have an instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(f"Profile indexing failed for project {project_id}: {e}")

        return path

    # ------------------------------------------------------------------
    # Phase 2: Post-Task Profile Revision
    # ------------------------------------------------------------------

    async def revise_profile(
        self,
        project_id: str,
        task: Any,
        output: Any,
        workspace_path: str,
    ) -> str | None:
        """Revise the project profile based on a completed task.

        Reads the current profile (or seeds from a template), calls an LLM
        to produce an updated version incorporating what the task learned,
        and writes the result back. Only called for COMPLETED tasks.

        Returns the updated profile content on success, ``None`` on failure
        or if revision is disabled.
        """
        if not self.config.revision_enabled or not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import (
            PROFILE_SEED_TEMPLATE,
            REVISION_SYSTEM_PROMPT,
            REVISION_USER_PROMPT,
        )

        # Get current profile or seed
        current_profile = await self.get_profile(project_id)
        if not current_profile:
            current_profile = PROFILE_SEED_TEMPLATE

        # Build the revision prompt
        task_type = task.task_type.value if (task.task_type and hasattr(task.task_type, "value")) else "unknown"
        status = output.result.value if hasattr(output.result, "value") else str(output.result)
        summary = output.summary or "No summary available."
        files_changed = "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."

        # Optionally include recent notes in revision context
        notes_section = ""
        if self.config.notes_inform_profile:
            notes_content = self._read_recent_notes(project_id, max_notes=5)
            if notes_content:
                notes_section = f"## Recent Project Notes\n{notes_content}\n\n"

        system_prompt = REVISION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = REVISION_USER_PROMPT.format(
            current_profile=current_profile,
            task_title=task.title,
            task_type=task_type,
            task_status=status,
            task_summary=summary,
            files_changed=files_changed,
            notes_section=notes_section,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for profile revision")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            # Extract text from response
            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile revision")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(f"Profile revised for project {project_id} after task {task.id}")
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Profile revision failed for project {project_id}: {e}")
            return None

    def _get_revision_provider(self) -> Any | None:
        """Create a chat provider for revision/note-generation LLM calls.

        Uses revision_provider/revision_model config if set, otherwise
        falls back to the main chat_provider settings.
        """
        try:
            from src.chat_providers import create_chat_provider
            from src.config import ChatProviderConfig

            provider_name = self.config.revision_provider or "anthropic"
            model_name = self.config.revision_model or ""

            provider_config = ChatProviderConfig(
                provider=provider_name,
                model=model_name,
            )
            return create_chat_provider(provider_config)
        except Exception as e:
            logger.warning(f"Failed to create revision LLM provider: {e}")
            return None

    def _read_recent_notes(self, project_id: str, max_notes: int = 5) -> str:
        """Read the most recent notes for a project.

        Returns formatted markdown of the most recent notes, or empty string.
        """
        notes_dir = self._notes_dir(project_id)
        if not os.path.isdir(notes_dir):
            return ""

        try:
            note_files = sorted(
                glob.glob(os.path.join(notes_dir, "*.md")),
                key=os.path.getmtime,
                reverse=True,
            )[:max_notes]

            if not note_files:
                return ""

            sections = []
            for nf in note_files:
                with open(nf) as f:
                    content = f.read().strip()
                basename = os.path.basename(nf)
                sections.append(f"### {basename}\n{content}")

            return "\n\n".join(sections)
        except Exception as e:
            logger.warning(f"Failed to read notes for project {project_id}: {e}")
            return ""

    async def regenerate_profile(
        self,
        project_id: str,
        workspace_path: str,
    ) -> str | None:
        """Force-regenerate the project profile from task history.

        Reads all stored task memories and recent notes, then calls an LLM
        to synthesize a brand-new profile from scratch. This replaces the
        existing profile entirely.

        Returns the new profile content on success, ``None`` on failure.
        """
        if not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import (
            REGENERATION_SYSTEM_PROMPT,
            REGENERATION_USER_PROMPT,
        )

        # Gather all task memory files
        tasks_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
        task_summaries: list[str] = []
        if os.path.isdir(tasks_dir):
            task_files = sorted(
                glob.glob(os.path.join(tasks_dir, "*.md")),
                key=os.path.getmtime,
            )
            for tf in task_files:
                try:
                    with open(tf) as f:
                        content = f.read().strip()
                    if content:
                        task_summaries.append(content)
                except Exception:
                    pass

        if not task_summaries:
            logger.info(f"No task history for project {project_id}, cannot regenerate profile")
            return None

        # Optionally include recent notes
        notes_section = ""
        if self.config.notes_inform_profile:
            notes_content = self._read_recent_notes(project_id, max_notes=10)
            if notes_content:
                notes_section = f"## Recent Project Notes\n{notes_content}\n\n"

        system_prompt = REGENERATION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = REGENERATION_USER_PROMPT.format(
            task_count=len(task_summaries),
            task_summaries="\n\n---\n\n".join(task_summaries),
            notes_section=notes_section,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for profile regeneration")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile during regeneration")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(f"Profile regenerated for project {project_id} from {len(task_summaries)} tasks")
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Profile regeneration failed for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 3: Notes Integration
    # ------------------------------------------------------------------

    async def generate_task_notes(
        self, project_id: str, task: Any, output: Any, workspace_path: str
    ) -> list[str]:
        """Auto-generate notes from a completed task if warranted.

        Uses an LLM to assess whether the task produced noteworthy insights
        and, if so, creates categorized note files. Returns a list of note
        file paths created, or an empty list.
        """
        if not self.config.auto_generate_notes:
            return []

        from src.prompts.memory_revision import (
            NOTE_GENERATION_SYSTEM_PROMPT,
            NOTE_GENERATION_USER_PROMPT,
        )

        task_type = task.task_type.value if (task.task_type and hasattr(task.task_type, "value")) else "unknown"
        summary = output.summary or "No summary available."
        files_changed = "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."

        user_prompt = NOTE_GENERATION_USER_PROMPT.format(
            task_title=task.title,
            task_type=task_type,
            project_id=project_id,
            task_summary=summary,
            files_changed=files_changed,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                return []

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=NOTE_GENERATION_SYSTEM_PROMPT,
                max_tokens=1024,
            )

            # Extract text and parse JSON
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON array from the response
            response_text = response_text.strip()
            # Handle markdown code fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                # Remove first and last lines (code fences)
                lines = [l for l in lines if not l.strip().startswith("```")]
                response_text = "\n".join(lines)

            notes = json.loads(response_text)
            if not isinstance(notes, list) or not notes:
                return []

            notes_dir = self._notes_dir(project_id)
            os.makedirs(notes_dir, exist_ok=True)

            created_paths: list[str] = []
            timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

            for note in notes:
                category = note.get("category", "general")
                slug = note.get("slug", "note")
                content = note.get("content", "")
                if not content:
                    continue

                filename = f"{category}-{slug}-{timestamp}.md"
                path = os.path.join(notes_dir, filename)

                with open(path, "w") as f:
                    f.write(content)
                created_paths.append(path)

                # Index the new note file
                instance = await self.get_instance(project_id, workspace_path)
                if instance:
                    try:
                        await instance.index_file(path)
                    except Exception as e:
                        logger.warning(f"Note indexing failed for {path}: {e}")

            if created_paths:
                logger.info(
                    f"Generated {len(created_paths)} note(s) for project {project_id} "
                    f"from task {task.id}"
                )

            return created_paths

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse note generation response: {e}")
            return []
        except Exception as e:
            logger.warning(f"Note generation failed for project {project_id}: {e}")
            return []

    # ------------------------------------------------------------------
    # Phase 4: Memory Compaction & Enhanced Context Delivery
    # ------------------------------------------------------------------

    async def compact(self, project_id: str, workspace_path: str) -> dict:
        """Run memsearch compaction for a project.

        Thin wrapper around ``instance.compact()``. Returns a stats dict
        with the compaction result, or an error dict on failure.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return {"error": "MemSearch instance not available"}

        if not hasattr(instance, "compact"):
            return {"error": "memsearch version does not support compact()"}

        try:
            # Determine LLM provider for compaction
            provider = self.config.compact_llm_provider or self.config.revision_provider or "anthropic"
            model = self.config.compact_llm_model or self.config.revision_model or ""

            kwargs: dict[str, Any] = {"provider": provider}
            if model:
                kwargs["model"] = model

            result = await instance.compact(**kwargs)
            self._last_compact[project_id] = time.time()

            return {
                "status": "compacted",
                "project_id": project_id,
                "result": str(result) if result else "ok",
            }
        except Exception as e:
            logger.warning(f"Memory compaction failed for project {project_id}: {e}")
            return {"error": str(e)}

    async def build_context(
        self, project_id: str, task: Any, workspace_path: str
    ) -> MemoryContext:
        """Build a structured, tiered memory context for a task.

        Returns a ``MemoryContext`` with fields for each priority tier:
        1. Project profile (always included, highest priority)
        2. Relevant notes (semantic search matched)
        3. Recent task memories (for continuity)
        4. Semantic search results (de-duplicated against above)

        The orchestrator uses this instead of the old flat recall approach.
        """
        ctx = MemoryContext()

        # Tier 1: Project Profile
        if self.config.profile_enabled:
            profile = await self.get_profile(project_id)
            if profile:
                ctx.profile = profile

        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return ctx

        query = f"{task.title} {task.description}"
        seen_sources: set[str] = set()

        # Tier 2: Relevant Notes (search notes directory specifically)
        if self.config.index_notes:
            try:
                notes_results = await instance.search(query, top_k=3)
                notes_lines = []
                if notes_results:
                    notes_dir = self._notes_dir(project_id)
                    for mem in notes_results:
                        source = mem.get("source", "")
                        # Only include results from the notes directory
                        if notes_dir and source.startswith(notes_dir):
                            seen_sources.add(source)
                            heading = mem.get("heading", "")
                            content = mem.get("content", "")
                            score = mem.get("score", 0)
                            entry = f"**{os.path.basename(source)}**"
                            if heading:
                                entry += f" — {heading}"
                            entry += f" (relevance: {score:.2f})\n{content}"
                            notes_lines.append(entry)
                if notes_lines:
                    ctx.notes = "\n\n".join(notes_lines)
            except Exception as e:
                logger.warning(f"Notes search failed for project {project_id}: {e}")

        # Tier 3: Recent Task Memories
        recent_count = self.config.context_include_recent
        if recent_count > 0:
            try:
                tasks_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
                if os.path.isdir(tasks_dir):
                    task_files = sorted(
                        glob.glob(os.path.join(tasks_dir, "*.md")),
                        key=os.path.getmtime,
                        reverse=True,
                    )[:recent_count]

                    recent_lines = []
                    for tf in task_files:
                        seen_sources.add(tf)
                        try:
                            with open(tf) as f:
                                content = f.read().strip()
                            # Only include the first ~500 chars for recent tasks
                            if len(content) > 500:
                                content = content[:500] + "..."
                            recent_lines.append(content)
                        except Exception:
                            pass
                    if recent_lines:
                        ctx.recent_tasks = "\n\n---\n\n".join(recent_lines)
            except Exception as e:
                logger.warning(f"Recent tasks read failed for project {project_id}: {e}")

        # Tier 4: Semantic Search Results (de-duplicated)
        try:
            k = self.config.recall_top_k
            results = await instance.search(query, top_k=k + len(seen_sources))
            if results:
                search_lines = []
                for mem in results:
                    source = mem.get("source", "")
                    # Skip profile and already-seen sources
                    if source in seen_sources:
                        continue
                    if source == self._profile_path(project_id):
                        continue
                    seen_sources.add(source)

                    heading = mem.get("heading", "")
                    content = mem.get("content", "")
                    score = mem.get("score", 0)
                    entry = f"*Source: {source}*"
                    if heading:
                        entry += f"\n*Section: {heading}*"
                    entry += f" (relevance: {score:.2f})\n{content}"
                    search_lines.append(entry)

                    if len(search_lines) >= k:
                        break

                if search_lines:
                    ctx.search_results = "\n\n".join(search_lines)
        except Exception as e:
            logger.warning(f"Semantic search failed for project {project_id}: {e}")

        return ctx

    # ------------------------------------------------------------------
    # Original Public API (preserved for compatibility)
    # ------------------------------------------------------------------

    async def recall(self, task: Any, workspace_path: str, top_k: int | None = None) -> list[dict]:
        """Search for memories relevant to *task*.

        Uses ``task.title + task.description`` as the hybrid search query so
        both semantic similarity and keyword overlap contribute to ranking.

        Returns an empty list on any error — memory recall failures must never
        block task execution.
        """
        if not self.config.auto_recall:
            return []

        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return []

        k = top_k or self.config.recall_top_k
        query = f"{task.title} {task.description}"
        try:
            results = await instance.search(query, top_k=k)
            return results if results else []
        except Exception as e:
            logger.warning(f"Memory recall failed for task {task.id}: {e}")
            return []

    async def remember(self, task: Any, output: Any, workspace_path: str) -> str | None:
        """Save a task result as a structured markdown memory file.

        Writes the file to ``{data_dir}/memory/{project_id}/tasks/{task_id}.md``
        and indexes it via ``memsearch.index_file()``. Returns the file path
        on success, ``None`` otherwise.
        """
        if not self.config.auto_remember:
            return None

        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return None

        memory_path = os.path.join(
            self._project_memory_dir(task.project_id), "tasks", f"{task.id}.md"
        )
        try:
            content = self._format_task_memory(task, output)
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)
            with open(memory_path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write memory file for task {task.id}: {e}")
            return None

        # Index the new file (non-fatal)
        try:
            await instance.index_file(memory_path)
        except Exception as e:
            logger.warning(f"Memory indexing failed for task {task.id}: {e}")

        return memory_path

    async def search(
        self, project_id: str, workspace_path: str, query: str, top_k: int = 10
    ) -> list[dict]:
        """Ad-hoc semantic search across a project's memory.

        Exposed for command-handler and hook-engine usage.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return []

        try:
            results = await instance.search(query, top_k=top_k)
            return results if results else []
        except Exception as e:
            logger.warning(f"Memory search failed for project {project_id}: {e}")
            return []

    async def reindex(self, project_id: str, workspace_path: str) -> int:
        """Force a full reindex of a project's memory.

        Returns the number of chunks indexed, or 0 on failure.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return 0

        try:
            result = await instance.index(force=True)
            return result if isinstance(result, int) else 0
        except Exception as e:
            logger.warning(f"Memory reindex failed for project {project_id}: {e}")
            return 0

    async def stats(self, project_id: str, workspace_path: str) -> dict:
        """Get memory statistics for a project.

        Returns a dict with at least ``enabled`` and ``available`` keys.
        """
        if not self.config.enabled:
            return {"enabled": False, "available": MEMSEARCH_AVAILABLE}

        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return {
                "enabled": True,
                "available": MEMSEARCH_AVAILABLE,
                "error": "Failed to initialize MemSearch instance",
            }

        profile_exists = os.path.isfile(self._profile_path(project_id))
        last_compact = self._last_compact.get(project_id)

        return {
            "enabled": True,
            "available": True,
            "collection": self._collection_name(project_id),
            "milvus_uri": self.config.milvus_uri,
            "embedding_provider": self.config.embedding_provider,
            "auto_recall": self.config.auto_recall,
            "auto_remember": self.config.auto_remember,
            "recall_top_k": self.config.recall_top_k,
            "profile_enabled": self.config.profile_enabled,
            "profile_exists": profile_exists,
            "revision_enabled": self.config.revision_enabled,
            "auto_generate_notes": self.config.auto_generate_notes,
            "compact_enabled": self.config.compact_enabled,
            "last_compact": last_compact,
        }

    async def close(self) -> None:
        """Shutdown all MemSearch instances and file watchers."""
        for watcher in self._watchers.values():
            try:
                watcher.stop()
            except Exception as e:
                logger.debug(f"Error stopping memory watcher: {e}")
        for project_id, instance in self._instances.items():
            try:
                instance.close()
            except Exception as e:
                logger.debug(f"Error closing MemSearch for project {project_id}: {e}")
        self._instances.clear()
        self._watchers.clear()

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_task_memory(self, task: Any, output: Any) -> str:
        """Format a task result as a structured markdown memory file.

        The format follows the template from the design doc: a heading with
        the task ID and title, metadata block, summary, and files-changed
        section. This structure is optimised for memsearch's heading-based
        chunker.
        """
        date_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime())

        # Safely extract enum values
        status = output.result.value if hasattr(output.result, "value") else str(output.result)
        task_type = task.task_type.value if (task.task_type and hasattr(task.task_type, "value")) else "unknown"
        tokens = output.tokens_used if output.tokens_used else 0

        files_section = "No files changed."
        if output.files_changed:
            files_section = "\n".join(f"- {f}" for f in output.files_changed)

        summary = output.summary or "No summary available."

        return (
            f"# Task: {task.id} — {task.title}\n"
            f"\n"
            f"**Project:** {task.project_id} | **Type:** {task_type} | **Status:** {status}\n"
            f"**Date:** {date_str} | **Tokens:** {tokens:,}\n"
            f"\n"
            f"## Summary\n"
            f"{summary}\n"
            f"\n"
            f"## Files Changed\n"
            f"{files_section}\n"
        )
