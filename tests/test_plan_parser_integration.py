"""Tests for plan parser integration helpers and quality validation.

These tests verify the enhanced plan_parser.py features:
  - PlanQualityReport generation
  - validate_plan_quality() function
  - parse_and_generate_steps() orchestrator integration
  - Safety cap enforcement across all parser paths
"""

import pytest

from src.plan_parser import (
    PlanQualityReport,
    parse_and_generate_steps,
    validate_plan_quality,
)


# ---------------------------------------------------------------------------
# Test fixtures: Document content
# ---------------------------------------------------------------------------

CLEAN_IMPLEMENTATION_PLAN = """\
# Implementation Plan: Add User Authentication

## Step 1: Set Up Database Schema

Create the users table with email, password_hash, and created_at columns.

**Estimated effort:** ~1 hour

## Step 2: Implement Registration Endpoint

Create POST /api/register endpoint with email validation.

**Estimated effort:** ~2 hours

## Step 3: Implement Login Endpoint

Create POST /api/login endpoint with JWT token generation.

**Estimated effort:** ~2 hours

## Step 4: Add Authentication Middleware

Create middleware that validates JWT tokens on protected routes.

**Estimated effort:** ~1 hour

## Step 5: Write Integration Tests

Add tests for registration, login, and protected route access.

**Estimated effort:** ~2 hours
"""

DESIGN_DOCUMENT = """\
# Discord Embedded Responses Design Document

## Executive Summary

This document describes a redesign of Discord message formatting.

## Current Architecture

The project uses discord.py and has inconsistent formatting.

## Design Principles

1. Single source of truth
2. Automatic safety
3. Consistent branding

## Color Palette

| Context | Color | Hex |
|---------|-------|-----|
| Success | Green | #2ecc71 |
| Error | Red | #e74c3c |

## Trade-offs

We chose embeds over plain text for richer formatting.

## Risk Assessment

| Risk | Impact |
|------|--------|
| 6K char limit | Medium |

## Future Enhancements

- Paginated embeds
- Interactive components

## Appendix: Discord API Limits

Title: 256 chars, Description: 4096 chars.
"""

MIXED_DOCUMENT = """\
# Feature: Payment Processing

## Overview

This feature adds payment processing via Stripe.

## Architecture

The system uses a service-oriented architecture.

## Phase 1: Create Payment Service

Implement `src/services/payment.py` with Stripe integration.

**Estimated effort:** ~3 hours

## Phase 2: Add Payment Endpoints

Create POST /api/pay and GET /api/payments/:id endpoints.

**Estimated effort:** ~2 hours

## Design Decisions

We chose Stripe over PayPal for better API docs.

## Phase 3: Write Tests

Add tests for the payment service and endpoints.

**Estimated effort:** ~2 hours

## File Change Summary

| File | Changes |
|------|---------|
| src/services/payment.py | New |
"""

EMPTY_DOCUMENT = ""

NO_HEADINGS_DOCUMENT = "Just some text without any structure at all."


# ---------------------------------------------------------------------------
# Tests: Plan quality validation
# ---------------------------------------------------------------------------

