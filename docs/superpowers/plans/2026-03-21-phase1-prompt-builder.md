# Phase 1: PromptBuilder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a unified `PromptBuilder` that replaces all 5+ scattered prompt assembly paths with a single layered system. Migrations preserve functional equivalence — the same information is assembled in the same order, though minor whitespace/separator differences are acceptable since prompts are consumed by LLMs, not parsed structurally.

**Architecture:** `PromptBuilder` uses a 5-layer pipeline (identity → project context → rules → specific context → tools) to assemble prompts. It absorbs `prompt_registry.py`, `prompt_manager.py`, and `agent_prompting.py`. All existing call sites are migrated one at a time with parity tests ensuring no behavioral change.

**Tech Stack:** Python 3.12+, pytest with pytest-asyncio, dataclasses, YAML parsing, regex template rendering

**Spec:** `docs/superpowers/specs/2026-03-21-supervisor-refactor-design.md` (Section 5)

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `src/prompt_builder.py` | Unified prompt assembly — 5-layer pipeline, template loading, variable substitution |
| Create | `tests/test_prompt_builder.py` | All PromptBuilder unit tests |
| Create | `specs/prompt-builder.md` | Behavioral spec for PromptBuilder |
| Modify | `src/adapters/claude.py` | Replace `_build_prompt()` with PromptBuilder call |
| Modify | `src/orchestrator.py` | Replace ~200 lines of context assembly with PromptBuilder calls |
| Modify | `src/chat_agent.py` | Replace `_build_system_prompt()` with PromptBuilder call |
| Modify | `src/hooks.py` | Replace `_build_hook_context()` + `_render_prompt()` with PromptBuilder call |
| Keep | `src/prompts/*.md` | Template files remain as source content |
| Keep | `src/agent_prompting.py` | NOT removed in Phase 1 — its `TaskContext` and `detect_design_doc_output` are still needed. Prompt builders deprecated but kept until Phase 6. |
| Keep | `src/prompt_registry.py` | NOT removed in Phase 1 — deprecated but kept as fallback until all call sites migrated. Removed in Phase 6. |
| Keep | `src/prompt_manager.py` | NOT removed in Phase 1 — internal logic absorbed into PromptBuilder. File removed in Phase 6. |

---

### Task 1: Write the PromptBuilder spec

**Files:**
- Create: `specs/prompt-builder.md`

- [ ] **Step 1: Write the spec**

Write the behavioral specification for PromptBuilder. This spec describes what the system does, not how. It covers:

