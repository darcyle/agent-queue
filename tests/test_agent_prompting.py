"""Tests for the agent prompting module.

These tests verify Layer 3 of the multi-layer task splitting fix:
  - Correct prompt selection based on task depth and type
  - Plan structure guide inclusion in appropriate prompts
  - Execution-only mode for max-depth tasks
  - Design document detection in agent output
  - Retry prompt generation with stricter instructions
"""

import pytest

from src.agent_prompting import (
    CONTROLLED_SPLITTING_INSTRUCTIONS,
    EXECUTION_FOCUS_INSTRUCTIONS,
    PLAN_STRUCTURE_GUIDE,
    PromptConfig,
    TaskContext,
    build_execution_prompt,
    build_plan_generation_prompt,
    build_retry_prompt,
    build_subtask_prompt,
    detect_design_doc_output,
    select_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def root_task():
    """A root-level task (not a plan subtask)."""
    return TaskContext(
        task_id="root-task",
        title="Implement User Authentication",
        description="Add JWT-based authentication to the API.",
        project_id="my-project",
        is_plan_subtask=False,
        plan_depth=0,
    )


@pytest.fixture
def depth_one_subtask():
    """A depth-1 plan subtask (generated from a plan)."""
    return TaskContext(
        task_id="subtask-a",
        title="Phase 1: Create Auth Module",
        description="Implement src/auth.py with JWT token generation.",
        project_id="my-project",
        is_plan_subtask=True,
        plan_depth=1,
        parent_title="Implement User Authentication",
        parent_description="Add JWT-based authentication to the API.",
    )


@pytest.fixture
def max_depth_subtask():
    """A max-depth plan subtask (should only execute, no splitting)."""
    return TaskContext(
        task_id="subtask-b",
        title="Create JWT Helper Functions",
        description="Implement encode_token() and decode_token() in auth.py.",
        project_id="my-project",
        is_plan_subtask=True,
        plan_depth=2,
        parent_title="Phase 1: Create Auth Module",
    )


@pytest.fixture
def config():
    """Default prompt configuration."""
    return PromptConfig(
        max_plan_depth=2,
        max_steps_per_plan=10,
        project_context="A Python web API using FastAPI and SQLAlchemy.",
    )


# ---------------------------------------------------------------------------
# Tests: Prompt selection
# ---------------------------------------------------------------------------

class TestSelectPrompt:
    """Test automatic prompt selection based on task context."""

    def test_root_task_gets_plan_generation(self, root_task, config):
        """Root tasks should get plan generation prompts."""
        prompt = select_prompt(root_task, config)
        assert "Plan Structure Requirements" in prompt
        assert "Ideal Plan Structure" in prompt

    def test_depth_one_gets_controlled_splitting(self, depth_one_subtask, config):
        """Depth-1 subtasks should get controlled splitting prompts."""
        prompt = select_prompt(depth_one_subtask, config)
        assert "Optional Sub-Planning" in prompt

    def test_max_depth_gets_execution_only(self, max_depth_subtask, config):
        """Max-depth subtasks should get execution-only prompts."""
        prompt = select_prompt(max_depth_subtask, config)
        assert "Execution Focus" in prompt
        assert "Do not produce a new implementation plan" in prompt

    def test_non_plan_subtask_gets_plan_generation(self, config):
        """Non-plan subtasks should get plan generation prompts."""
        task = TaskContext(
            task_id="manual-task",
            title="Add Feature X",
            is_plan_subtask=False,
            plan_depth=0,
        )
        prompt = select_prompt(task, config)
        assert "Plan Structure Requirements" in prompt


# ---------------------------------------------------------------------------
# Tests: Plan generation prompt
# ---------------------------------------------------------------------------

class TestBuildPlanGenerationPrompt:
    """Test the plan generation prompt builder."""

    def test_includes_task_title(self, root_task, config):
        """Should include the task title."""
        prompt = build_plan_generation_prompt(root_task, config)
        assert "Implement User Authentication" in prompt

    def test_includes_description(self, root_task, config):
        """Should include the task description."""
        prompt = build_plan_generation_prompt(root_task, config)
        assert "JWT-based authentication" in prompt

    def test_includes_plan_structure_guide(self, root_task, config):
        """Should include the plan structure formatting guide."""
        prompt = build_plan_generation_prompt(root_task, config)
        assert "## Plan Structure Requirements" in prompt
        assert "### DO:" in prompt
        assert "### DON'T:" in prompt

    def test_includes_project_context(self, root_task, config):
        """Should include project context when provided."""
        prompt = build_plan_generation_prompt(root_task, config)
        assert "FastAPI and SQLAlchemy" in prompt

    def test_max_steps_in_guide(self, root_task, config):
        """The plan structure guide should mention the max steps."""
        config.max_steps_per_plan = 15
        prompt = build_plan_generation_prompt(root_task, config)
        assert "15" in prompt

    def test_includes_dependency_context(self, root_task, config):
        """Should include dependency information when available."""
        root_task.dependency_titles = [
            "Set Up Database Schema",
            "Configure Environment Variables",
        ]
        prompt = build_plan_generation_prompt(root_task, config)
        assert "Set Up Database Schema" in prompt
        assert "Configure Environment Variables" in prompt
        assert "Completed Dependencies" in prompt

    def test_includes_files_in_scope(self, root_task, config):
        """Should include file scope when available."""
        root_task.files_in_scope = ["src/auth.py", "src/middleware.py"]
        prompt = build_plan_generation_prompt(root_task, config)
        assert "src/auth.py" in prompt
        assert "src/middleware.py" in prompt

    def test_anti_pattern_guidance(self, root_task, config):
        """Should include guidance against design document patterns."""
        prompt = build_plan_generation_prompt(root_task, config)
        # The DON'T section should warn against common anti-patterns
        assert "overview/summary" in prompt.lower() or "design discussion" in prompt.lower()

    def test_action_verb_guidance(self, root_task, config):
        """Should mention action-verb headings."""
        prompt = build_plan_generation_prompt(root_task, config)
        assert "action-verb" in prompt.lower() or "Implement X" in prompt


# ---------------------------------------------------------------------------
# Tests: Execution prompt
# ---------------------------------------------------------------------------

class TestBuildExecutionPrompt:
    """Test the execution-only prompt builder."""

    def test_includes_task_title(self, max_depth_subtask, config):
        """Should include the task title."""
        prompt = build_execution_prompt(max_depth_subtask, config)
        assert "Create JWT Helper Functions" in prompt

    def test_includes_execution_instructions(self, max_depth_subtask, config):
        """Should include execution focus instructions."""
        prompt = build_execution_prompt(max_depth_subtask, config)
        assert "Execution Focus" in prompt
        assert "execute" in prompt.lower()

    def test_no_plan_instructions(self, max_depth_subtask, config):
        """Should NOT include plan generation instructions."""
        prompt = build_execution_prompt(max_depth_subtask, config)
        assert "Do not produce a new implementation plan" in prompt

    def test_includes_parent_context(self, max_depth_subtask, config):
        """Should include parent task context."""
        prompt = build_execution_prompt(max_depth_subtask, config)
        assert "Phase 1: Create Auth Module" in prompt

    def test_truncates_long_parent_description(self, config):
        """Should truncate very long parent descriptions."""
        task = TaskContext(
            task_id="subtask",
            title="Small Task",
            is_plan_subtask=True,
            plan_depth=2,
            parent_title="Big Parent",
            parent_description="x" * 1000,
        )
        prompt = build_execution_prompt(task, config)
        # Should truncate and add ellipsis
        assert "..." in prompt

    def test_includes_description(self, max_depth_subtask, config):
        """Should include task description."""
        prompt = build_execution_prompt(max_depth_subtask, config)
        assert "encode_token" in prompt
        assert "decode_token" in prompt


# ---------------------------------------------------------------------------
# Tests: Subtask prompt (depth-aware)
# ---------------------------------------------------------------------------

class TestBuildSubtaskPrompt:
    """Test the depth-aware subtask prompt builder."""

    def test_at_max_depth_uses_execution_prompt(self, max_depth_subtask, config):
        """At max depth, should use execution-only prompt."""
        prompt = build_subtask_prompt(max_depth_subtask, config)
        assert "Execution Focus" in prompt

    def test_below_max_depth_allows_splitting(self, depth_one_subtask, config):
        """Below max depth, should allow controlled splitting."""
        prompt = build_subtask_prompt(depth_one_subtask, config)
        assert "Optional Sub-Planning" in prompt

    def test_shows_current_depth(self, depth_one_subtask, config):
        """Should show the current depth in the instructions."""
        prompt = build_subtask_prompt(depth_one_subtask, config)
        assert "depth 1/2" in prompt or "1/2" in prompt

    def test_includes_plan_structure_guide(self, depth_one_subtask, config):
        """Below max depth, should include plan structure guide."""
        prompt = build_subtask_prompt(depth_one_subtask, config)
        assert "Plan Structure Requirements" in prompt

    def test_limits_sub_plan_steps(self, depth_one_subtask, config):
        """Sub-plan step limit should be lower than root plan."""
        config.max_steps_per_plan = 20
        prompt = build_subtask_prompt(depth_one_subtask, config)
        # Should limit to 5 steps for sub-plans
        assert "5" in prompt

    def test_prefers_direct_execution(self, depth_one_subtask, config):
        """Should recommend direct execution over sub-planning."""
        prompt = build_subtask_prompt(depth_one_subtask, config)
        assert "Prefer direct execution" in prompt or "preferred" in prompt.lower()


# ---------------------------------------------------------------------------
# Tests: Design document detection
# ---------------------------------------------------------------------------

class TestDetectDesignDocOutput:
    """Test detection of design documents in agent output."""

    def test_detects_design_document(self):
        """Should detect content with multiple design doc signals."""
        content = """\
# Feature Design

## Executive Summary

This document describes the architecture for Feature X.

## Architecture Review

The current system uses microservices.

## Design Principles

1. Separation of concerns
2. Single responsibility

## Risk Assessment

| Risk | Impact |
|------|--------|
| Data loss | High |

## Trade-offs

We considered several approaches.

## Alternatives Considered

Option A vs Option B.
"""
        is_design, signals = detect_design_doc_output(content)
        assert is_design is True
        assert len(signals) >= 3

    def test_does_not_flag_implementation_plan(self):
        """Should not flag a focused implementation plan."""
        content = """\
# Implementation Plan

## Phase 1: Create Module

Implement the authentication module.

## Phase 2: Add Tests

Write unit tests.

## Phase 3: Deploy

Deploy to staging.
"""
        is_design, signals = detect_design_doc_output(content)
        assert is_design is False

    def test_returns_specific_signals(self):
        """Should return the specific signals that were detected."""
        content = "## Executive Summary\n\nSome text.\n\n## Architecture Review\n\nMore text."
        _, signals = detect_design_doc_output(content)
        assert "executive summary" in signals
        assert "architecture review" in signals

    def test_threshold_is_three_signals(self):
        """Should require 3+ signals to classify as design doc."""
        # Two signals — not enough
        content = "## Executive Summary\n\nText.\n\n## Risk Assessment\n\nText."
        is_design, signals = detect_design_doc_output(content)
        assert len(signals) == 2
        assert is_design is False

    def test_case_insensitive(self):
        """Should detect signals case-insensitively."""
        content = """\
## EXECUTIVE SUMMARY
Text.
## ARCHITECTURE REVIEW
Text.
## DESIGN PRINCIPLES
Text.
"""
        is_design, _ = detect_design_doc_output(content)
        assert is_design is True

    def test_empty_content(self):
        """Should handle empty content gracefully."""
        is_design, signals = detect_design_doc_output("")
        assert is_design is False
        assert len(signals) == 0


# ---------------------------------------------------------------------------
# Tests: Retry prompt
# ---------------------------------------------------------------------------

class TestBuildRetryPrompt:
    """Test retry prompt generation for design doc corrections."""

    def test_mentions_retry(self, root_task, config):
        """Should clearly indicate this is a retry."""
        prompt = build_retry_prompt(
            root_task,
            original_output="...",
            design_signals=["executive summary", "architecture review", "design principles"],
            config=config,
        )
        assert "RETRY" in prompt

    def test_lists_detected_signals(self, root_task, config):
        """Should list the detected design doc signals."""
        signals = ["executive summary", "architecture review", "risk assessment"]
        prompt = build_retry_prompt(
            root_task,
            original_output="...",
            design_signals=signals,
            config=config,
        )
        for signal in signals:
            assert signal in prompt

    def test_includes_plan_structure_guide(self, root_task, config):
        """Should include the plan structure guide."""
        prompt = build_retry_prompt(
            root_task,
            original_output="...",
            design_signals=["executive summary", "architecture review", "design principles"],
            config=config,
        )
        assert "Plan Structure Requirements" in prompt

    def test_emphasizes_action_verbs(self, root_task, config):
        """Should emphasize using action verbs in headings."""
        prompt = build_retry_prompt(
            root_task,
            original_output="...",
            design_signals=["executive summary", "architecture review", "design principles"],
            config=config,
        )
        assert "action verb" in prompt.lower()

    def test_includes_original_task_description(self, root_task, config):
        """Should include the original task description for context."""
        prompt = build_retry_prompt(
            root_task,
            original_output="...",
            design_signals=["executive summary"],
            config=config,
        )
        assert "JWT-based authentication" in prompt


# ---------------------------------------------------------------------------
# Tests: Plan structure guide content
# ---------------------------------------------------------------------------

class TestPlanStructureGuide:
    """Test the plan structure guide template."""

    def test_has_do_section(self):
        """Should have a DO section with positive guidance."""
        assert "### DO:" in PLAN_STRUCTURE_GUIDE

    def test_has_dont_section(self):
        """Should have a DON'T section with anti-patterns."""
        assert "### DON'T:" in PLAN_STRUCTURE_GUIDE

    def test_has_ideal_structure_example(self):
        """Should include an ideal plan structure example."""
        assert "### Ideal Plan Structure:" in PLAN_STRUCTURE_GUIDE or \
               "Ideal Plan Structure" in PLAN_STRUCTURE_GUIDE

    def test_mentions_action_verbs(self):
        """Should mention using action-verb headings."""
        assert "action-verb" in PLAN_STRUCTURE_GUIDE.lower()

    def test_mentions_estimated_effort(self):
        """Should mention including effort estimates."""
        assert "Estimated effort" in PLAN_STRUCTURE_GUIDE

    def test_warns_against_overview(self):
        """Should warn against overview/summary sections."""
        guide_lower = PLAN_STRUCTURE_GUIDE.lower()
        assert "overview" in guide_lower or "summary" in guide_lower

    def test_warns_against_design_docs(self):
        """Should warn against writing design documents."""
        guide_lower = PLAN_STRUCTURE_GUIDE.lower()
        assert "design" in guide_lower

    def test_has_max_steps_placeholder(self):
        """Should have {max_steps} placeholder for configuration."""
        assert "{max_steps}" in PLAN_STRUCTURE_GUIDE


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test edge cases in prompt generation."""

    def test_empty_task_context(self):
        """Should handle minimal task context."""
        task = TaskContext()
        prompt = select_prompt(task)
        assert len(prompt) > 0

    def test_no_config_uses_defaults(self, root_task):
        """Should work with no config (uses defaults)."""
        prompt = select_prompt(root_task)
        assert len(prompt) > 0
        assert "Implement User Authentication" in prompt

    def test_empty_project_context(self, root_task):
        """Should handle empty project context."""
        config = PromptConfig(project_context="")
        prompt = build_plan_generation_prompt(root_task, config)
        assert "Project Context" not in prompt

    def test_empty_description(self):
        """Should handle tasks with no description."""
        task = TaskContext(
            task_id="task",
            title="Do Something",
            description="",
            is_plan_subtask=False,
        )
        prompt = build_plan_generation_prompt(task)
        assert "Do Something" in prompt

    def test_depth_zero_plan_subtask(self):
        """Edge case: plan subtask at depth 0 (shouldn't happen but handle it)."""
        task = TaskContext(
            task_id="weird-task",
            title="Odd Task",
            is_plan_subtask=True,
            plan_depth=0,
        )
        config = PromptConfig(max_plan_depth=2)
        prompt = select_prompt(task, config)
        # Should get controlled splitting (since depth < max)
        assert "Optional Sub-Planning" in prompt

    def test_very_high_depth(self):
        """Should handle very high depth values."""
        task = TaskContext(
            task_id="deep-task",
            title="Very Deep Task",
            is_plan_subtask=True,
            plan_depth=100,
        )
        config = PromptConfig(max_plan_depth=2)
        prompt = select_prompt(task, config)
        # Should definitely get execution-only
        assert "Execution Focus" in prompt
