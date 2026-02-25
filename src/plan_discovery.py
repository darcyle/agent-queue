"""Plan file discovery and validation for the task orchestration pipeline.

This module handles Layer 1 of the multi-layer task splitting fix:
finding plan files in agent workspaces, validating them before parsing,
and managing plan generation depth to prevent unbounded recursion.

The discovery pipeline:
  1. Scan workspace directory for candidate plan files
  2. Validate candidates (file size, structure, freshness)
  3. Classify as implementation plan vs design document
  4. Track plan generation depth for recursive splitting control
  5. Return validated plan files ready for parsing

This addresses Root Cause 1 (tasks stuck in DEFINED) and Root Cause 2
(is_plan_subtask hard guard) from the keen-beacon analysis by providing:
  - Smarter file discovery that finds plans even in non-standard locations
  - Depth-aware discovery that replaces the blanket is_plan_subtask block
  - Pre-parse validation that prevents garbage-in scenarios
"""

from __future__ import annotations

import glob
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryConfig:
    """Configuration for plan file discovery.

    Parameters
    ----------
    max_plan_depth : int
        Maximum depth of recursive plan generation. A task at depth 0
        (root task) can generate subtasks. Those subtasks are at depth 1.
        If max_plan_depth=2, subtasks at depth 1 can also generate their
        own subtasks, but depth-2 subtasks cannot.
        Default: 2 (allows one level of recursive splitting).
    max_file_size_bytes : int
        Maximum plan file size in bytes. Files larger than this are likely
        full design documents, not focused implementation plans.
        Default: 100KB.
    min_file_size_bytes : int
        Minimum plan file size in bytes. Files smaller than this are likely
        empty or trivial.
        Default: 50 bytes.
    max_file_age_seconds : float
        Maximum age of plan files to consider. Stale files from previous
        runs should be ignored.
        Default: 3600 (1 hour).
    plan_file_patterns : tuple[str, ...]
        Glob patterns to search for plan files.
    plan_file_names : tuple[str, ...]
        Exact filenames to look for (case-insensitive).
    """

    max_plan_depth: int = 2
    max_file_size_bytes: int = 100 * 1024  # 100KB
    min_file_size_bytes: int = 50
    max_file_age_seconds: float = 3600.0  # 1 hour
    plan_file_patterns: tuple[str, ...] = (
        "*.md",
        "*.markdown",
        "*.txt",
    )
    plan_file_names: tuple[str, ...] = (
        "plan.md",
        "implementation-plan.md",
        "implementation_plan.md",
        "plan.txt",
        "task-plan.md",
        "task_plan.md",
        "steps.md",
        "subtasks.md",
        "execution-plan.md",
        "execution_plan.md",
    )
    # Additional subdirectory glob patterns to search for plan files.
    # These are checked in addition to the standard one-level-deep
    # subdirectory scan, catching plans in nested or non-standard locations.
    extra_search_globs: tuple[str, ...] = (
        "notes/*.md",
        "notes/plans/*.md",
        "docs/plans/*.md",
        "plans/*.md",
    )
    # Maximum age (seconds) for the deep scan fallback. Only recently-modified
    # files are considered when doing a recursive workspace search.
    deep_scan_max_age_seconds: float = 1800.0  # 30 minutes


# Default configuration
DEFAULT_CONFIG = DiscoveryConfig()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PlanFileCandidate:
    """A candidate plan file found during discovery."""

    path: Path
    size_bytes: int
    modified_time: float
    age_seconds: float
    heading_count: int = 0
    has_implementation_section: bool = False
    has_actionable_structure: bool = False
    confidence_score: float = 0.0
    rejection_reason: str | None = None

    @property
    def is_valid(self) -> bool:
        """Whether this candidate passed validation."""
        return self.rejection_reason is None

    @property
    def filename(self) -> str:
        return self.path.name


@dataclass
class DiscoveryResult:
    """Result of plan file discovery in a workspace."""

    workspace_path: Path
    candidates_found: list[PlanFileCandidate]
    best_plan: PlanFileCandidate | None
    rejected_candidates: list[PlanFileCandidate]
    current_depth: int
    max_depth: int
    depth_exceeded: bool = False

    @property
    def has_valid_plan(self) -> bool:
        """Whether a valid plan file was found."""
        return self.best_plan is not None and not self.depth_exceeded


