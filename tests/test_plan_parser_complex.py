"""Tests for complex plan parsing — design documents, implementation sections, and quality scoring.

These tests verify the enhanced plan parser's ability to handle complex
design documents (like the keen-beacon Discord embeds plan) where
implementation phases are embedded within a larger reference document.
"""

import os
import time

import pytest

from src.plan_parser import (
    ParsedPlan,
    PlanStep,
    find_plan_file,
    parse_plan,
    _parse_implementation_section,
    _parse_heading_sections,
    _score_parse_quality,
    _is_likely_actionable,
    _deep_scan_for_plan,
    STEP_HEADING_PATTERN,
    NON_ACTIONABLE_HEADINGS,
)


# ── Fixture: keen-beacon style design document ─────────────────────────── #

DESIGN_DOCUMENT = """\
# Discord Embedded Responses Improvement Plan

## Executive Summary

This document proposes a comprehensive redesign of Discord message formatting.

### Current State

The project uses discord.py and has a mixed formatting approach.

## 1. Discord Embed Capabilities & Constraints

### What Embeds Support

Discord embeds are structured message components with title, description, fields, etc.

### Hard Limits (Discord API enforced)

Title: 256 characters, Description: 4096 characters, etc.

### Markdown Support Within Embeds

Supported: bold, italic, code blocks, hyperlinks, blockquotes.

## 2. Proposed Architecture: Centralized Embed Factory

### New Module: `src/discord/embeds.py`

Create a centralized embed factory that enforces consistent styling.

### Design Principles

1. Single source of truth
2. Automatic safety
3. Consistent branding

## 3. Converting Notifications to Embeds

### Strategy: Hybrid Approach (Recommended)

Keep existing string formatters and add parallel embed functions.

### Updated Notification Signature

Update the notify callback to accept either strings or embeds.

## 4. Standardizing Slash Command Responses

### Current Inconsistencies

Mixed embeds and plain text. No consistent error handling pattern.

### Proposed Standard Patterns

Use success_embed() for success, error_embed() for errors, always ephemeral.

## 5. Implementation Plan

### Phase 1: Foundation (Low Risk)

Create `src/discord/embeds.py` module.
Move `_STATUS_COLORS` and `_STATUS_EMOJIS` from `commands.py`.
Implement `make_embed()`, convenience builders, `truncate()`, `unix_timestamp()`.
Update `commands.py` to import from `embeds.py`.

**Estimated effort:** ~2 hours

### Phase 2: Notification Embeds (Medium Risk)

Add embed formatters to `notifications.py`.
Add `*_embed()` variants for all 8 notification types.
Update `bot.py` notify callback to accept optional embed kwarg.

**Estimated effort:** ~3-4 hours

### Phase 3: Slash Command Consistency (Medium Risk)

Standardize all ~50 slash commands.
Replace inline `discord.Embed()` calls with factory functions.
Convert plain-text error responses to `error_embed()` calls.

**Estimated effort:** ~4-5 hours

### Phase 4: Chat Agent Responses (Low Risk)

Enhance chat agent formatting.
Tool execution results embedded in chat could use embeds.
Evaluate after Phases 1-3.

**Estimated effort:** ~1-2 hours

### Phase 5: Polish & Testing

Add unit tests for `embeds.py`.
Visual QA in a test Discord server.
Verify all embeds stay under 6,000-char total limit.
Test mobile rendering.

**Estimated effort:** ~2-3 hours

## 6. Visual Design Specification

### Color Palette

Success: Green (#2ecc71), Error: Red (#e74c3c), Warning: Amber (#f39c12).

### Embed Structure Template

Standard template with title, inline fields, full-width fields, footer.

### Inline Field Layout Rules

3-column for metadata, 2-column for pairs, full-width for descriptions.

## 7. Libraries and Dependencies

### No Additional Dependencies Required

discord.py >= 2.3.0 has everything needed.

### Optional Enhancements (Future)

discord-ext-pages for paginated embeds.

## 8. Risk Assessment & Mitigations

6000-char limit, mobile rendering, breaking tests, callback signature changes.

### Total Character Guard

Add _check_embed_size() guard in factory; fall back to text if exceeded.

## 9. Summary of Recommendations

Create embeds.py, add embed formatters, update bot.py callback, standardize responses.

### Expected Outcome

Consistent visual language, easier scanning, better error visibility.
"""