class TestValidatePlanQuality:
    """Test the plan quality validation function."""

    def test_clean_plan_high_quality(self):
        """A well-structured implementation plan should score high."""
        report = validate_plan_quality(CLEAN_IMPLEMENTATION_PLAN)
        assert report.is_implementation_plan is True
        assert report.is_design_doc is False
        assert report.quality_score >= 0.5
        assert report.actionable_sections == 5
        assert report.is_suitable_for_splitting is True

    def test_design_doc_detected(self):
        """A design document should be detected as such."""
        report = validate_plan_quality(DESIGN_DOCUMENT)
        assert report.is_design_doc is True
        assert report.quality_score < 0.5
        assert report.actionable_sections <= 2

    def test_mixed_doc_classified(self):
        """A mixed document should still be suitable for splitting."""
        report = validate_plan_quality(MIXED_DOCUMENT)
        assert report.actionable_sections >= 3
        assert report.is_suitable_for_splitting is True

    def test_empty_doc_not_suitable(self):
        """Empty document should not be suitable for splitting."""
        report = validate_plan_quality(EMPTY_DOCUMENT)
        assert report.is_suitable_for_splitting is False
        assert report.actionable_sections == 0
        assert report.total_sections == 0

    def test_no_headings_not_suitable(self):
        """Document without headings should not be suitable."""
        report = validate_plan_quality(NO_HEADINGS_DOCUMENT)
        assert report.is_suitable_for_splitting is False
        assert report.actionable_sections == 0

    def test_actionable_ratio_correct(self):
        """Actionable ratio should be calculated correctly."""
        report = validate_plan_quality(CLEAN_IMPLEMENTATION_PLAN)
        expected_ratio = report.actionable_sections / report.total_sections
        assert abs(report.actionable_ratio - round(expected_ratio, 2)) < 0.02

    def test_warnings_for_design_doc(self):
        """Design documents should produce warnings."""
        report = validate_plan_quality(DESIGN_DOCUMENT)
        assert len(report.warnings) > 0

    def test_recommendation_provided(self):
        """All reports should include a recommendation."""
        report = validate_plan_quality(CLEAN_IMPLEMENTATION_PLAN)
        assert len(report.recommendation) > 0

        report2 = validate_plan_quality(DESIGN_DOCUMENT)
        assert len(report2.recommendation) > 0

    def test_high_step_count_warning(self):
        """Plans with many steps should produce a warning."""
        # Create a plan with 20 steps
        lines = ["# Big Plan\n"]
        for i in range(20):
            lines.append(
                f"## Step {i+1}: Task {i+1}\n\n"
                f"Implement task {i+1}.\n\n"
                f"**Estimated effort:** ~1 hour\n"
            )
        doc = "\n".join(lines)
        report = validate_plan_quality(doc)
        assert any("step count" in w.lower() or "High" in w for w in report.warnings)


class TestPlanQualityReport:
    """Test the PlanQualityReport dataclass."""

    def test_is_suitable_requires_implementation_plan(self):
        """is_suitable_for_splitting requires is_implementation_plan=True."""
        report = PlanQualityReport(
            is_design_doc=True,
            is_implementation_plan=False,
            quality_score=0.5,
            total_sections=10,
            actionable_sections=5,
            filtered_sections=5,
            actionable_ratio=0.5,
            warnings=[],
            recommendation="Test",
        )
        assert report.is_suitable_for_splitting is False

    def test_is_suitable_requires_minimum_quality(self):
        """is_suitable_for_splitting requires quality_score >= 0.3."""
        report = PlanQualityReport(
            is_design_doc=False,
            is_implementation_plan=True,
            quality_score=0.1,
            total_sections=10,
            actionable_sections=1,
            filtered_sections=9,
            actionable_ratio=0.1,
            warnings=[],
            recommendation="Test",
        )
        assert report.is_suitable_for_splitting is False

    def test_is_suitable_requires_actionable_sections(self):
        """is_suitable_for_splitting requires at least 1 actionable section."""
        report = PlanQualityReport(
            is_design_doc=False,
            is_implementation_plan=True,
            quality_score=0.5,
            total_sections=10,
            actionable_sections=0,
            filtered_sections=10,
            actionable_ratio=0.0,
            warnings=[],
            recommendation="Test",
        )
        assert report.is_suitable_for_splitting is False


# ---------------------------------------------------------------------------
# Tests: parse_and_generate_steps (orchestrator integration)
# ---------------------------------------------------------------------------