```markdown
# Prompt Builder

## Purpose

Single entry point for all prompt assembly in the system. Replaces scattered string concatenation across orchestrator, adapters, chat agent, hooks, and agent_prompting.

## Concepts

### Five Layers

Every prompt is assembled from up to 5 ordered layers:

1. **Identity** — Who is the LLM acting as? Loaded from a prompt template file in `src/prompts/`.
   - `supervisor` — the Discord-facing Supervisor (from `chat_agent_system.md`)
   - `task-agent` — a Claude Code agent executing a task
   - `hook-executor` — the Supervisor reasoning about a hook result (from `hook_context.md`)

2. **Project Context** — What project is this for? Pulled from the memory system:
   - Project profile (from `profile.md`)
   - Project documentation (CLAUDE.md, README.md)
   - Falls back to empty string if memory unavailable

3. **Relevant Rules** — What rules apply to this action? Semantic search against the current query, or all project rules if memsearch is unavailable. (Populated in Phase 2 — returns empty in Phase 1.)

4. **Specific Context** — What is the LLM doing right now? Arbitrary named context blocks:
   - `task` — task description
   - `upstream` — completed dependency summaries
   - `task_depth` — depth-aware execution rules (plan generation, controlled splitting, execution focus)
   - `hook` — hook trigger data and context step results
   - `system_metadata` — workspace path, project name, branch
   - `role_instructions` — agent profile system_prompt_suffix

5. **Tools** — What tools are available? A list of JSON Schema tool definitions.

### Template Loading

PromptBuilder loads templates from `src/prompts/*.md` files with YAML frontmatter. It parses frontmatter for metadata (name, variables, category) and renders the body with `{{variable}}` substitution. This absorbs the functionality of `prompt_registry.py` and `prompt_manager.py`.

Templates are cached after first load. `reload()` forces re-read from disk.

### Output

`build()` returns a tuple of `(system_prompt: str, tools: list[dict])`.

- `system_prompt` is the concatenation of all non-empty layers in order, separated by `\n\n---\n\n`
- `tools` is the list of tool definitions set via `set_core_tools()` or `set_tools()`

For task execution prompts (identity="task-agent"), the output is a single prompt string (no separate system prompt), since it goes to the Claude Code CLI as a flat prompt. `build_task_prompt()` returns `str` instead.

## Interfaces

### Constructor

`PromptBuilder(project_id: str | None = None, memory_manager: Any | None = None)`

- `project_id` scopes project context and rule loading
- `memory_manager` is optional; if None, layers 2 and 3 are empty

### Methods

- `set_identity(name: str, variables: dict | None = None)` — Load identity template, render with variables
- `load_project_context()` — async, pulls from memory_manager if available
- `load_relevant_rules(query: str)` — async, semantic search or fallback. Returns empty in Phase 1.
- `add_context(name: str, content: str)` — Add a named context block to layer 4
- `add_context_section(name: str, data: dict)` — Add structured context rendered as markdown. When `name="task_depth"`, dispatches to depth-aware template selection (see Task 4)
- `set_core_tools(tools: list[dict])` — Set the tool definitions for layer 5
- `set_tools(tools: list[dict])` — Alias for set_core_tools
- `build() -> tuple[str, list[dict]]` — Assemble and return (system_prompt, tools)
- `build_task_prompt() -> str` — Assemble and return flat prompt string for task execution
- `get_template(name: str) -> str | None` — Load and return raw template body (for backward compat)
- `render_template(name: str, variables: dict | None = None) -> str` — Load, render, return template

### Template Rendering

- Loads `.md` files from `src/prompts/` directory
- Parses YAML frontmatter (between `---` delimiters)
- Substitutes `{{variable_name}}` with provided values
- Missing required variables raise `ValueError` in strict mode, use empty string in non-strict
- Templates are cached per PromptBuilder instance

## Invariants

- Layer order is always 1 → 2 → 3 → 4 → 5
- Empty layers are omitted (no blank sections in output)
- Multiple `add_context()` calls append in call order within layer 4
- `build()` can be called multiple times (idempotent)
- Template loading never raises on missing files — returns None
- PromptBuilder is not thread-safe (single-use per prompt assembly)
```

- [ ] **Step 2: Commit the spec**

```bash
git add specs/prompt-builder.md
git commit -m "Add PromptBuilder behavioral spec for Phase 1 of supervisor refactor"
```

---

### Task 2: Core PromptBuilder — template loading and rendering

**Files:**
- Create: `tests/test_prompt_builder.py`
- Create: `src/prompt_builder.py`

- [ ] **Step 1: Write failing tests for template loading**

```python
"""Tests for PromptBuilder — template loading and rendering."""

import textwrap
import pytest
from pathlib import Path


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_prompt_builder.py -v`
Expected: FAIL — `src.prompt_builder` does not exist yet

- [ ] **Step 3: Implement core PromptBuilder with template loading**

```python
"""Unified prompt assembly pipeline.

Replaces prompt_registry.py, prompt_manager.py, and scattered string
concatenation across orchestrator, adapters, chat_agent, and hooks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass
class _TemplateCache:
    """Parsed template with metadata and body."""
    name: str
    body: str
    variables: list[dict] = field(default_factory=list)
    category: str = ""
    metadata: dict = field(default_factory=dict)


class PromptBuilder:
    """Unified 5-layer prompt assembly pipeline.

    Layers:
        1. Identity — who is the LLM? (loaded from template)
        2. Project context — what project? (from memory)
        3. Relevant rules — what rules apply? (Phase 2)
        4. Specific context — named context blocks
        5. Tools — JSON Schema tool definitions
    """

    def __init__(
        self,
        project_id: str | None = None,
        memory_manager: Any | None = None,
        prompts_dir: Path | str | None = None,
    ):
        self._project_id = project_id
        self._memory_manager = memory_manager
        self._prompts_dir = Path(prompts_dir) if prompts_dir else _DEFAULT_PROMPTS_DIR

        # Layer state
        self._identity: str = ""
        self._project_context: str = ""
        self._rules: str = ""
        self._context_blocks: list[tuple[str, str]] = []
        self._tools: list[dict] = []

        # Template cache
        self._templates: dict[str, _TemplateCache] = {}
        self._templates_loaded = False

    # ------------------------------------------------------------------
    # Template loading
    # ------------------------------------------------------------------

    def _ensure_templates_loaded(self) -> None:
        if self._templates_loaded:
            return
        self._templates_loaded = True
        if not self._prompts_dir.is_dir():
            return
        for md_file in self._prompts_dir.glob("*.md"):
            tpl = self._parse_template_file(md_file)
            if tpl:
                self._templates[tpl.name] = tpl

    def _parse_template_file(self, path: Path) -> _TemplateCache | None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None

        frontmatter, body = self._split_frontmatter(content)
        name = frontmatter.get("name", path.stem)
        variables = frontmatter.get("variables", [])
        category = frontmatter.get("category", "")

        return _TemplateCache(
            name=name,
            body=body.strip(),
            variables=variables if isinstance(variables, list) else [],
            category=category,
            metadata=frontmatter,
        )

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[dict, str]:
        if not content.startswith("---"):
            return {}, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return {}, content
        return meta, parts[2]

    def _render_variables(self, body: str, variables: dict[str, str] | None) -> str:
        if not variables:
            return body

        def replacer(match: re.Match) -> str:
            var_name = match.group(1).strip()
            return str(variables.get(var_name, match.group(0)))

        return re.sub(r"\{\{(.+?)\}\}", replacer, body)

    # ------------------------------------------------------------------
    # Public template API
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """Force re-read templates from disk."""
        self._templates.clear()
        self._templates_loaded = False

    def get_template(self, name: str) -> str | None:
        """Return raw template body with {{placeholders}} intact."""
        self._ensure_templates_loaded()
        tpl = self._templates.get(name)
        return tpl.body if tpl else None

    def render_template(
        self, name: str, variables: dict[str, str] | None = None
    ) -> str | None:
        """Load and render a template with variable substitution."""
        self._ensure_templates_loaded()
        tpl = self._templates.get(name)
        if not tpl:
            return None
        return self._render_variables(tpl.body, variables)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_prompt_builder.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/prompt_builder.py tests/test_prompt_builder.py
git commit -m "Add PromptBuilder core with template loading and rendering"
```

---

### Task 3: PromptBuilder — 5-layer assembly pipeline

**Files:**
- Modify: `tests/test_prompt_builder.py`
- Modify: `src/prompt_builder.py`

- [ ] **Step 1: Write failing tests for layer assembly**

Add to `tests/test_prompt_builder.py`:

```python
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


def test_build_is_idempotent(prompts_dir):
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.set_identity("simple")
    builder.add_context("task", "Something.")
    r1 = builder.build()
    r2 = builder.build()
    assert r1 == r2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_prompt_builder.py -v`
Expected: FAIL — `set_identity`, `build`, `add_context`, etc. not implemented

- [ ] **Step 3: Implement the 5-layer assembly methods**

Add to the `PromptBuilder` class in `src/prompt_builder.py`:

```python
    # ------------------------------------------------------------------
    # Layer setters
    # ------------------------------------------------------------------

    def set_identity(self, name: str, variables: dict[str, str] | None = None) -> None:
        """Layer 1: Set the identity from a prompt template."""
        rendered = self.render_template(name, variables)
        self._identity = rendered or ""

    async def load_project_context(self) -> None:
        """Layer 2: Load project context from memory system."""
        if not self._memory_manager or not self._project_id:
            return
        try:
            ctx = await self._memory_manager.build_context(
                self._project_id, task=None, workspace_path=""
            )
            if ctx and not ctx.is_empty():
                self._project_context = ctx.to_context_block()
        except Exception:
            pass  # graceful degradation

    async def load_relevant_rules(self, query: str) -> None:
        """Layer 3: Load relevant rules. Stub for Phase 1 — always empty."""
        # Implemented in Phase 2
        self._rules = ""

    def add_context(self, name: str, content: str) -> None:
        """Layer 4: Add a named context block."""
        if content and content.strip():
            self._context_blocks.append((name, content))

    def add_context_section(self, name: str, data: dict) -> None:
        """Layer 4: Add structured context rendered as markdown."""
        if not data:
            return
        lines = [f"## {name.replace('_', ' ').title()}"]
        for key, value in data.items():
            lines.append(f"- **{key}:** {value}")
        self._context_blocks.append((name, "\n".join(lines)))

    def set_core_tools(self, tools: list[dict]) -> None:
        """Layer 5: Set tool definitions."""
        self._tools = list(tools)

    def set_tools(self, tools: list[dict]) -> None:
        """Alias for set_core_tools."""
        self.set_core_tools(tools)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> tuple[str, list[dict]]:
        """Assemble all layers into (system_prompt, tools)."""
        sections: list[str] = []

        if self._identity:
            sections.append(self._identity)
        if self._project_context:
            sections.append(self._project_context)
        if self._rules:
            sections.append(self._rules)
        for _name, content in self._context_blocks:
            sections.append(content)

        system_prompt = "\n\n---\n\n".join(sections) if sections else ""
        return system_prompt, list(self._tools)

    def build_task_prompt(self) -> str:
        """Assemble into a flat prompt string (for task execution)."""
        prompt, _ = self.build()
        return prompt
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_prompt_builder.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/prompt_builder.py tests/test_prompt_builder.py
git commit -m "Add 5-layer assembly pipeline to PromptBuilder"
```

---

### Task 4: PromptBuilder — depth-aware task prompting

**Files:**
- Modify: `tests/test_prompt_builder.py`
- Modify: `src/prompt_builder.py`

This replicates the depth-aware logic from `agent_prompting.py` inside PromptBuilder so it can be used when building task-agent prompts.

- [ ] **Step 1: Write failing tests for depth-aware context**

Add to `tests/test_prompt_builder.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_prompt_builder.py::test_depth_zero_gets_plan_guide -v`
Expected: FAIL — `add_depth_context` not defined

- [ ] **Step 3: Implement depth-aware context via `add_context_section`**

Modify `add_context_section` in `src/prompt_builder.py` to detect `task_depth` and dispatch:

```python
    def add_context_section(self, name: str, data: dict) -> None:
        """Layer 4: Add structured context rendered as markdown.

        Special handling for name="task_depth":
          - depth 0 (root): plan structure guide
          - intermediate: controlled splitting
          - max_depth: execution focus only
        """
        if not data:
            return

        if name == "task_depth":
            self._add_depth_context(data)
            return

        lines = [f"## {name.replace('_', ' ').title()}"]
        for key, value in data.items():
            lines.append(f"- **{key}:** {value}")
        self._context_blocks.append((name, "\n".join(lines)))

    def _add_depth_context(self, data: dict) -> None:
        """Dispatch to appropriate depth-aware template."""
        depth = int(data.get("depth", 0))
        max_depth = int(data.get("max_depth", 2))
        max_steps = int(data.get("max_steps", 20))

        if depth >= max_depth:
            content = self.render_template("execution-focus") or ""
        elif depth == 0:
            content = self.render_template(
                "plan-structure-guide", {"max_steps": str(max_steps)}
            ) or ""
        else:
            content = self.render_template(
                "controlled-splitting",
                {"current_depth": str(depth), "max_depth": str(max_depth)},
            ) or ""

        if content:
            self.add_context("execution_rules", content)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_prompt_builder.py -v -k "depth"`
Expected: All 3 depth tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/prompt_builder.py tests/test_prompt_builder.py
git commit -m "Add depth-aware task prompting to PromptBuilder"
```

---

### Task 5: Migrate adapter — replace `_build_prompt()`

**Files:**
- Modify: `tests/test_prompt_builder.py`
- Modify: `src/adapters/claude.py`

The adapter's `_build_prompt()` (lines 513-533) is the simplest call site. It reads `TaskContext.description`, `acceptance_criteria`, `test_commands`, and `attached_context` and joins them.

- [ ] **Step 1: Write a parity test**

Add to `tests/test_prompt_builder.py`:

```python
def test_build_task_prompt_matches_adapter_format(prompts_dir):
    """Verify PromptBuilder produces the same output as adapter._build_prompt()."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)
    builder.add_context("description", "Fix the login bug in auth.py.")
    builder.add_context(
        "acceptance_criteria",
        "## Acceptance Criteria\n- Login works with valid credentials\n- Invalid creds show error",
    )
    builder.add_context(
        "test_commands",
        "## Test Commands\n- `pytest tests/test_auth.py`",
    )
    builder.add_context(
        "additional_context",
        "## Additional Context\nProject uses JWT tokens.",
    )

    prompt = builder.build_task_prompt()

    assert "Fix the login bug" in prompt
    assert "Acceptance Criteria" in prompt
    assert "pytest tests/test_auth.py" in prompt
    assert "Project uses JWT tokens" in prompt
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_prompt_builder.py::test_build_task_prompt_matches_adapter_format -v`
Expected: PASS (this uses existing PromptBuilder methods)

- [ ] **Step 3: Modify adapter to use PromptBuilder**

In `src/adapters/claude.py`, modify `_build_prompt()` (around line 513):

Replace the existing method body with:

```python
def _build_prompt(self) -> str:
    """Build the final prompt from TaskContext using PromptBuilder."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder()

    # Main description (already assembled by orchestrator)
    if self._task.description:
        builder.add_context("description", self._task.description)

    # Optional sections
    if self._task.acceptance_criteria:
        criteria = "\n".join(f"- {c}" for c in self._task.acceptance_criteria)
        builder.add_context("acceptance_criteria", f"## Acceptance Criteria\n{criteria}")

    if self._task.test_commands:
        cmds = "\n".join(f"- `{c}`" for c in self._task.test_commands)
        builder.add_context("test_commands", f"## Test Commands\n{cmds}")

    if self._task.attached_context:
        combined = "\n".join(self._task.attached_context)
        builder.add_context("additional_context", f"## Additional Context\n{combined}")

    return builder.build_task_prompt()
```

- [ ] **Step 4: Run existing adapter tests to verify no regression**

Run: `pytest tests/test_adapters.py -v`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/adapters/claude.py tests/test_prompt_builder.py
git commit -m "Migrate adapter._build_prompt() to use PromptBuilder"
```

---

### Task 6: Migrate chat agent — replace `_build_system_prompt()`

**Files:**
- Modify: `tests/test_prompt_builder.py`
- Modify: `src/chat_agent.py`

- [ ] **Step 1: Write parity test for supervisor identity prompt**

Add to `tests/test_prompt_builder.py`:

```python
def test_supervisor_identity_with_active_project():
    """Verify supervisor identity renders with project context appended."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=_DEFAULT_PROMPTS_DIR)
    builder.set_identity("chat-agent-system", {"workspace_dir": "/home/user/.agent-queue"})
    builder.add_context(
        "active_project",
        'ACTIVE PROJECT: `my-game`. Use this as the default project_id for all tools '
        'unless the user explicitly specifies a different project.',
    )
    system_prompt, _ = builder.build()

    assert "/home/user/.agent-queue" in system_prompt
    assert "ACTIVE PROJECT: `my-game`" in system_prompt


def test_supervisor_identity_without_project():
    """Verify supervisor identity renders without active project."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=_DEFAULT_PROMPTS_DIR)
    builder.set_identity("chat-agent-system", {"workspace_dir": "/home/user/.agent-queue"})
    system_prompt, _ = builder.build()

    assert "/home/user/.agent-queue" in system_prompt
    assert "ACTIVE PROJECT" not in system_prompt
```

Note: These tests use the real `src/prompts/` directory. Add this import near the top of the test file:

```python
from pathlib import Path
_DEFAULT_PROMPTS_DIR = Path(__file__).parent.parent / "src" / "prompts"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_prompt_builder.py -v -k "supervisor_identity"`
Expected: PASS

- [ ] **Step 3: Modify chat_agent.py to use PromptBuilder**

> **Note:** The identity name `"chat-agent-system"` is used here for Phase 1 parity with the existing template filename. In Phase 4 (Supervisor Identity), this template will be rewritten and renamed to `"supervisor"` when the ChatAgent becomes the Supervisor.

In `src/chat_agent.py`, modify `_build_system_prompt()` (around line 2388):

```python
def _build_system_prompt(self) -> str:
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder()
    builder.set_identity(
        "chat-agent-system",
        {"workspace_dir": self.config.workspace_dir},
    )
    if self._active_project_id:
        builder.add_context(
            "active_project",
            f"ACTIVE PROJECT: `{self._active_project_id}`. "
            f"Use this as the default project_id for all tools unless the user "
            f"explicitly specifies a different project. When creating tasks, "
            f"listing notes, or any project-scoped operation, use this project.",
        )
    system_prompt, _ = builder.build()
    return system_prompt
```

- [ ] **Step 4: Run existing chat agent tests to verify no regression**

Run: `pytest tests/ -v -k "chat" --ignore=tests/chat_eval`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/chat_agent.py tests/test_prompt_builder.py
git commit -m "Migrate chat_agent._build_system_prompt() to use PromptBuilder"
```

---

### Task 7: Migrate hooks — replace `_build_hook_context()`

**Files:**
- Modify: `tests/test_prompt_builder.py`
- Modify: `src/hooks.py`

The hook engine has two prompt assembly functions: `_build_hook_context()` for the preamble and `_render_prompt()` for template substitution. PromptBuilder already handles template rendering, but hook placeholder substitution (`{{step_N}}`, `{{event}}`) is hook-specific and stays in hooks.py. We migrate only the context preamble assembly.

- [ ] **Step 1: Write parity test for hook context**

Add to `tests/test_prompt_builder.py`:

```python
def test_hook_executor_identity():
    """Verify hook-context template renders with project metadata."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=_DEFAULT_PROMPTS_DIR)
    builder.set_identity("hook-context", {
        "hook_name": "tunnel-monitor",
        "project_id": "my-game",
        "project_name": "My Game Server",
        "workspace_dir": "- **Workspace:** `/home/user/game`\n",
        "repo_url": "",
        "default_branch": "",
        "trigger_reason": "periodic (every 300s)",
        "timing_context": "",
    })
    system_prompt, _ = builder.build()

    assert "tunnel-monitor" in system_prompt
    assert "My Game Server" in system_prompt
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_prompt_builder.py::test_hook_executor_identity -v`
Expected: PASS

- [ ] **Step 3: Modify hooks.py to use PromptBuilder for context preamble**

In `src/hooks.py`, modify `_build_hook_context()` (around line 1277). Replace the `_prompt_registry.render()` call with:

```python
async def _build_hook_context(
    self, hook: Hook, trigger_reason: str, event_data: dict | None = None
) -> str:
    from src.prompt_builder import PromptBuilder

    # ... existing project metadata fetching code stays the same ...

    builder = PromptBuilder()
    builder.set_identity("hook-context", {
        "hook_name": hook.name,
        "project_id": hook.project_id or "",
        "project_name": project_name,
        "workspace_dir": workspace_line,
        "repo_url": repo_line,
        "default_branch": branch_line,
        "trigger_reason": trigger_reason,
        "timing_context": timing_lines,
    })
    result, _ = builder.build()
    return result
```

Keep `_render_prompt()` and `_resolve_placeholder()` unchanged — hook-specific placeholder substitution is not part of PromptBuilder's responsibility.

- [ ] **Step 4: Run existing hook tests to verify no regression**

Run: `pytest tests/test_hooks.py tests/test_hook_events.py -v`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hooks.py tests/test_prompt_builder.py
git commit -m "Migrate hooks._build_hook_context() to use PromptBuilder"
```

---

### Task 8: Migrate orchestrator — replace context assembly in `_execute_task()`

**Files:**
- Modify: `tests/test_prompt_builder.py`
- Modify: `src/orchestrator.py`

This is the largest migration — ~150 lines of string concatenation in `_execute_task()` (lines 3116-3265). The orchestrator builds system metadata, execution rules, upstream dependency context, role instructions, and task description. We replace this with PromptBuilder calls.

> **TaskContext classes:** There are two `TaskContext` classes in the codebase:
> - `models.TaskContext` (line 318) — used by the adapter, has `description`, `acceptance_criteria`, `checkout_path`, etc.
> - `agent_prompting.TaskContext` (line 111) — used by prompt builders, has `plan_depth`, `is_plan_subtask`, `parent_title`, etc.
>
> The orchestrator works with the database `task` object (not either TaskContext). It reads `task.is_plan_subtask` and `task.plan_depth` from the DB row to decide execution rules, then constructs a `models.TaskContext` to pass to the adapter. The `agent_prompting.TaskContext` is only used by `agent_prompting.py`'s prompt builders, which are NOT migrated in Phase 1 (kept for Phase 6).
>
> In this task, `_build_task_context_with_prompt_builder()` reads depth-related fields directly from the DB task object and passes them to `builder.add_context_section("task_depth", {"depth": task.plan_depth, "max_depth": config.max_plan_depth})`.

- [ ] **Step 1: Write parity test for orchestrator context assembly**

Add to `tests/test_prompt_builder.py`:

```python
def test_task_agent_assembly_ordering(prompts_dir):
    """Verify task agent prompt assembles all sections in correct order."""
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(prompts_dir=prompts_dir)

    # Layer 1: identity (use simple for test)
    builder.set_identity("simple")

    # Layer 4: system metadata
    builder.add_context("system_context", (
        "## System Context\n"
        "- Workspace directory: /home/user/project\n"
        "- Project: my-game (id: game-001)\n"
        "- Git branch: feat/login"
    ))

    # Layer 4: execution rules
    builder.add_context("execution_rules", (
        "## Execution Rules\n"
        "- Do not use plan mode\n"
        "- Commit your changes when done"
    ))

    # Layer 4: upstream work
    builder.add_context("upstream_work", (
        "## Completed Upstream Work\n"
        "### Auth Module\n"
        "**Summary:** Implemented JWT tokens.\n"
        "**Files changed:**\n- `src/auth.py`"
    ))

    # Layer 4: role instructions
    builder.add_context("role_instructions", (
        "## Agent Role Instructions\n"
        "You are a backend developer specializing in security."
    ))

    # Layer 4: task description
    builder.add_context("task", (
        "## Task\n"
        "Fix the login endpoint to validate JWT expiration."
    ))

    prompt = builder.build_task_prompt()

    # All sections present in order
    assert "System Context" in prompt
    assert "Execution Rules" in prompt
    assert "Completed Upstream Work" in prompt
    assert "Agent Role Instructions" in prompt
    assert "Fix the login endpoint" in prompt

    # Verify ordering
    sys_pos = prompt.index("System Context")
    rules_pos = prompt.index("Execution Rules")
    upstream_pos = prompt.index("Completed Upstream Work")
    task_pos = prompt.index("Fix the login endpoint")
    assert sys_pos < rules_pos < upstream_pos < task_pos
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_prompt_builder.py::test_task_agent_assembly_ordering -v`
Expected: PASS

- [ ] **Step 3: Create helper function in orchestrator**

In `src/orchestrator.py`, add a new method that uses PromptBuilder to assemble the task context. This replaces the inline string concatenation in `_execute_task()` (around lines 3116-3253):

```python
def _build_task_context_with_prompt_builder(
    self,
    task,
    workspace: str,
    profile,
    upstream_summaries: list[dict],
) -> str:
    """Build the task description using PromptBuilder.

    Replaces the inline string concatenation that was here previously.
    """
    from src.prompt_builder import PromptBuilder

    builder = PromptBuilder(project_id=task.project_id)

    # System metadata
    parts = [f"- Workspace directory: {workspace}"]
    project = self._get_project(task.project_id)
    if project:
        parts.append(f"- Project: {project.name} (id: {project.id})")
    if task.branch_name:
        parts.append(f"- Git branch: {task.branch_name}")
    builder.add_context("system_context", "## System Context\n" + "\n".join(parts))

    # Execution rules — keep existing logic but pass as context block
    # (The actual execution rules text is preserved exactly as-is)
    if getattr(task, "is_plan_subtask", False):
        rules = self._get_subtask_execution_rules()
    else:
        rules = self._get_root_task_execution_rules()
    builder.add_context("execution_rules", rules)

    # Upstream dependency summaries
    if upstream_summaries:
        upstream_parts = ["## Completed Upstream Work"]
        for dep in upstream_summaries:
            upstream_parts.append(f"### {dep.get('title', 'Unnamed')}")
            summary = dep.get("summary", "")[:2000]
            upstream_parts.append(f"**Summary:** {summary}")
            files = dep.get("files_changed", [])
            if files:
                upstream_parts.append("**Files changed:**")
                for f in files:
                    upstream_parts.append(f"  - `{f}`")
        builder.add_context("upstream_work", "\n".join(upstream_parts))

    # Agent role instructions from profile
    if profile and profile.system_prompt_suffix:
        builder.add_context(
            "role_instructions",
            f"## Agent Role Instructions\n{profile.system_prompt_suffix}",
        )

    # Task description
    builder.add_context("task", f"## Task\n{task.description}")

    return builder.build_task_prompt()
```

Then in `_execute_task()`, replace the ~130 lines of string concatenation (lines 3116-3242) with a call to this new method:

```python
full_description = self._build_task_context_with_prompt_builder(
    task, workspace, profile, upstream_summaries
)
```

The existing execution rules text (subtask vs. root task instructions) should be extracted into `_get_subtask_execution_rules()` and `_get_root_task_execution_rules()` helper methods that return the same strings as the current inline code.

- [ ] **Step 4: Run existing orchestrator tests to verify no regression**

Run: `pytest tests/test_orchestrator.py -v`
Expected: All existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator.py tests/test_prompt_builder.py
git commit -m "Migrate orchestrator context assembly to use PromptBuilder"
```

---

### Task 9: Update existing specs

**Files:**
- Modify: `specs/chat-agent.md` — note that `_build_system_prompt()` now uses PromptBuilder
- Modify: `specs/hooks.md` — note that `_build_hook_context()` now uses PromptBuilder
- Modify: `specs/orchestrator.md` — note that task context assembly now uses PromptBuilder

- [ ] **Step 1: Update chat-agent spec**

Add a note to the relevant section in `specs/chat-agent.md`:

```markdown
### System Prompt Assembly

The system prompt is assembled using `PromptBuilder` (see `specs/prompt-builder.md`).
`_build_system_prompt()` uses `PromptBuilder.set_identity("chat-agent-system")` with
the `workspace_dir` variable, and optionally adds an active project context block.
```

- [ ] **Step 2: Update hooks spec**

Add a note to the relevant section in `specs/hooks.md`:

```markdown
### Hook Context Preamble

The hook context preamble is assembled using `PromptBuilder` (see `specs/prompt-builder.md`).
`_build_hook_context()` uses `PromptBuilder.set_identity("hook-context")` with project
metadata variables. Hook-specific placeholder substitution (`{{step_N}}`, `{{event}}`)
remains in the hook engine's `_render_prompt()` method.
```

- [ ] **Step 3: Update orchestrator spec**

Add a note to the relevant section in `specs/orchestrator.md`:

```markdown
### Task Context Assembly

Task execution context is assembled using `PromptBuilder` (see `specs/prompt-builder.md`).
The orchestrator calls `_build_task_context_with_prompt_builder()` which uses PromptBuilder
to compose system metadata, execution rules, upstream dependency summaries, agent role
instructions, and the task description into a single prompt string.
```

- [ ] **Step 4: Commit spec updates**

```bash
git add specs/chat-agent.md specs/hooks.md specs/orchestrator.md specs/prompt-builder.md
git commit -m "Update specs to reflect PromptBuilder migration"
```

---

### Task 10: Final verification and cleanup

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --ignore=tests/chat_eval`
Expected: All tests PASS. No regressions.

- [ ] **Step 2: Run linter**

Run: `ruff check src/prompt_builder.py`
Expected: No errors

- [ ] **Step 3: Verify prompt output parity**

Manually check that the PromptBuilder-based prompt assembly in each migrated call site produces output consistent with the old implementation. Key files to spot-check:
- `src/adapters/claude.py:_build_prompt()` — same format as before
- `src/chat_agent.py:_build_system_prompt()` — same system prompt content
- `src/hooks.py:_build_hook_context()` — same preamble format
- `src/orchestrator.py:_build_task_context_with_prompt_builder()` — same section ordering

- [ ] **Step 4: Commit any final cleanup**

```bash
git add -A
git commit -m "Phase 1 complete: PromptBuilder unified prompt assembly"
```

---

## Summary

| Task | What | Files | Tests |
|------|------|-------|-------|
| 1 | Write PromptBuilder spec | `specs/prompt-builder.md` | — |
| 2 | Template loading & rendering | `src/prompt_builder.py`, tests | 5 tests |
| 3 | 5-layer assembly pipeline | `src/prompt_builder.py`, tests | 7 tests |
| 4 | Depth-aware task prompting | `src/prompt_builder.py`, tests | 3 tests |
| 5 | Migrate adapter | `src/adapters/claude.py`, tests | 1 parity test |
| 6 | Migrate chat agent | `src/chat_agent.py`, tests | 2 parity tests |
| 7 | Migrate hooks | `src/hooks.py`, tests | 1 parity test |
| 8 | Migrate orchestrator | `src/orchestrator.py`, tests | 1 parity test |
| 9 | Update existing specs | `specs/*.md` | — |
| 10 | Final verification | — | Full suite |

**Total new tests:** ~20
**Files created:** 2 (`src/prompt_builder.py`, `specs/prompt-builder.md`)
**Files modified:** 4 (`src/adapters/claude.py`, `src/chat_agent.py`, `src/hooks.py`, `src/orchestrator.py`) + 3 specs
**Files NOT removed (deferred to Phase 6):** `agent_prompting.py`, `prompt_registry.py`, `prompt_manager.py`