# ── _parse_implementation_section ─────────────────────────────────────── #

class TestParseImplementationSection:
    def test_extracts_phases_from_implementation_section(self):
        """The key test: should extract phases from the design doc.

        The 5 raw phases exceed _IMPL_PHASE_THRESHOLD (4) so they get
        consolidated into 3 coarser phases.
        """
        steps = _parse_implementation_section(DESIGN_DOCUMENT)
        assert len(steps) == 3
        # Consolidated phases should reference the original phase titles
        all_text = " ".join(s.title + " " + s.description for s in steps)
        assert "Foundation" in all_text
        assert "Notification Embeds" in all_text
        assert "Slash Command Consistency" in all_text
        assert "Chat Agent Responses" in all_text
        assert "Polish & Testing" in all_text

    def test_does_not_include_non_implementation_sections(self):
        """Reference sections should NOT appear as steps."""
        steps = _parse_implementation_section(DESIGN_DOCUMENT)
        titles = [s.title.lower() for s in steps]
        assert not any("color palette" in t for t in titles)
        assert not any("what embeds support" in t for t in titles)
        assert not any("hard limits" in t for t in titles)
        assert not any("current state" in t for t in titles)

    def test_descriptions_have_content(self):
        """Each extracted phase should have meaningful description."""
        steps = _parse_implementation_section(DESIGN_DOCUMENT)
        for step in steps:
            assert len(step.description) > 20, f"Step '{step.title}' has too little content"

    def test_consolidated_phases_include_step_outline(self):
        """Consolidated phases should include a 'Steps in this phase:' outline."""
        steps = _parse_implementation_section(DESIGN_DOCUMENT)
        # At least some of the consolidated phases should have the outline
        has_outline = any("Steps in this phase:" in s.description for s in steps)
        assert has_outline, "Consolidated phases should include a step outline"

    def test_priority_hints_sequential(self):
        steps = _parse_implementation_section(DESIGN_DOCUMENT)
        assert [s.priority_hint for s in steps] == [0, 1, 2]

    def test_returns_empty_for_simple_plan(self):
        """Plans without a dedicated implementation section should return empty."""
        simple = """# Simple Plan

## Add authentication

Implement JWT auth with login/logout.

## Write tests

Add pytest coverage for auth endpoints.
"""
        steps = _parse_implementation_section(simple)
        assert steps == []

    def test_different_implementation_keywords(self):
        """Should recognize various implementation section titles."""
        for keyword in ["Implementation Steps", "Action Items", "Development Phases",
                        "Execution Plan", "Work Plan", "Implementation"]:
            content = f"""# Plan

## Background

Some background context for the project and its requirements.

## {keyword}

### Step 1: Do first thing

Implementation details for the first step with enough content to be actionable.

### Step 2: Do second thing

Implementation details for the second step with enough content to be actionable.

## References

Some reference material that should not be extracted as a task.
"""
            steps = _parse_implementation_section(content)
            assert len(steps) == 2, f"Failed for keyword: {keyword}"

    def test_stops_at_next_h2(self):
        """Should only extract sub-headings within the implementation section."""
        content = """# Plan

## Implementation Plan

### Step A

Do A with enough detail to pass the minimum content threshold.

### Step B

Do B with enough detail to pass the minimum content threshold.

## Appendix

### Extra Reference

This should NOT be extracted since it's outside implementation section.
"""
        steps = _parse_implementation_section(content)
        assert len(steps) == 2
        titles = [s.title for s in steps]
        assert "Step A" in titles
        assert "Step B" in titles
        assert "Extra Reference" not in titles


# ── parse_plan with implementation section ────────────────────────────── #

