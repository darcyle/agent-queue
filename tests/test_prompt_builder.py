"""Tests for PromptBuilder — template loading and rendering."""

import textwrap

import pytest


@pytest.fixture
def prompts_dir(tmp_path):
    """Create a temp prompts directory with test templates."""
    tpl = tmp_path / "test_identity.md"
    tpl.write_text(textwrap.dedent("""\
        ---
        name: test-identity
        category: system
        variables:
          - name: workspace_dir
            required: true
        ---
        You are a test agent.
        Workspace: {{workspace_dir}}
    """))

    no_vars = tmp_path / "simple.md"
    no_vars.write_text(textwrap.dedent("""\
        ---
        name: simple
        category: task
        ---
        No variables here.
    """))

    return tmp_path


def test_load_template_with_variables(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("test-identity", {"workspace_dir": "/home/user"})
    assert "You are a test agent." in result
    assert "Workspace: /home/user" in result


def test_load_template_no_variables(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("simple")
    assert "No variables here." in result


def test_load_missing_template(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    result = builder.render_template("nonexistent")
    assert result is None


def test_get_raw_template(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    raw = builder.get_template("test-identity")
    assert "{{workspace_dir}}" in raw


def test_template_caching(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    r1 = builder.render_template("simple")
    r2 = builder.render_template("simple")
    assert r1 == r2
