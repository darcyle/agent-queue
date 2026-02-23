"""Tests for src/plan_parser.py — plan file reading and parsing."""

import os
import pytest

from src.plan_parser import (
    PlanStep,
    ParsedPlan,
    find_plan_file,
    read_plan_file,
    parse_plan,
    build_task_description,
    _clean_step_title,
    _parse_heading_sections,
    _parse_numbered_list,
)


# ── find_plan_file ──────────────────────────────────────────────────────── #

class TestFindPlanFile:
    def test_finds_claude_plan_file(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        plan = claude_dir / "plan.md"
        plan.write_text("# Plan")

        result = find_plan_file(str(tmp_path))
        assert result == str(plan)

    def test_finds_root_plan_file(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("# Plan")

        result = find_plan_file(str(tmp_path))
        assert result == str(plan)

    def test_prefers_claude_dir_over_root(self, tmp_path):
        # Both exist; .claude/plan.md should be found first
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "plan.md").write_text("# Claude Plan")
        (tmp_path / "plan.md").write_text("# Root Plan")

        result = find_plan_file(str(tmp_path))
        assert result == str(claude_dir / "plan.md")

    def test_returns_none_when_no_plan_exists(self, tmp_path):
        assert find_plan_file(str(tmp_path)) is None

    def test_custom_patterns(self, tmp_path):
        custom = tmp_path / "my-plan.md"
        custom.write_text("# Custom Plan")

        assert find_plan_file(str(tmp_path), ["my-plan.md"]) == str(custom)
        assert find_plan_file(str(tmp_path), ["nonexistent.md"]) is None

    def test_ignores_directories(self, tmp_path):
        # If plan.md is a directory, it should not be returned
        plan_dir = tmp_path / "plan.md"
        plan_dir.mkdir()

        assert find_plan_file(str(tmp_path)) is None


# ── read_plan_file ──────────────────────────────────────────────────────── #

class TestReadPlanFile:
    def test_reads_file_content(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("Hello, plan!")

        assert read_plan_file(str(plan)) == "Hello, plan!"

    def test_reads_utf8(self, tmp_path):
        plan = tmp_path / "plan.md"
        plan.write_text("Plan with special chars: é, ñ, ü", encoding="utf-8")

        assert "é" in read_plan_file(str(plan))

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_plan_file(str(tmp_path / "nonexistent.md"))


# ── parse_plan — heading-based ─────────────────────────────────────────── #

class TestParseHeadingSections:
    def test_basic_h2_sections(self):
        content = """# Implementation Plan

## Update the database schema

Add a new `users` table with email column.

## Create API endpoints

Build REST endpoints for CRUD operations.

## Write tests

Add pytest test cases for all endpoints.
"""
        plan = parse_plan(content, source_file="plan.md")
        assert len(plan.steps) == 3
        assert plan.steps[0].title == "Update the database schema"
        assert "users" in plan.steps[0].description
        assert plan.steps[1].title == "Create API endpoints"
        assert plan.steps[2].title == "Write tests"

    def test_h3_sections(self):
        content = """# Plan

### First task

Details about first task.

### Second task

Details about second task.
"""
        plan = parse_plan(content)
        assert len(plan.steps) == 2
        assert plan.steps[0].title == "First task"
        assert plan.steps[1].title == "Second task"

    def test_removes_step_prefix(self):
        content = """## Step 1: Setup database

Content.

## Step 2: Add migrations

More content.
"""
        plan = parse_plan(content)
        assert plan.steps[0].title == "Setup database"
        assert plan.steps[1].title == "Add migrations"

    def test_removes_phase_prefix(self):
        content = """## Phase 1 - Foundation

Content.

## Phase 2 - Implementation

More content.
"""
        plan = parse_plan(content)
        assert plan.steps[0].title == "Foundation"
        assert plan.steps[1].title == "Implementation"

    def test_removes_bold_markdown(self):
        content = """## **Setup the project**

Content here.

## **Deploy the service**

More content.
"""
        plan = parse_plan(content)
        assert plan.steps[0].title == "Setup the project"
        assert plan.steps[1].title == "Deploy the service"

    def test_preserves_description_content(self):
        content = """## Add authentication

- Install `bcrypt` library
- Create `auth.py` module
- Add login/logout endpoints

```python
from bcrypt import hashpw
```

## Update frontend

- Add login form
"""
        plan = parse_plan(content)
        assert len(plan.steps) == 2
        assert "bcrypt" in plan.steps[0].description
        assert "hashpw" in plan.steps[0].description
        assert "login form" in plan.steps[1].description

    def test_priority_hints_are_sequential(self):
        content = """## A

Desc.

## B

Desc.

## C

Desc.
"""
        plan = parse_plan(content)
        assert [s.priority_hint for s in plan.steps] == [0, 1, 2]


# ── parse_plan — numbered list ─────────────────────────────────────────── #

class TestParseNumberedList:
    def test_basic_numbered_list(self):
        content = """1. Create the database models
2. Build the API layer
3. Write integration tests
"""
        plan = parse_plan(content)
        assert len(plan.steps) == 3
        assert plan.steps[0].title == "Create the database models"
        assert plan.steps[1].title == "Build the API layer"
        assert plan.steps[2].title == "Write integration tests"

    def test_numbered_list_with_subitems(self):
        content = """1. Set up project structure
   - Create src/ directory
   - Initialize package.json

2. Implement core logic
   - Add parser module
   - Add formatter module

3. Add CLI interface
"""
        plan = parse_plan(content)
        assert len(plan.steps) == 3
        assert "src/ directory" in plan.steps[0].description
        assert "parser module" in plan.steps[1].description

    def test_numbered_list_with_parenthesis(self):
        content = """1) First task
2) Second task
3) Third task
"""
        plan = parse_plan(content)
        assert len(plan.steps) == 3

    def test_title_truncation(self):
        long_title = "A" * 200
        content = f"1. {long_title}\n2. Short title\n"
        plan = parse_plan(content)
        assert len(plan.steps[0].title) <= 120
        assert plan.steps[0].title.endswith("...")


# ── parse_plan — edge cases ────────────────────────────────────────────── #

class TestParsePlanEdgeCases:
    def test_empty_content(self):
        plan = parse_plan("")
        assert plan.steps == []

    def test_whitespace_only(self):
        plan = parse_plan("   \n\n  \t  \n")
        assert plan.steps == []

    def test_single_paragraph_no_structure(self):
        content = """# My Plan

This is a free-form description of what needs to be done.
It has multiple lines but no structured steps.
"""
        plan = parse_plan(content)
        # Should fall back to treating the body as a single step
        assert len(plan.steps) == 1
        assert "free-form description" in plan.steps[0].description

    def test_heading_based_takes_priority_over_numbered(self):
        content = """## Task A

1. Sub-item under A
2. Another sub-item

## Task B

Some description.
"""
        plan = parse_plan(content)
        # Heading-based should win
        assert len(plan.steps) == 2
        assert plan.steps[0].title == "Task A"
        assert plan.steps[1].title == "Task B"

    def test_source_file_is_preserved(self):
        plan = parse_plan("## Step\nContent.", source_file="/path/plan.md")
        assert plan.source_file == "/path/plan.md"

    def test_raw_content_is_preserved(self):
        content = "## Step\nContent."
        plan = parse_plan(content)
        assert plan.raw_content == content

    def test_skips_empty_headings(self):
        content = """##

Nothing here.

## Real step

Content.
"""
        plan = parse_plan(content)
        # The empty heading should be skipped
        assert len(plan.steps) >= 1
        has_real = any(s.title == "Real step" for s in plan.steps)
        assert has_real


# ── _clean_step_title ───────────────────────────────────────────────────── #

class TestCleanStepTitle:
    def test_removes_step_prefix(self):
        assert _clean_step_title("Step 1: Setup") == "Setup"
        assert _clean_step_title("Step 2 - Build") == "Build"
        assert _clean_step_title("Step3: Test") == "Test"

    def test_removes_phase_prefix(self):
        assert _clean_step_title("Phase 1: Init") == "Init"

    def test_removes_numbered_prefix(self):
        assert _clean_step_title("1. First") == "First"
        assert _clean_step_title("2) Second") == "Second"
        assert _clean_step_title("3: Third") == "Third"

    def test_removes_bold(self):
        assert _clean_step_title("**Bold Title**") == "Bold Title"
        assert _clean_step_title("*Italic Title*") == "Italic Title"

    def test_handles_combined(self):
        assert _clean_step_title("Step 1: **Create Models**") == "Create Models"

    def test_preserves_plain_text(self):
        assert _clean_step_title("Just a title") == "Just a title"


# ── build_task_description ──────────────────────────────────────────────── #

class TestBuildTaskDescription:
    def test_basic_description(self):
        step = PlanStep(title="Add logging", description="Add structured logging.")
        desc = build_task_description(step)
        assert "Add logging" in desc
        assert "structured logging" in desc

    def test_includes_parent_context(self):
        step = PlanStep(title="Add tests", description="Write pytest tests.")

        class FakeTask:
            title = "Refactor auth system"
            description = "Full refactor..."

        desc = build_task_description(step, parent_task=FakeTask())
        assert "Refactor auth system" in desc

    def test_includes_plan_context(self):
        step = PlanStep(title="Step one", description="Details.")
        desc = build_task_description(
            step, plan_context="This plan covers the migration."
        )
        assert "migration" in desc

    def test_description_is_self_contained(self):
        step = PlanStep(
            title="Deploy to prod",
            description="Run `kubectl apply -f deploy.yaml`",
        )

        class FakeTask:
            title = "Infrastructure setup"

        desc = build_task_description(
            step,
            parent_task=FakeTask(),
            plan_context="Setting up K8s cluster for the app.",
        )
        # Should contain title, context, parent ref, and details
        assert "Deploy to prod" in desc
        assert "Infrastructure setup" in desc
        assert "K8s cluster" in desc
        assert "kubectl" in desc