class TestParsePlanDesignDocument:
    def test_design_document_uses_implementation_section(self):
        """Full parse_plan() should use implementation section parsing for the design doc.

        The 5 raw phases get consolidated into 3 coarser phases.
        """
        plan = parse_plan(DESIGN_DOCUMENT, source_file="plan.md")
        assert len(plan.steps) == 3
        # Verify it extracted the actual phases (consolidated), not the reference sections
        all_text = " ".join(s.title + " " + s.description for s in plan.steps)
        assert "Foundation" in all_text
        assert "Polish & Testing" in all_text

    def test_simple_plan_still_works(self):
        """Normal plans without implementation sections should parse as before."""
        content = """# Plan

## Create database models

Add User and Post models with all required fields and relationships.

## Build API endpoints

Create REST endpoints for CRUD operations with input validation.

## Add frontend components

Build React components for the user interface with proper state management.
"""
        plan = parse_plan(content)
        assert len(plan.steps) == 3
        assert plan.steps[0].title == "Create database models"


# ── Quality scoring ───────────────────────────────────────────────────── #

class TestScoreParseQuality:
    def test_high_quality_actionable_steps(self):
        """Steps with action verbs should score high."""
        steps = [
            PlanStep(title="Create database models", description="..."),
            PlanStep(title="Implement authentication", description="..."),
            PlanStep(title="Add unit tests", description="..."),
        ]
        score = _score_parse_quality(steps)
        assert score >= 0.7

    def test_low_quality_informational_steps(self):
        """Steps that are clearly informational should score low."""
        steps = [
            PlanStep(title="Current State", description="..."),
            PlanStep(title="What Embeds Support", description="..."),
            PlanStep(title="Hard Limits", description="..."),
            PlanStep(title="Color Palette", description="..."),
            PlanStep(title="Design Principles", description="..."),
            PlanStep(title="Existing Implementation", description="..."),
        ]
        score = _score_parse_quality(steps)
        assert score < 0.4

    def test_mixed_quality(self):
        """Mixed steps should have intermediate score."""
        steps = [
            PlanStep(title="Background", description="..."),
            PlanStep(title="Create API endpoints", description="..."),
            PlanStep(title="Color Palette", description="..."),
            PlanStep(title="Add tests", description="..."),
        ]
        score = _score_parse_quality(steps)
        assert 0.0 < score < 1.0

    def test_empty_steps(self):
        assert _score_parse_quality([]) == 0.0

    def test_phase_headings_score_high(self):
        """Phase-numbered headings should always be considered actionable."""
        steps = [
            PlanStep(title="Phase 1: Foundation", description="..."),
            PlanStep(title="Phase 2: Implementation", description="..."),
            PlanStep(title="Phase 3: Testing", description="..."),
        ]
        score = _score_parse_quality(steps)
        assert score >= 0.9


# ── _is_likely_actionable ─────────────────────────────────────────────── #

class TestIsLikelyActionable:
    def test_action_verbs(self):
        assert _is_likely_actionable("Create the user model")
        assert _is_likely_actionable("Add authentication middleware")
        assert _is_likely_actionable("Implement retry logic")
        assert _is_likely_actionable("Update the configuration")
        assert _is_likely_actionable("Fix the login bug")

    def test_phase_pattern(self):
        assert _is_likely_actionable("Phase 1: Foundation")
        assert _is_likely_actionable("Step 3: Deployment")

    def test_informational_headings(self):
        assert not _is_likely_actionable("Color Palette")
        assert not _is_likely_actionable("Hard Limits")
        assert not _is_likely_actionable("What Embeds Support")
        assert not _is_likely_actionable("Design Principles")


# ── find_plan_file — notes directory ──────────────────────────────────── #