# ---------------------------------------------------------------------------
# Plan depth tracking
# ---------------------------------------------------------------------------

def get_plan_depth(
    task_id: str,
    parent_task_ids: list[str],
    plan_subtask_flags: dict[str, bool],
) -> int:
    """Calculate the plan generation depth for a task.

    Walks the parent chain counting how many ancestors were generated
    from plans (is_plan_subtask=True). This replaces the blanket
    is_plan_subtask guard with depth-aware logic.

    Parameters
    ----------
    task_id : str
        The current task's ID.
    parent_task_ids : list[str]
        Ordered list of ancestor task IDs from immediate parent to root.
    plan_subtask_flags : dict[str, bool]
        Mapping of task_id -> is_plan_subtask for all ancestors.

    Returns
    -------
    int
        The plan depth (0 = root task, 1 = subtask of a plan, etc.)

    Examples
    --------
    >>> get_plan_depth("task-c", ["task-b", "task-a"], {"task-a": False, "task-b": True})
    1
    >>> get_plan_depth("task-d", ["task-c", "task-b", "task-a"],
    ...               {"task-a": False, "task-b": True, "task-c": True})
    2
    """
    depth = 0
    for ancestor_id in parent_task_ids:
        if plan_subtask_flags.get(ancestor_id, False):
            depth += 1
    # If the current task itself is a plan subtask, count it
    if plan_subtask_flags.get(task_id, False):
        depth += 1
    return depth


def can_generate_subtasks(
    task_id: str,
    parent_task_ids: list[str],
    plan_subtask_flags: dict[str, bool],
    max_plan_depth: int = 2,
) -> tuple[bool, int, str]:
    """Determine whether a task is allowed to generate subtasks from a plan.

    Replaces the blanket `if task.is_plan_subtask: return []` guard with
    depth-aware logic. Tasks can recursively split up to max_plan_depth levels.

    Parameters
    ----------
    task_id : str
        The current task's ID.
    parent_task_ids : list[str]
        Ordered list of ancestor task IDs from immediate parent to root.
    plan_subtask_flags : dict[str, bool]
        Mapping of task_id -> is_plan_subtask for all ancestors.
    max_plan_depth : int
        Maximum allowed plan generation depth.

    Returns
    -------
    tuple[bool, int, str]
        (allowed, current_depth, reason)
    """
    current_depth = get_plan_depth(task_id, parent_task_ids, plan_subtask_flags)

    if current_depth >= max_plan_depth:
        return (
            False,
            current_depth,
            f"plan depth {current_depth} >= max_plan_depth {max_plan_depth}",
        )

    return (
        True,
        current_depth,
        f"plan depth {current_depth} < max_plan_depth {max_plan_depth}",
    )


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_IMPL_CONTAINER_RE = re.compile(
    r"^#{1,3}\s+(?:\d+[\.\):\s]+)?(?:implementation|execution|delivery)\s+"
    r"(?:plan|steps|phases?|order)",
    re.IGNORECASE | re.MULTILINE,
)
_ACTIONABLE_HEADING_RE = re.compile(
    r"^#{1,3}\s+(?:(?:phase|step|stage|milestone)\s+\d+|"
    r"(?:\d+[\.\):\s]+)?(?:implement|create|add|update|fix|refactor|migrate|build|"
    r"setup|set\s+up|configure|deploy|test|write|remove|delete|extract))",
    re.IGNORECASE | re.MULTILINE,
)


def _quick_structure_check(content: str) -> tuple[int, bool, bool]:
    """Quick structural check of file content without full parsing.

    Returns (heading_count, has_implementation_section, has_actionable_structure).
    """
    headings = _HEADING_RE.findall(content)
    heading_count = len(headings)
    has_impl = bool(_IMPL_CONTAINER_RE.search(content))
    has_actionable = bool(_ACTIONABLE_HEADING_RE.search(content))
    return heading_count, has_impl, has_actionable