class TestParseAndGenerateSteps:
    """Test the orchestrator integration helper."""

    def test_generates_steps_from_clean_plan(self):
        """Should generate steps from a well-structured plan."""
        steps, quality = parse_and_generate_steps(CLEAN_IMPLEMENTATION_PLAN)
        assert len(steps) == 5
        assert quality.is_implementation_plan is True

    def test_steps_have_title_and_description(self):
        """Each step should have 'title' and 'description' keys."""
        steps, _ = parse_and_generate_steps(CLEAN_IMPLEMENTATION_PLAN)
        for step in steps:
            assert "title" in step
            assert "description" in step
            assert len(step["title"]) > 0

    def test_enforces_max_steps(self):
        """Should enforce max_steps regardless of content."""
        steps, _ = parse_and_generate_steps(
            CLEAN_IMPLEMENTATION_PLAN, max_steps=3
        )
        assert len(steps) <= 3

    def test_filters_design_doc_when_enforcing_quality(self):
        """With quality enforcement, design docs should produce no steps."""
        steps, quality = parse_and_generate_steps(
            DESIGN_DOCUMENT,
            enforce_quality=True,
            min_quality_score=0.5,
        )
        # If the quality is too low, no steps should be generated
        if quality.quality_score < 0.5:
            assert len(steps) == 0

    def test_allows_design_doc_without_quality_enforcement(self):
        """Without quality enforcement, any document can produce steps."""
        steps, quality = parse_and_generate_steps(
            MIXED_DOCUMENT,
            enforce_quality=False,
        )
        assert len(steps) >= 3

    def test_returns_quality_report(self):
        """Should always return a quality report."""
        _, quality = parse_and_generate_steps(CLEAN_IMPLEMENTATION_PLAN)
        assert isinstance(quality, PlanQualityReport)
        assert quality.total_sections > 0

    def test_empty_document_returns_no_steps(self):
        """Empty document should return no steps."""
        steps, quality = parse_and_generate_steps(EMPTY_DOCUMENT)
        assert len(steps) == 0

    def test_post_filter_applied(self):
        """The post-filter should remove any remaining non-actionable steps."""
        # Create content where some non-actionable headings might slip through
        content = """\
# Plan

## Overview

Background info about the project.

## Step 1: Implement Feature

Create the feature module.

**Estimated effort:** ~2 hours

## File Change Summary

| File | Changes |
|------|---------|
| src/feature.py | New |
"""
        steps, _ = parse_and_generate_steps(content)
        titles = [s["title"] for s in steps]
        assert "Overview" not in titles
        assert "File Change Summary" not in titles

    def test_safety_cap_prevents_excessive_steps(self):
        """Safety cap should prevent more than max_steps regardless of parser."""
        # Create a massive plan
        lines = ["# Massive Plan\n"]
        for i in range(50):
            lines.append(
                f"## Step {i+1}: Implement Feature {i+1}\n\n"
                f"Create feature {i+1}.\n\n"
            )
        doc = "\n".join(lines)

        steps, _ = parse_and_generate_steps(doc, max_steps=10)
        assert len(steps) <= 10


# ---------------------------------------------------------------------------
# Tests: Integration across layers
# ---------------------------------------------------------------------------

class TestCrossLayerIntegration:
    """Test that the parsing layer works correctly with discovery scenarios."""

    def test_plan_with_implementation_container(self):
        """Plans with implementation containers should parse cleanly."""
        content = """\
# Feature Plan

## Background

Some context about the feature.

## Implementation Plan

### Phase 1: Create Service

Implement the service layer.

**Estimated effort:** ~3 hours

### Phase 2: Add API Endpoints

Create REST endpoints.

**Estimated effort:** ~2 hours

### Phase 3: Write Tests

Add integration tests.

**Estimated effort:** ~2 hours

## Appendix

Reference material.
"""
        steps, quality = parse_and_generate_steps(content)
        assert len(steps) == 3
        assert quality.is_implementation_plan is True

    def test_agent_output_style_plan(self):
        """Plans that look like agent output should parse correctly."""
        content = """\
# Implementation Plan for Task: Add Caching

I've analyzed the codebase and here's my implementation plan:

## Step 1: Add Redis Client Configuration

Create `src/cache/config.py` with Redis connection setup.

Files to modify:
- `src/cache/config.py` (new)
- `requirements.txt`

**Estimated effort:** ~1 hour

## Step 2: Implement Cache Decorator

Create a decorator that caches function results in Redis.

Files to modify:
- `src/cache/decorator.py` (new)

**Estimated effort:** ~2 hours

## Step 3: Apply Caching to Hot Paths

Add the cache decorator to the most frequently called endpoints.

Files to modify:
- `src/api/users.py`
- `src/api/products.py`

**Estimated effort:** ~1 hour

## Step 4: Add Cache Invalidation

Implement cache invalidation on data mutations.

Files to modify:
- `src/cache/invalidation.py` (new)
- `src/api/mutations.py`

**Estimated effort:** ~2 hours
"""
        steps, quality = parse_and_generate_steps(content)
        assert len(steps) == 4
        assert quality.is_suitable_for_splitting is True
        assert all("Step" in s["title"] for s in steps)