class TestFindPlanFileNarrowScope:
    def test_ignores_notes_directory(self, tmp_path):
        """Plans in notes/ are NOT discovered — only .claude/plan.md is checked."""
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        plan = notes_dir / "my-improvement-plan.md"
        plan.write_text("# Plan\n\n## Phase 1: Do stuff\n\nDetails about phase 1.\n")

        result = find_plan_file(str(tmp_path))
        assert result is None

    def test_only_finds_claude_plan_md(self, tmp_path):
        """Only .claude/plan.md is found, even when other plan files exist."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("# Claude Plan")
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "plan.md").write_text("# Notes Plan")
        (tmp_path / "plan.md").write_text("# Root Plan")

        result = find_plan_file(str(tmp_path))
        assert result == str(claude_dir / "plan.md")


# ── _deep_scan_for_plan ───────────────────────────────────────────────── #

class TestDeepScanDisabled:
    def test_deep_scan_always_returns_none(self, tmp_path):
        """Deep scan is disabled — always returns None regardless of content."""
        subdir = tmp_path / "custom" / "location"
        subdir.mkdir(parents=True)
        plan = subdir / "my-plan.md"
        plan.write_text(
            "# Plan\n\n## Phase 1: Do stuff\n\nDetails here.\n"
            "## Phase 2: More stuff\n\nMore details.\n"
        )

        result = _deep_scan_for_plan(str(tmp_path))
        assert result is None

    def test_deep_scan_ignores_all_files(self, tmp_path):
        """Even recent files with plan indicators are not returned."""
        plan = tmp_path / "design-doc.md"
        plan.write_text(
            "# Design\n\n## Background\n\nStuff.\n\n"
            "## Implementation Plan\n\n### Step 1\n\nDo things.\n"
        )

        result = _deep_scan_for_plan(str(tmp_path))
        assert result is None


# ── Full pipeline integration test with keen-beacon plan ──────────────── #

class TestKeenBeaconIntegration:
    """Integration test simulating the keen-beacon failure scenario."""

    def test_plan_in_notes_not_discovered_but_parses_correctly(self, tmp_path):
        """Plan in notes/ is NOT discovered by find_plan_file (narrow scope),
        but if passed directly to parse_plan it parses correctly.

        The 5 raw implementation phases get consolidated into 3 coarser
        phases (threshold=4, target=3).
        """
        # Setup: plan file in notes/ (not a checked location)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        plan_path = notes_dir / "discord-responses-improvement-plan.md"
        plan_path.write_text(DESIGN_DOCUMENT)

        # Step 1: Discovery should NOT find it (only .claude/plan.md is checked)
        found = find_plan_file(str(tmp_path))
        assert found is None, "Plan in notes/ should NOT be discovered"

        # Step 2: But parsing should still work when given the content directly
        content = plan_path.read_text()
        plan = parse_plan(content, source_file=str(plan_path))
        assert len(plan.steps) == 3

        # Step 3: Verify the consolidated phases contain all original phase content
        all_text = " ".join(s.title + " " + s.description for s in plan.steps)
        assert "Foundation" in all_text
        assert "Notification Embeds" in all_text
        assert "Slash Command Consistency" in all_text
        assert "Chat Agent Responses" in all_text
        assert "Polish & Testing" in all_text

        # Step 4: Verify NO reference sections were extracted
        for step in plan.steps:
            title_lower = step.title.lower()
            assert "color palette" not in title_lower
            assert "hard limits" not in title_lower
            assert "what embeds support" not in title_lower
            assert "current state" not in title_lower
            assert "design principles" not in title_lower

        # Step 5: Verify consolidated phases include step outlines
        has_outline = any("Steps in this phase:" in s.description for s in plan.steps)
        assert has_outline, "Consolidated phases should include a step outline"


# ── NON_ACTIONABLE_HEADINGS completeness ──────────────────────────────── #

class TestNonActionableHeadingsExpanded:
    def test_all_entries_are_lowercase(self):
        for heading in NON_ACTIONABLE_HEADINGS:
            assert heading == heading.lower(), f"Entry '{heading}' should be lowercase"

    def test_new_entries_included(self):
        """Verify the newly added entries for design document patterns."""
        assert "current state" in NON_ACTIONABLE_HEADINGS
        assert "color palette" in NON_ACTIONABLE_HEADINGS
        assert "design principles" in NON_ACTIONABLE_HEADINGS
        assert "visual design specification" in NON_ACTIONABLE_HEADINGS
        assert "libraries and dependencies" in NON_ACTIONABLE_HEADINGS
        assert "risk assessment & mitigations" in NON_ACTIONABLE_HEADINGS
        assert "expected outcome" in NON_ACTIONABLE_HEADINGS

    def test_strategy_and_approach_entries_included(self):
        """Verify strategy/approach headings are filtered as non-actionable."""
        assert "strategy" in NON_ACTIONABLE_HEADINGS
        assert "approach" in NON_ACTIONABLE_HEADINGS
        assert "methodology" in NON_ACTIONABLE_HEADINGS
        assert "rationale" in NON_ACTIONABLE_HEADINGS
        assert "alternatives" in NON_ACTIONABLE_HEADINGS
        assert "trade-offs" in NON_ACTIONABLE_HEADINGS

    def test_miscellaneous_informational_entries(self):
        """Verify miscellaneous informational headings are non-actionable."""
        assert "changelog" in NON_ACTIONABLE_HEADINGS
        assert "known issues" in NON_ACTIONABLE_HEADINGS
        assert "limitations" in NON_ACTIONABLE_HEADINGS
        assert "troubleshooting" in NON_ACTIONABLE_HEADINGS
        assert "best practices" in NON_ACTIONABLE_HEADINGS
        assert "patterns" in NON_ACTIONABLE_HEADINGS

    def test_step_heading_pattern(self):
        """Phase/Step patterns should be detected."""
        assert STEP_HEADING_PATTERN.match("Phase 1: Foundation")
        assert STEP_HEADING_PATTERN.match("Step 3: Testing")
        assert STEP_HEADING_PATTERN.match("Part 2: Implementation")
        assert not STEP_HEADING_PATTERN.match("Color Palette")
        assert not STEP_HEADING_PATTERN.match("What Embeds Support")


# ── Regression tests: max_steps enforcement ────────────────────────────── #

class TestMaxStepsEnforcement:
    """Ensure parse_plan() respects the max_steps parameter."""

    def test_max_steps_caps_implementation_section_steps(self):
        """Implementation section with many steps should be consolidated
        into fewer phases and then capped by max_steps."""
        content = "# Plan\n\n## Implementation Plan\n\n"
        for i in range(1, 12):
            content += f"### Phase {i}: Do thing {i}\n\n"
            content += f"Detailed description for phase {i} with enough content.\n\n"

        plan = parse_plan(content, max_steps=3)
        # 11 steps exceeds the phase threshold (4), so they get consolidated
        # into 3 phases (target_phases=3), then capped at max_steps=3
        assert len(plan.steps) <= 3
        # Should have been consolidated (fewer than 11)
        assert len(plan.steps) < 11

    def test_max_steps_caps_heading_sections(self):
        """Heading-based parsing should also respect max_steps."""
        content = "# Plan\n\n"
        for i in range(1, 12):
            content += f"## Create component {i}\n\n"
            content += f"Build component {i} with all necessary features and styles.\n\n"

        plan = parse_plan(content, max_steps=3)
        assert len(plan.steps) == 3

    def test_max_steps_caps_numbered_list(self):
        """Numbered list with many items should be consolidated into
        fewer phases and then capped by max_steps."""
        items = "\n".join(
            f"{i}. Implement feature {i}\n   Details for feature {i}."
            for i in range(1, 15)
        )
        plan = parse_plan(items, max_steps=5)
        # 14 items exceeds the phase threshold (4), so they get consolidated
        # into 3 phases (target_phases=3), then capped at max_steps=5
        assert len(plan.steps) <= 5
        # Should have been consolidated (fewer than 14)
        assert len(plan.steps) < 14


# ── Regression tests: quality scoring filters garbage parses ────────────── #

class TestQualityScoringFilters:
    """Verify that quality scoring filters out garbage parses with
    mostly-informational headings (the root cause of the 33-step explosion)."""

    def test_heavily_informational_plan_is_filtered(self):
        """A plan with 80%+ informational headings should be filtered to
        only actionable steps when parsed through parse_plan()."""
        content = """# Investigation Report