def _score_candidate(candidate: PlanFileCandidate) -> float:
    """Score a plan file candidate for selection.

    Higher scores indicate better plan file candidates.
    """
    score = 0.0

    # Prefer files with implementation sections
    if candidate.has_implementation_section:
        score += 3.0

    # Prefer files with actionable structure
    if candidate.has_actionable_structure:
        score += 2.0

    # Prefer files with a moderate number of headings (3-15 is ideal)
    if 3 <= candidate.heading_count <= 15:
        score += 1.5
    elif candidate.heading_count > 15:
        # Too many headings suggests a design doc
        score -= 0.5
    elif candidate.heading_count < 2:
        # Too few headings — not really a plan
        score -= 1.0

    # Prefer known plan file names
    name_lower = candidate.filename.lower()
    if name_lower in {n.lower() for n in DEFAULT_CONFIG.plan_file_names}:
        score += 2.5

    # Prefer newer files
    if candidate.age_seconds < 300:  # < 5 minutes
        score += 1.0
    elif candidate.age_seconds < 1800:  # < 30 minutes
        score += 0.5

    # Slight preference for moderate file size (1KB - 20KB)
    if 1024 <= candidate.size_bytes <= 20 * 1024:
        score += 0.5

    return score


# Regex for plan-like headings used by the deep scan fallback.
# Matches headings like "## Phase 1", "### Step 2:", "# Part 3 - ...", etc.
_PLAN_INDICATOR_RE = re.compile(
    r"^#{1,3}\s+(?:Phase|Step|Part)\s+\d",
    re.MULTILINE | re.IGNORECASE,
)


