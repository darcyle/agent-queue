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
        """The key test: should extract ONLY Phase 1-5 from the design doc."""
        steps = _parse_implementation_section(DESIGN_DOCUMENT)
        assert len(steps) == 5
        titles = [s.title for s in steps]
        assert "Foundation (Low Risk)" in titles
        assert "Notification Embeds (Medium Risk)" in titles
        assert "Slash Command Consistency (Medium Risk)" in titles
        assert "Chat Agent Responses (Low Risk)" in titles
        assert "Polish & Testing" in titles

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

    def test_priority_hints_sequential(self):
        steps = _parse_implementation_section(DESIGN_DOCUMENT)
        assert [s.priority_hint for s in steps] == [0, 1, 2, 3, 4]

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
        """Full parse_plan() should use implementation section parsing for the design doc."""
        plan = parse_plan(DESIGN_DOCUMENT, source_file="plan.md")
        assert len(plan.steps) == 5
        # Verify it extracted the actual phases, not the reference sections
        titles = [s.title for s in plan.steps]
        assert "Foundation (Low Risk)" in titles
        assert "Polish & Testing" in titles

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

class TestFindPlanFileNotes:
    def test_finds_plan_in_notes_directory(self, tmp_path):
        """Plans in notes/ should be discovered via the expanded patterns."""
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        plan = notes_dir / "my-improvement-plan.md"
        plan.write_text("# Plan\n\n## Phase 1: Do stuff\n\nDetails about phase 1.\n")

        result = find_plan_file(str(tmp_path))
        assert result == str(plan)

    def test_prefers_claude_dir_over_notes(self, tmp_path):
        """Standard patterns should take priority over notes/."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("# Claude Plan")
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "plan.md").write_text("# Notes Plan")

        result = find_plan_file(str(tmp_path))
        assert result == str(claude_dir / "plan.md")


# ── _deep_scan_for_plan ───────────────────────────────────────────────── #

class TestDeepScanForPlan:
    def test_finds_recent_plan_with_phase_headings(self, tmp_path):
        """Deep scan should find markdown files with Phase/Step headings."""
        subdir = tmp_path / "custom" / "location"
        subdir.mkdir(parents=True)
        plan = subdir / "my-plan.md"
        plan.write_text(
            "# Plan\n\n## Phase 1: Do stuff\n\nDetails here.\n"
            "## Phase 2: More stuff\n\nMore details.\n"
        )

        result = _deep_scan_for_plan(str(tmp_path))
        assert result == str(plan)

    def test_ignores_old_files(self, tmp_path):
        """Files older than max_age_seconds should be ignored."""
        plan = tmp_path / "old-plan.md"
        plan.write_text("# Plan\n\n## Phase 1: Stuff\n\nDetails.\n")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(str(plan), (old_time, old_time))

        result = _deep_scan_for_plan(str(tmp_path), max_age_seconds=1800)
        assert result is None

    def test_ignores_archived_plans(self, tmp_path):
        """Plans in .claude/plans/ should be skipped (they're already processed)."""
        plans_dir = tmp_path / ".claude" / "plans"
        plans_dir.mkdir(parents=True)
        archived = plans_dir / "task-123-plan.md"
        archived.write_text("# Archived\n\n## Phase 1: Stuff\n\nDetails.\n")

        result = _deep_scan_for_plan(str(tmp_path))
        assert result is None

    def test_ignores_files_without_plan_indicators(self, tmp_path):
        """Markdown files without Phase/Step headings should be skipped."""
        readme = tmp_path / "README.md"
        readme.write_text("# My Project\n\nThis is a README.\n")

        result = _deep_scan_for_plan(str(tmp_path))
        assert result is None

    def test_finds_implementation_plan_keyword(self, tmp_path):
        """Files with 'Implementation Plan' heading should be found."""
        plan = tmp_path / "design-doc.md"
        plan.write_text(
            "# Design\n\n## Background\n\nStuff.\n\n"
            "## Implementation Plan\n\n### Step 1\n\nDo things.\n"
        )

        result = _deep_scan_for_plan(str(tmp_path))
        assert result == str(plan)

    def test_returns_newest_when_multiple(self, tmp_path):
        """When multiple candidates exist, the newest should be returned."""
        plan1 = tmp_path / "plan-old.md"
        plan1.write_text("# Old\n\n## Phase 1: Stuff\n\nDetails.\n")
        time.sleep(0.05)
        plan2 = tmp_path / "plan-new.md"
        plan2.write_text("# New\n\n## Phase 1: Stuff\n\nDetails.\n")

        result = _deep_scan_for_plan(str(tmp_path))
        assert result == str(plan2)


# ── Full pipeline integration test with keen-beacon plan ──────────────── #

class TestKeenBeaconIntegration:
    """Integration test simulating the keen-beacon failure scenario."""

    def test_plan_in_notes_is_discovered_and_correctly_parsed(self, tmp_path):
        """Simulate the keen-beacon scenario: plan in notes/ with design doc structure."""
        # Setup: plan file in notes/ (not standard location)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        plan_path = notes_dir / "discord-responses-improvement-plan.md"
        plan_path.write_text(DESIGN_DOCUMENT)

        # Step 1: Discovery should find it
        found = find_plan_file(str(tmp_path))
        assert found is not None, "Plan in notes/ should be discovered"
        assert found == str(plan_path)

        # Step 2: Parsing should extract only the implementation phases
        content = plan_path.read_text()
        plan = parse_plan(content, source_file=str(plan_path))
        assert len(plan.steps) == 5

        # Step 3: Verify the correct phases were extracted
        titles = [s.title for s in plan.steps]
        assert "Foundation (Low Risk)" in titles
        assert "Notification Embeds (Medium Risk)" in titles
        assert "Slash Command Consistency (Medium Risk)" in titles
        assert "Chat Agent Responses (Low Risk)" in titles
        assert "Polish & Testing" in titles

        # Step 4: Verify NO reference sections were extracted
        for step in plan.steps:
            title_lower = step.title.lower()
            assert "color palette" not in title_lower
            assert "hard limits" not in title_lower
            assert "what embeds support" not in title_lower
            assert "current state" not in title_lower
            assert "design principles" not in title_lower


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

    def test_step_heading_pattern(self):
        """Phase/Step patterns should be detected."""
        assert STEP_HEADING_PATTERN.match("Phase 1: Foundation")
        assert STEP_HEADING_PATTERN.match("Step 3: Testing")
        assert STEP_HEADING_PATTERN.match("Part 2: Implementation")
        assert not STEP_HEADING_PATTERN.match("Color Palette")
        assert not STEP_HEADING_PATTERN.match("What Embeds Support")
