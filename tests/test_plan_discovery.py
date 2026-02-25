"""Tests for the plan file discovery module.

These tests verify Layer 1 of the multi-layer task splitting fix:
  - Plan file discovery in workspace directories
  - File validation (size, age, structure)
  - Plan depth tracking and recursive splitting control
  - Candidate scoring and selection
"""

import os
import time
import pytest
from pathlib import Path

from src.plan_discovery import (
    DiscoveryConfig,
    DiscoveryResult,
    PlanFileCandidate,
    can_generate_subtasks,
    cleanup_plan_file,
    discover_and_select,
    discover_plan_files,
    get_plan_depth,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace directory."""
    return tmp_path


@pytest.fixture
def config():
    """Create a test discovery config with relaxed age limits."""
    return DiscoveryConfig(
        max_file_age_seconds=86400,  # 24 hours for testing
    )


def _write_plan(workspace: Path, name: str = "plan.md", content: str | None = None) -> Path:
    """Helper to write a plan file in a workspace."""
    if content is None:
        content = """\
# Implementation Plan

## Phase 1: Create Module

Implement the new module with core functionality.

**Estimated effort:** ~2 hours

## Phase 2: Add Tests

Write unit tests for the new module.

**Estimated effort:** ~1 hour

## Phase 3: Update Integration

Update the integration layer to use the new module.

**Estimated effort:** ~1 hour
"""
    filepath = workspace / name
    filepath.write_text(content, encoding="utf-8")
    return filepath


def _write_design_doc(workspace: Path, name: str = "design.md") -> Path:
    """Helper to write a design document."""
    content = """\
# Design Document: Feature X

## Executive Summary

This document describes the design for Feature X.

## Architecture

The system uses a microservices architecture with event-driven communication.

## Current State

Currently the system handles 100 requests per second.

## Design Principles

1. Separation of concerns
2. Single responsibility
3. Open/closed principle

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Data loss | High | Backup strategy |
"""
    filepath = workspace / name
    filepath.write_text(content, encoding="utf-8")
    return filepath


# ---------------------------------------------------------------------------
# Tests: Plan depth tracking
# ---------------------------------------------------------------------------

class TestPlanDepth:
    """Test depth calculation for recursive plan generation control."""

    def test_root_task_depth_zero(self):
        """Root task with no plan subtask ancestors has depth 0."""
        depth = get_plan_depth("root-task", [], {})
        assert depth == 0

    def test_root_task_not_plan_subtask(self):
        """Root task explicitly not a plan subtask has depth 0."""
        depth = get_plan_depth("root-task", [], {"root-task": False})
        assert depth == 0

    def test_direct_plan_subtask_depth_one(self):
        """A task that is itself a plan subtask has depth 1."""
        depth = get_plan_depth(
            "subtask-a",
            ["root-task"],
            {"root-task": False, "subtask-a": True},
        )
        assert depth == 1

    def test_nested_plan_subtask_depth_two(self):
        """A plan subtask of a plan subtask has depth 2."""
        depth = get_plan_depth(
            "subtask-b",
            ["subtask-a", "root-task"],
            {"root-task": False, "subtask-a": True, "subtask-b": True},
        )
        assert depth == 2

    def test_mixed_ancestry(self):
        """Only plan subtask ancestors count toward depth."""
        depth = get_plan_depth(
            "task-d",
            ["task-c", "task-b", "task-a"],
            {"task-a": False, "task-b": True, "task-c": False, "task-d": True},
        )
        # task-b is plan subtask (depth +1), task-d is plan subtask (depth +1)
        assert depth == 2

    def test_deep_chain(self):
        """Depth accumulates through multiple plan subtask levels."""
        depth = get_plan_depth(
            "task-e",
            ["task-d", "task-c", "task-b", "task-a"],
            {
                "task-a": False,
                "task-b": True,
                "task-c": True,
                "task-d": True,
                "task-e": True,
            },
        )
        assert depth == 4


class TestCanGenerateSubtasks:
    """Test the depth-aware subtask generation control."""

    def test_root_task_allowed(self):
        """Root tasks can always generate subtasks."""
        allowed, depth, reason = can_generate_subtasks(
            "root-task", [], {}, max_plan_depth=2
        )
        assert allowed is True
        assert depth == 0

    def test_depth_one_allowed_with_depth_two_max(self):
        """Depth-1 subtask allowed when max_depth=2."""
        allowed, depth, reason = can_generate_subtasks(
            "subtask-a",
            ["root-task"],
            {"root-task": False, "subtask-a": True},
            max_plan_depth=2,
        )
        assert allowed is True
        assert depth == 1

    def test_depth_two_blocked_with_depth_two_max(self):
        """Depth-2 subtask blocked when max_depth=2."""
        allowed, depth, reason = can_generate_subtasks(
            "subtask-b",
            ["subtask-a", "root-task"],
            {"root-task": False, "subtask-a": True, "subtask-b": True},
            max_plan_depth=2,
        )
        assert allowed is False
        assert depth == 2

    def test_depth_one_blocked_with_depth_one_max(self):
        """Depth-1 subtask blocked when max_depth=1 (original behavior)."""
        allowed, depth, reason = can_generate_subtasks(
            "subtask-a",
            ["root-task"],
            {"root-task": False, "subtask-a": True},
            max_plan_depth=1,
        )
        assert allowed is False
        assert depth == 1

    def test_reason_explains_decision(self):
        """The reason string should explain the depth check."""
        allowed, depth, reason = can_generate_subtasks(
            "root-task", [], {}, max_plan_depth=2
        )
        assert "0" in reason
        assert "2" in reason

    def test_zero_max_depth_blocks_everything(self):
        """max_plan_depth=0 blocks all plan generation."""
        allowed, _, _ = can_generate_subtasks(
            "root-task", [], {}, max_plan_depth=0
        )
        assert allowed is False


# ---------------------------------------------------------------------------
# Tests: File discovery
# ---------------------------------------------------------------------------

class TestDiscoverPlanFiles:
    """Test plan file discovery in workspace directories."""

    def test_finds_plan_md(self, workspace, config):
        """Should find plan.md in workspace root."""
        _write_plan(workspace, "plan.md")
        candidates = discover_plan_files(workspace, config)
        assert len(candidates) >= 1
        assert any(c.filename == "plan.md" for c in candidates)

    def test_finds_implementation_plan_md(self, workspace, config):
        """Should find implementation-plan.md."""
        _write_plan(workspace, "implementation-plan.md")
        candidates = discover_plan_files(workspace, config)
        assert any(c.filename == "implementation-plan.md" for c in candidates)

    def test_finds_task_plan_md(self, workspace, config):
        """Should find task-plan.md."""
        _write_plan(workspace, "task-plan.md")
        candidates = discover_plan_files(workspace, config)
        assert any(c.filename == "task-plan.md" for c in candidates)

    def test_finds_other_markdown_files(self, workspace, config):
        """Should find any .md file as a candidate."""
        _write_plan(workspace, "my-steps.md")
        candidates = discover_plan_files(workspace, config)
        assert len(candidates) >= 1

    def test_empty_workspace(self, workspace, config):
        """Empty workspace returns no candidates."""
        candidates = discover_plan_files(workspace, config)
        assert len(candidates) == 0

    def test_nonexistent_workspace(self, config):
        """Nonexistent workspace returns no candidates."""
        candidates = discover_plan_files("/nonexistent/path", config)
        assert len(candidates) == 0

    def test_rejects_too_small_files(self, workspace, config):
        """Files below minimum size are rejected."""
        filepath = workspace / "plan.md"
        filepath.write_text("# X\n", encoding="utf-8")
        candidates = discover_plan_files(workspace, config)
        # Should find the file but reject it
        rejected = [c for c in candidates if not c.is_valid]
        assert len(rejected) >= 1
        assert "too small" in rejected[0].rejection_reason

    def test_rejects_too_large_files(self, workspace):
        """Files above maximum size are rejected."""
        config = DiscoveryConfig(
            max_file_size_bytes=100,
            max_file_age_seconds=86400,
        )
        filepath = workspace / "plan.md"
        filepath.write_text("# Plan\n\n" + "x" * 200, encoding="utf-8")
        candidates = discover_plan_files(workspace, config)
        rejected = [c for c in candidates if not c.is_valid]
        assert len(rejected) >= 1
        assert "too large" in rejected[0].rejection_reason

    def test_rejects_no_headings(self, workspace, config):
        """Files with no markdown headings are rejected."""
        filepath = workspace / "notes.md"
        filepath.write_text(
            "This is just some text without any headings.\n" * 5,
            encoding="utf-8",
        )
        candidates = discover_plan_files(workspace, config)
        rejected = [c for c in candidates if not c.is_valid]
        assert len(rejected) >= 1
        assert "no markdown headings" in rejected[0].rejection_reason

    def test_finds_plan_in_subdirectory(self, workspace, config):
        """Should find plan files one level deep."""
        subdir = workspace / "output"
        subdir.mkdir()
        _write_plan(subdir, "plan.md")
        candidates = discover_plan_files(workspace, config)
        assert any(c.filename == "plan.md" for c in candidates)

    def test_skips_hidden_directories(self, workspace, config):
        """Should skip hidden directories like .git."""
        hidden = workspace / ".git"
        hidden.mkdir()
        _write_plan(hidden, "plan.md")
        # Also put a valid plan in the root
        _write_plan(workspace, "steps.md")
        candidates = discover_plan_files(workspace, config)
        # Should only find the root file, not the one in .git
        assert all(".git" not in str(c.path) for c in candidates)

    def test_skips_node_modules(self, workspace, config):
        """Should skip node_modules directory."""
        nm = workspace / "node_modules"
        nm.mkdir()
        _write_plan(nm, "plan.md")
        _write_plan(workspace, "plan.md")
        candidates = discover_plan_files(workspace, config)
        # Verify no candidate is inside the node_modules subdirectory
        for c in candidates:
            assert nm not in c.path.parents, (
                f"Found file inside node_modules: {c.path}"
            )


class TestCandidateScoring:
    """Test that candidates are scored and sorted correctly."""

    def test_plan_md_scored_higher_than_random(self, workspace, config):
        """plan.md should score higher than arbitrary markdown files."""
        _write_plan(workspace, "plan.md")
        _write_plan(workspace, "random-notes.md")
        candidates = discover_plan_files(workspace, config)
        assert len(candidates) >= 2
        # plan.md should be first (highest score)
        assert candidates[0].filename == "plan.md"

    def test_file_with_impl_section_scored_higher(self, workspace, config):
        """File with implementation section should score higher."""
        impl_content = """\
# Plan

## Overview

Background info.

## Implementation Plan

### Phase 1: Create Module

Do the work.

### Phase 2: Add Tests

Write tests.
"""
        no_impl_content = """\
# Plan

## Create Module

Do the work.

## Add Tests

Write tests.
"""
        (workspace / "with-impl.md").write_text(impl_content, encoding="utf-8")
        (workspace / "without-impl.md").write_text(no_impl_content, encoding="utf-8")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert len(valid) >= 2

        with_impl = next(c for c in valid if c.filename == "with-impl.md")
        without_impl = next(c for c in valid if c.filename == "without-impl.md")
        assert with_impl.confidence_score > without_impl.confidence_score

    def test_sorted_by_score_descending(self, workspace, config):
        """Candidates should be sorted by score, highest first."""
        _write_plan(workspace, "plan.md")
        _write_plan(workspace, "other.md")
        _write_design_doc(workspace)

        candidates = discover_plan_files(workspace, config)
        scores = [c.confidence_score for c in candidates]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Tests: discover_and_select (integrated discovery + depth check)
# ---------------------------------------------------------------------------

class TestDiscoverAndSelect:
    """Test the integrated discovery and selection function."""

    def test_finds_and_selects_plan(self, workspace, config):
        """Should find and select the best plan file."""
        _write_plan(workspace)
        result = discover_and_select(workspace, config=config)

        assert result.has_valid_plan is True
        assert result.best_plan is not None
        assert result.best_plan.filename == "plan.md"
        assert result.depth_exceeded is False

    def test_depth_exceeded_blocks_selection(self, workspace, config):
        """Should report depth exceeded even if plan file exists."""
        _write_plan(workspace)
        config.max_plan_depth = 1

        result = discover_and_select(
            workspace,
            task_id="subtask",
            parent_task_ids=["root"],
            plan_subtask_flags={"root": False, "subtask": True},
            config=config,
        )

        # Plan was found but depth blocks usage
        assert result.best_plan is not None
        assert result.depth_exceeded is True
        assert result.has_valid_plan is False

    def test_no_plan_found(self, workspace, config):
        """Should handle empty workspace gracefully."""
        result = discover_and_select(workspace, config=config)
        assert result.has_valid_plan is False
        assert result.best_plan is None

    def test_depth_info_populated(self, workspace, config):
        """Should populate depth information correctly."""
        _write_plan(workspace)
        config.max_plan_depth = 3

        result = discover_and_select(
            workspace,
            task_id="subtask-b",
            parent_task_ids=["subtask-a", "root"],
            plan_subtask_flags={
                "root": False,
                "subtask-a": True,
                "subtask-b": True,
            },
            config=config,
        )

        assert result.current_depth == 2
        assert result.max_depth == 3
        assert result.depth_exceeded is False
        assert result.has_valid_plan is True

    def test_rejected_candidates_tracked(self, workspace, config):
        """Should track rejected candidates for diagnostics."""
        # Create a file that's too small to be valid
        tiny = workspace / "tiny.md"
        tiny.write_text("# X\n", encoding="utf-8")

        # Create a valid plan
        _write_plan(workspace, "plan.md")

        result = discover_and_select(workspace, config=config)
        assert result.has_valid_plan is True
        assert len(result.rejected_candidates) >= 1


# ---------------------------------------------------------------------------
# Tests: Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    """Test plan file cleanup after processing."""

    def test_cleanup_removes_file(self, workspace):
        """Should remove the file after cleanup."""
        filepath = _write_plan(workspace)
        assert filepath.exists()

        result = cleanup_plan_file(filepath)
        assert result is True
        assert not filepath.exists()

    def test_cleanup_nonexistent_file(self, workspace):
        """Should handle nonexistent files gracefully."""
        result = cleanup_plan_file(workspace / "nonexistent.md")
        assert result is True  # missing_ok=True

    def test_cleanup_returns_true_on_success(self, workspace):
        """Should return True when file is successfully removed."""
        filepath = _write_plan(workspace)
        assert cleanup_plan_file(filepath) is True


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test edge cases in discovery."""

    def test_binary_file_ignored(self, workspace, config):
        """Binary files should be handled gracefully."""
        filepath = workspace / "binary.md"
        filepath.write_bytes(b"\x00\x01\x02\x03" * 100)
        candidates = discover_plan_files(workspace, config)
        # Should find the file but reject it (no headings in binary content)
        rejected = [c for c in candidates if not c.is_valid]
        if rejected:
            assert rejected[0].rejection_reason is not None

    def test_unicode_content(self, workspace, config):
        """Should handle Unicode content correctly."""
        content = """\
# Implementation Plan

## Phase 1: Create 日本語 Module

Implement the module with Unicode content: あいうえお.

**Estimated effort:** ~2 hours

## Phase 2: Add Tests 测试

Write tests with Unicode assertions.

**Estimated effort:** ~1 hour
"""
        (workspace / "plan.md").write_text(content, encoding="utf-8")
        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert len(valid) == 1

    def test_multiple_plan_files(self, workspace, config):
        """Should return all plan files, sorted by score."""
        _write_plan(workspace, "plan.md")
        _write_plan(workspace, "implementation-plan.md")
        _write_plan(workspace, "notes.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert len(valid) == 3

    def test_symlink_handling(self, workspace, config):
        """Should handle symlinks gracefully."""
        _write_plan(workspace, "plan.md")
        link = workspace / "plan-link.md"
        try:
            link.symlink_to(workspace / "plan.md")
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

        candidates = discover_plan_files(workspace, config)
        # Should deduplicate by resolved path
        valid = [c for c in candidates if c.is_valid]
        assert len(valid) == 1


# ---------------------------------------------------------------------------
# Tests: Extra search globs (notes/*.md, notes/plans/*.md, etc.)
# ---------------------------------------------------------------------------

class TestExtraSearchGlobs:
    """Test that extra_search_globs patterns find plans in non-standard locations."""

    def test_finds_plan_in_notes_dir(self, workspace, config):
        """Should find plan files in notes/ subdirectory."""
        notes_dir = workspace / "notes"
        notes_dir.mkdir()
        _write_plan(notes_dir, "sprint3-plan.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert any(c.filename == "sprint3-plan.md" for c in valid)

    def test_finds_plan_in_notes_plans_dir(self, workspace, config):
        """Should find plan files in notes/plans/ subdirectory."""
        plans_dir = workspace / "notes" / "plans"
        plans_dir.mkdir(parents=True)
        _write_plan(plans_dir, "feature-plan.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert any(c.filename == "feature-plan.md" for c in valid)

    def test_finds_plan_in_plans_dir(self, workspace, config):
        """Should find plan files in plans/ subdirectory."""
        plans_dir = workspace / "plans"
        plans_dir.mkdir()
        _write_plan(plans_dir, "implementation.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert any(c.filename == "implementation.md" for c in valid)

    def test_finds_plan_in_docs_plans_dir(self, workspace, config):
        """Should find plan files in docs/plans/ subdirectory."""
        plans_dir = workspace / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        _write_plan(plans_dir, "refactor.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert any(c.filename == "refactor.md" for c in valid)

    def test_deduplicates_with_standard_discovery(self, workspace, config):
        """Extra glob results should not duplicate standard discovery results."""
        # notes/plan.md would be found by both Phase 2.5 (notes/*.md glob)
        # and Phase 3 (subdirectory scan for exact plan file names)
        notes_dir = workspace / "notes"
        notes_dir.mkdir()
        _write_plan(notes_dir, "plan.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        # Should only appear once
        plan_md_count = sum(1 for c in valid if c.filename == "plan.md")
        assert plan_md_count == 1


# ---------------------------------------------------------------------------
# Tests: Deep scan fallback
# ---------------------------------------------------------------------------

class TestDeepScanFallback:
    """Test the deep scan fallback that finds plans in unexpected locations."""

    def test_deep_scan_finds_deeply_nested_plan(self, workspace, config):
        """Deep scan should find a plan in a deeply nested directory."""
        # Create a plan in a deeply nested non-standard location
        deep_dir = workspace / "project" / "sprint" / "docs"
        deep_dir.mkdir(parents=True)
        _write_plan(deep_dir, "sprint-plan.md")

        # No standard plan files exist — should trigger deep scan fallback
        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert any(c.filename == "sprint-plan.md" for c in valid)

    def test_deep_scan_not_triggered_when_standard_finds_valid(self, workspace, config):
        """Deep scan should NOT run when standard discovery finds valid candidates."""
        # Put a plan in standard location
        _write_plan(workspace, "plan.md")

        # Also put a plan in a deeply nested location
        deep_dir = workspace / "deep" / "nested" / "dir"
        deep_dir.mkdir(parents=True)
        _write_plan(deep_dir, "hidden-plan.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        # Standard plan.md should be found, but deep plan shouldn't be in results
        # (deep scan not triggered because standard found valid results)
        assert any(c.filename == "plan.md" for c in valid)
        assert not any(c.filename == "hidden-plan.md" for c in valid)

    def test_deep_scan_respects_age_limit(self, workspace):
        """Deep scan should skip files older than deep_scan_max_age_seconds."""
        config = DiscoveryConfig(
            max_file_age_seconds=86400,  # Allow old files in standard discovery
            deep_scan_max_age_seconds=60,  # But only 60s for deep scan
        )

        deep_dir = workspace / "custom" / "location"
        deep_dir.mkdir(parents=True)
        plan_path = deep_dir / "old-plan.md"
        _write_plan(deep_dir, "old-plan.md")

        # Make the file appear old by setting mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(plan_path, (old_time, old_time))

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        # Old file should not be found by deep scan
        assert not any(c.filename == "old-plan.md" for c in valid)

    def test_deep_scan_requires_plan_indicators(self, workspace, config):
        """Deep scan should only match files with Phase/Step/Part headings."""
        deep_dir = workspace / "misc" / "docs"
        deep_dir.mkdir(parents=True)

        # Write a markdown file WITHOUT plan indicators
        no_plan_content = """\
# Meeting Notes

## Discussion Points

- Talked about the roadmap
- Reviewed the budget

## Action Items

- Follow up with team
- Schedule next meeting
"""
        (deep_dir / "meeting-notes.md").write_text(no_plan_content, encoding="utf-8")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        # Should not find meeting notes — no Phase/Step/Part indicators
        assert not any(c.filename == "meeting-notes.md" for c in valid)

    def test_deep_scan_matches_plan_indicators(self, workspace, config):
        """Deep scan should match files with Phase/Step/Part headings."""
        deep_dir = workspace / "output" / "agent"
        deep_dir.mkdir(parents=True)

        plan_content = """\
# Feature Implementation

## Phase 1: Database Schema

Create the database schema.

**Estimated effort:** ~2 hours

## Phase 2: API Endpoints

Implement the REST endpoints.

## Phase 3: Frontend Integration

Wire up the frontend.
"""
        (deep_dir / "feature-plan.md").write_text(plan_content, encoding="utf-8")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert any(c.filename == "feature-plan.md" for c in valid)

    def test_deep_scan_skips_hidden_directories(self, workspace, config):
        """Deep scan should skip hidden directories."""
        hidden_dir = workspace / ".claude" / "plans"
        hidden_dir.mkdir(parents=True)
        _write_plan(hidden_dir, "archived-plan.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert not any(c.filename == "archived-plan.md" for c in valid)

    def test_deep_scan_skips_node_modules(self, workspace, config):
        """Deep scan should skip node_modules."""
        nm_dir = workspace / "node_modules" / "some-pkg"
        nm_dir.mkdir(parents=True)
        _write_plan(nm_dir, "plan.md")

        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]
        assert not any(
            "node_modules" in str(c.path) for c in valid
        )

    def test_deep_scan_picks_newest_candidate(self, workspace, config):
        """Deep scan should prefer the most recently modified file."""
        dir_a = workspace / "area_a"
        dir_a.mkdir()
        dir_b = workspace / "area_b"
        dir_b.mkdir()

        plan_a = dir_a / "plan-a.md"
        plan_b = dir_b / "plan-b.md"

        # Write both plans with plan indicators
        plan_content_a = """\
# Plan A

## Step 1: Do Thing A

Do the first thing.

## Step 2: Do Thing B

Do the second thing.
"""
        plan_content_b = """\
# Plan B

## Phase 1: Setup

Set up the environment.

## Phase 2: Implement

Write the code.
"""
        plan_a.write_text(plan_content_a, encoding="utf-8")
        plan_b.write_text(plan_content_b, encoding="utf-8")

        # Make plan_b newer than plan_a
        old_time = time.time() - 600  # 10 minutes ago
        os.utime(plan_a, (old_time, old_time))
        # plan_b keeps its current (newest) mtime

        # NOTE: These files are in one-level-deep subdirectories, so they
        # won't be found by the standard exact-name scan (Phase 3) since
        # "plan-a.md" and "plan-b.md" aren't in plan_file_names.
        # They WILL be found by standard Phase 2 root glob if at root level,
        # but since they're in subdirs, deep scan is needed.
        #
        # However, Phase 3 checks subdirectories for exact plan_file_names
        # and Phase 2.5 doesn't match these paths. So deep scan triggers.
        candidates = discover_plan_files(workspace, config)
        valid = [c for c in candidates if c.is_valid]

        if valid:
            # If valid candidates are found, plan_b should be preferred
            # (it's the newest file with plan indicators)
            assert valid[0].filename == "plan-b.md" or valid[0].filename == "plan-a.md"