## Overview

This is a comprehensive analysis of the system architecture.

## Current Architecture Review

The system currently uses a modular design with clear separation.

## Strategy

The recommended approach uses progressive enhancement.

## Approach

We suggest a phased rollout to minimize risk.

## Limitations

Known constraints include API rate limits and memory usage.

## Analysis

Performance profiling reveals bottleneck in the database layer.

## Create notification module

Implement the new notification module with event-based design and hooks.

## Add retry logic

Build retry logic with exponential backoff for failed operations.

## Fix error handling

Update all error handlers to use consistent error formatting patterns.
"""
        plan = parse_plan(content)
        # Should have extracted the actionable steps (create/add/fix)
        # The quality filter should have removed the informational ones
        titles = [s.title.lower() for s in plan.steps]
        # The three actionable steps should survive
        assert any("notification module" in t for t in titles)
        assert any("retry logic" in t for t in titles)
        assert any("error handling" in t for t in titles)
        # Informational headings should be filtered out
        assert not any(t == "overview" for t in titles)
        assert not any(t == "strategy" for t in titles)
        assert not any(t == "approach" for t in titles)
        assert not any(t == "limitations" for t in titles)
        assert not any(t == "analysis" for t in titles)

    def test_quality_score_with_new_informational_keywords(self):
        """Quality scorer should rate steps with newly added keywords as low."""
        steps = [
            PlanStep(title="Strategy Overview", description="..."),
            PlanStep(title="Approach Comparison", description="..."),
            PlanStep(title="Known Limitations", description="..."),
            PlanStep(title="Analysis of Alternatives", description="..."),
        ]
        score = _score_parse_quality(steps)
        assert score < 0.3, f"Score {score} too high for purely informational steps"


# ── Regression: implementation section with non-actionable sub-headings ── #

class TestImplementationSectionWithNonActionable:
    """Verify that non-actionable headings WITHIN an implementation section
    are correctly filtered out."""

    def test_filters_non_actionable_within_implementation(self):
        """Even within '## Implementation Plan', sub-headings like
        'Summary' or 'Overview' should be skipped."""
        content = """# Plan

