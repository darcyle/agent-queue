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


def test_set_identity(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("test-identity", {"workspace_dir": "/work"})
    system_prompt, tools = builder.build()

    assert "You are a test agent." in system_prompt
    assert "Workspace: /work" in system_prompt
    assert tools == []


def test_add_context_blocks(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.add_context("task", "Fix the login bug.")
    builder.add_context("upstream", "Auth module was completed.")
    system_prompt, _ = builder.build()

    assert "No variables here." in system_prompt
    assert "Fix the login bug." in system_prompt
    assert "Auth module was completed." in system_prompt
    # Context blocks appear in order after identity
    identity_pos = system_prompt.index("No variables here.")
    task_pos = system_prompt.index("Fix the login bug.")
    upstream_pos = system_prompt.index("Auth module was completed.")
    assert identity_pos < task_pos < upstream_pos


def test_set_tools(prompts_dir):
    from src.prompt_builder import PromptBuilder

    tools = [{"name": "create_task", "description": "Create a task"}]
    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.set_core_tools(tools)
    _, returned_tools = builder.build()

    assert returned_tools == tools


def test_empty_layers_omitted(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    # No project context, no rules, no specific context
    system_prompt, _ = builder.build()

    # Should only contain the identity text, no empty sections
    assert system_prompt.strip() == "No variables here."


def test_build_task_prompt(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.add_context("task", "Do the thing.")
    prompt = builder.build_task_prompt()

    assert isinstance(prompt, str)
    assert "No variables here." in prompt
    assert "Do the thing." in prompt


def test_add_context_section(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.add_context_section("system_info", {"workspace": "/home", "branch": "main"})
    prompt = builder.build_task_prompt()

    assert "System Info" in prompt
    assert "**workspace:** /home" in prompt
    assert "**branch:** main" in prompt


@pytest.fixture
def task_prompts_dir(tmp_path):
    """Create temp prompts dir with task-related templates."""
    (tmp_path / "plan_structure_guide.md").write_text(textwrap.dedent("""\
        ---
        name: plan-structure-guide
        category: task
        variables:
          - name: max_steps
            required: true
        ---
        Write a plan with at most {{max_steps}} steps.
    """))

    (tmp_path / "controlled_splitting.md").write_text(textwrap.dedent("""\
        ---
        name: controlled-splitting
        category: task
        variables:
          - name: current_depth
            required: true
          - name: max_depth
            required: true
        ---
        You are at depth {{current_depth}} of {{max_depth}}. You may split further.
    """))

    (tmp_path / "execution_focus.md").write_text(textwrap.dedent("""\
        ---
        name: execution-focus
        category: task
        ---
        Execute directly. Do not create plans.
    """))

    return tmp_path


def test_depth_zero_gets_plan_guide(task_prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=task_prompts_dir)
    builder.add_context_section("task_depth", {"depth": 0, "max_depth": 2})
    prompt = builder.build_task_prompt()

    assert "Write a plan with at most" in prompt
    assert "Execute directly" not in prompt


def test_intermediate_depth_gets_controlled_splitting(task_prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=task_prompts_dir)
    builder.add_context_section("task_depth", {"depth": 1, "max_depth": 2})
    prompt = builder.build_task_prompt()

    assert "You are at depth 1 of 2" in prompt
    assert "Execute directly" not in prompt


def test_max_depth_gets_execution_focus(task_prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=task_prompts_dir)
    builder.add_context_section("task_depth", {"depth": 2, "max_depth": 2})
    prompt = builder.build_task_prompt()

    assert "Execute directly" in prompt
    assert "Write a plan" not in prompt


def test_build_is_idempotent(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.add_context("task", "Something.")
    r1 = builder.build()
    r2 = builder.build()
    assert r1 == r2