def _deep_scan_for_plan(
    workspace: str | Path,
    now: float,
    config: DiscoveryConfig,
) -> PlanFileCandidate | None:
    """Fallback: scan workspace recursively for recently-modified .md files with plan indicators.

    This catches plans written to unexpected locations (e.g. ``notes/sprint3.md``)
    that none of the configured patterns or exact filenames would match.

    Only files modified within ``config.deep_scan_max_age_seconds`` are considered,
    and only if they contain plan-like structure (Phase/Step/Part headings).

    Parameters
    ----------
    workspace : str or Path
        The workspace root directory.
    now : float
        Current time (``time.time()``), passed in for consistency with the caller.
    config : DiscoveryConfig
        Discovery configuration (provides age cutoff and size limits).

    Returns
    -------
    PlanFileCandidate or None
        The best candidate found by deep scan, or None if nothing qualifies.
    """
    workspace_str = str(workspace)
    cutoff = now - config.deep_scan_max_age_seconds

    candidates: list[tuple[float, Path]] = []

    for md_path_str in glob.glob(os.path.join(workspace_str, "**/*.md"), recursive=True):
        md_path = Path(md_path_str)
        if not md_path.is_file():
            continue

        # Skip archived plans (e.g. .claude/plans/) and hidden directories
        parts = md_path.relative_to(workspace).parts
        if any(part.startswith(".") for part in parts):
            continue
        # Skip common non-plan directories
        if any(part in {"node_modules", "__pycache__", "venv", ".venv",
                        "dist", "build", "target"} for part in parts):
            continue

        try:
            stat = md_path.stat()
        except OSError:
            continue

        # Skip files outside the age window
        if stat.st_mtime < cutoff:
            continue

        # Skip files outside size bounds
        if stat.st_size < config.min_file_size_bytes:
            continue
        if stat.st_size > config.max_file_size_bytes:
            continue

        # Quick content check — only read the first 4KB for plan-like structure
        try:
            with open(md_path, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(4096)
        except OSError:
            continue

        if _PLAN_INDICATOR_RE.search(head):
            candidates.append((stat.st_mtime, md_path))

    if not candidates:
        return None

    # Pick the most recently modified candidate
    candidates.sort(reverse=True)  # Newest first
    best_mtime, best_path = candidates[0]

    logger.info(
        "Deep scan found plan file: %s (modified %.0fs ago, %d total candidates)",
        best_path,
        now - best_mtime,
        len(candidates),
    )

    # Evaluate through the standard pipeline so it gets the same validation
    return _evaluate_file(best_path, now, config)


def discover_plan_files(
    workspace_path: str | Path,
    config: DiscoveryConfig | None = None,
) -> list[PlanFileCandidate]:
    """Discover candidate plan files in a workspace directory.

    Scans the workspace for markdown files that could be plan documents,
    validates them, and returns scored candidates.

    Parameters
    ----------
    workspace_path : str or Path
        The workspace directory to scan.
    config : DiscoveryConfig or None
        Discovery configuration. Uses defaults if None.

    Returns
    -------
    list[PlanFileCandidate]
        List of candidates sorted by confidence score (highest first).
    """
    config = config or DEFAULT_CONFIG
    workspace = Path(workspace_path)

    if not workspace.is_dir():
        return []

    candidates: list[PlanFileCandidate] = []
    now = time.time()

    # Phase 1: Check exact plan file names first (highest priority)
    checked_paths: set[Path] = set()

    for plan_name in config.plan_file_names:
        plan_path = workspace / plan_name
        if plan_path.is_file():
            candidate = _evaluate_file(plan_path, now, config)
            if candidate is not None:
                candidates.append(candidate)
            checked_paths.add(plan_path.resolve())

    # Phase 2: Scan for other markdown files in the workspace root
    for pattern in config.plan_file_patterns:
        for filepath in workspace.glob(pattern):
            if not filepath.is_file():
                continue
            resolved = filepath.resolve()
            if resolved in checked_paths:
                continue
            checked_paths.add(resolved)

            candidate = _evaluate_file(filepath, now, config)
            if candidate is not None:
                candidates.append(candidate)

    # Phase 2.5: Check extra search globs (notes/*.md, notes/plans/*.md, etc.)
    # These catch plans in non-standard subdirectory locations that the
    # standard one-level-deep scan might miss if the filename isn't in
    # plan_file_names.
    for extra_glob in config.extra_search_globs:
        for filepath in workspace.glob(extra_glob):
            if not filepath.is_file():
                continue
            resolved = filepath.resolve()
            if resolved in checked_paths:
                continue
            checked_paths.add(resolved)

            candidate = _evaluate_file(filepath, now, config)
            if candidate is not None:
                candidates.append(candidate)

    # Phase 3: Check one level of subdirectories for plan files
    for subdir in workspace.iterdir():
        if not subdir.is_dir():
            continue
        # Skip hidden directories and common non-plan directories
        if subdir.name.startswith(".") or subdir.name in {
            "node_modules", "__pycache__", ".git", "venv", ".venv",
            "dist", "build", "target",
        }:
            continue

        for plan_name in config.plan_file_names:
            plan_path = subdir / plan_name
            if plan_path.is_file():
                resolved = plan_path.resolve()
                if resolved not in checked_paths:
                    candidate = _evaluate_file(plan_path, now, config)
                    if candidate is not None:
                        candidates.append(candidate)
                    checked_paths.add(resolved)

    # Score and sort candidates
    for candidate in candidates:
        candidate.confidence_score = _score_candidate(candidate)

    candidates.sort(key=lambda c: c.confidence_score, reverse=True)

    # Phase 4: Deep scan fallback — if no valid candidates were found by
    # the standard discovery phases, do a recursive workspace search for
    # recently-modified markdown files that contain plan-like structure.
    valid_candidates = [c for c in candidates if c.is_valid]
    if not valid_candidates:
        logger.debug(
            "No valid plan files found via standard discovery in %s; "
            "attempting deep scan fallback",
            workspace,
        )
        deep_candidate = _deep_scan_for_plan(workspace, now, config)
        if deep_candidate is not None:
            # Check it wasn't already found (and rejected) by standard discovery
            resolved = deep_candidate.path.resolve()
            already_present = any(
                c.path.resolve() == resolved for c in candidates
            )
            if not already_present:
                deep_candidate.confidence_score = _score_candidate(deep_candidate)
                candidates.append(deep_candidate)
                candidates.sort(key=lambda c: c.confidence_score, reverse=True)
                logger.info(
                    "Deep scan fallback added candidate: %s (score=%.1f, valid=%s)",
                    deep_candidate.path,
                    deep_candidate.confidence_score,
                    deep_candidate.is_valid,
                )
            else:
                logger.debug(
                    "Deep scan candidate %s was already found (possibly rejected) "
                    "by standard discovery",
                    deep_candidate.path,
                )
    else:
        # Log what was found via standard discovery for operator visibility
        best = valid_candidates[0]
        logger.info(
            "Plan file discovered: %s (score=%.1f, headings=%d, "
            "has_impl_section=%s, age=%.0fs)",
            best.path,
            best.confidence_score,
            best.heading_count,
            best.has_implementation_section,
            best.age_seconds,
        )

    return candidates


def _evaluate_file(
    filepath: Path,
    now: float,
    config: DiscoveryConfig,
) -> PlanFileCandidate | None:
    """Evaluate a single file as a plan candidate.

    Returns a PlanFileCandidate (possibly with rejection_reason set),
    or None if the file cannot be read.
    """
    try:
        stat = filepath.stat()
    except OSError:
        return None

    size = stat.st_size
    modified = stat.st_mtime
    age = now - modified

    candidate = PlanFileCandidate(
        path=filepath,
        size_bytes=size,
        modified_time=modified,
        age_seconds=age,
    )

    # Size validation
    if size < config.min_file_size_bytes:
        candidate.rejection_reason = (
            f"file too small ({size} bytes < {config.min_file_size_bytes})"
        )
        return candidate

    if size > config.max_file_size_bytes:
        candidate.rejection_reason = (
            f"file too large ({size} bytes > {config.max_file_size_bytes})"
        )
        return candidate

    # Age validation
    if age > config.max_file_age_seconds:
        candidate.rejection_reason = (
            f"file too old ({age:.0f}s > {config.max_file_age_seconds:.0f}s)"
        )
        return candidate

    # Content validation
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        candidate.rejection_reason = f"cannot read file: {e}"
        return candidate

    heading_count, has_impl, has_actionable = _quick_structure_check(content)
    candidate.heading_count = heading_count
    candidate.has_implementation_section = has_impl
    candidate.has_actionable_structure = has_actionable

    # Reject files with no headings at all
    if heading_count == 0:
        candidate.rejection_reason = "no markdown headings found"
        return candidate

    return candidate


def discover_and_select(
    workspace_path: str | Path,
    task_id: str = "",
    parent_task_ids: list[str] | None = None,
    plan_subtask_flags: dict[str, bool] | None = None,
    config: DiscoveryConfig | None = None,
) -> DiscoveryResult:
    """Discover, validate, and select the best plan file in a workspace.

    This is the main entry point for plan file discovery. It combines
    file discovery with depth checking to provide a complete decision
    about whether and which plan file should be parsed.

    Parameters
    ----------
    workspace_path : str or Path
        The workspace directory to scan.
    task_id : str
        The current task's ID (for depth tracking).
    parent_task_ids : list[str] or None
        Ancestor task IDs for depth calculation.
    plan_subtask_flags : dict[str, bool] or None
        Mapping of task_id -> is_plan_subtask for ancestors.
    config : DiscoveryConfig or None
        Discovery configuration.

    Returns
    -------
    DiscoveryResult
        Complete discovery result with best plan selection and depth info.
    """
    config = config or DEFAULT_CONFIG
    parent_task_ids = parent_task_ids or []
    plan_subtask_flags = plan_subtask_flags or {}

    workspace = Path(workspace_path)

    # Check plan depth first
    allowed, current_depth, depth_reason = can_generate_subtasks(
        task_id=task_id,
        parent_task_ids=parent_task_ids,
        plan_subtask_flags=plan_subtask_flags,
        max_plan_depth=config.max_plan_depth,
    )

    # Discover candidates regardless of depth (for reporting)
    candidates = discover_plan_files(workspace, config)

    valid_candidates = [c for c in candidates if c.is_valid]
    rejected_candidates = [c for c in candidates if not c.is_valid]

    best_plan = valid_candidates[0] if valid_candidates else None

    return DiscoveryResult(
        workspace_path=workspace,
        candidates_found=candidates,
        best_plan=best_plan,
        rejected_candidates=rejected_candidates,
        current_depth=current_depth,
        max_depth=config.max_plan_depth,
        depth_exceeded=not allowed,
    )


# ---------------------------------------------------------------------------
# Plan file cleanup
# ---------------------------------------------------------------------------

def cleanup_plan_file(filepath: str | Path) -> bool:
    """Remove a plan file after it has been successfully parsed.

    Returns True if the file was removed, False otherwise.
    """
    try:
        Path(filepath).unlink(missing_ok=True)
        return True
    except OSError:
        return False