## Implementation Plan

### Overview

This section provides a brief overview of the implementation.

### Phase 1: Build the foundation

Create the core module with base classes and interfaces for the system.

### Testing Notes

Quick notes about the testing approach for reference purposes only.

### Phase 2: Add integrations

Integrate with external APIs and add webhook support for notifications.

### Summary

A summary of all implementation work that was planned above.
"""
        steps = _parse_implementation_section(content)
        titles = [s.title for s in steps]
        # Should only extract the actual phases
        assert "Build the foundation" in titles
        assert "Add integrations" in titles
        # Should skip non-actionable sub-headings
        assert "Overview" not in titles
        assert "Testing Notes" not in titles
        assert "Summary" not in titles
        assert len(steps) == 2


# ── Regression: deep scan finds plans in unusual directory structures ──── #

class TestDeepScanEdgeCases:
    def test_deep_scan_disabled_for_nested(self, tmp_path):
        """Deep scan is disabled — nested plans are not found."""
        deep_dir = tmp_path / "src" / "docs" / "internal" / "plans"
        deep_dir.mkdir(parents=True)
        plan = deep_dir / "feature-plan.md"
        plan.write_text(
            "# Feature Plan\n\n"
            "## Phase 1: Setup\n\nSetup the project structure.\n\n"
            "## Phase 2: Implementation\n\nImplement the core feature.\n"
        )

        result = _deep_scan_for_plan(str(tmp_path))
        assert result is None

    def test_deep_scan_disabled_for_implementation_steps(self, tmp_path):
        """Deep scan is disabled — implementation steps keyword not detected."""
        plan = tmp_path / "design.md"
        plan.write_text(
            "# Design\n\n## Background\n\nContext.\n\n"
            "## Implementation Steps\n\n### Do thing 1\n\nDetails.\n"
        )

        result = _deep_scan_for_plan(str(tmp_path))
        assert result is None
