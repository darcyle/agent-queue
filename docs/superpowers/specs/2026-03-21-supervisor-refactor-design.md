# Supervisor Refactor — Design Specification

**Date:** 2026-03-21
**Status:** Draft
**Branch:** `supervisor-refactor`

## Overview

Transform the agent-queue's ChatAgent from a simple Discord chat interface into a Supervisor — a single intelligent entity that oversees all project activity, manages its own behavioral rules, and continuously reflects on its work. This refactor also unifies the fragmented prompt assembly system, introduces tiered tool loading, and replaces the half-built ChatAnalyzer with integrated chat observation.

## Goals

1. **Single intelligent entity** — All LLM reasoning flows through the Supervisor. No competing systems (ChatAgent, ChatAnalyzer, hook LLM calls) making independent decisions.
2. **Self-managing rules** — The Supervisor can create, modify, and delete its own behavioral rules in response to user instructions. Rules are stored in the memory system and backed by auto-generated hooks.
3. **Continuous reflection** — Every action the Supervisor takes gets a verification pass. Event-driven and periodic reflection catches issues, enforces rules, and drives autonomous follow-up work.
4. **Unified prompt assembly** — A single `PromptBuilder` replaces 5+ scattered prompt assembly paths.
5. **Lean context** — Tiered tool loading replaces the 70+ tool system prompt with ~10 core tools and on-demand category loading.
6. **Chat absorption** — Passive observation of project channels feeds the memory system and enables proactive suggestions without autonomous action.

## Non-Goals

- Changing the orchestrator's deterministic behavior
- Modifying the database schema for tasks, agents, or projects
- Changing the Claude Code adapter's SDK integration
- Replacing the Discord bot layer or slash commands
- Multi-agent coordination (single supervisor, not agent mesh)

---

## Section 1: Supervisor Identity

The `ChatAgent` class evolves into `Supervisor`. It is the single intelligent entity in the system. All LLM reasoning flows through it.

### Three Activation Modes

**1. Direct Address**
A user @mentions agent-queue or messages it directly. The Supervisor reasons and acts autonomously — creates tasks, modifies rules, manages hooks, runs commands. Full authority.

**2. Passive Observation**
Users discuss the project in a channel without addressing agent-queue. The Supervisor absorbs information into memory and may propose suggestions (posted as messages with accept/dismiss buttons), but does NOT take autonomous action on the project. It CAN autonomously update its own memory and understanding.

**3. Reflection**
Periodic and event-driven self-initiated reasoning. The Supervisor reviews task completions, hook executions, user request outcomes, and system state. This can trigger autonomous actions (spin up tasks, fire hooks, post notifications) because it is executing rules the user previously authorized.

**Key distinction:** Direct address and rule-authorized reflection can act. Passive observation can only learn and suggest.

### What It Replaces

- `ChatAgent` — absorbed entirely; the Supervisor IS the evolved ChatAgent
- `ChatAnalyzer` — removed; observation and suggestion behavior absorbed into passive observation mode
- Hook LLM invocations — hooks still execute via the hook engine, but the hook engine no longer creates fresh `ChatAgent` instances for LLM calls. Instead, hook LLM invocations use an async callback pattern: the hook engine calls `await supervisor.process_hook_llm(hook_context, rendered_prompt)` which returns the Supervisor's response. The Supervisor processes this through its normal action-reflect cycle and returns the result to the hook engine, which then completes its execution (posting notifications, running follow-up steps, etc.). This preserves the "single intelligent entity" guarantee while keeping the hook engine's scheduling, context gathering, and short-circuit logic intact. The Supervisor instance is passed to the hook engine at initialization

---

## Section 2: Tiered Tool & Context Navigation

The Supervisor starts each interaction with a compact context and ~10 core tools. Everything else is loaded on demand.

### Core Tools (Always Loaded)

| Tool | Purpose |
|------|---------|
| `create_task` | Spin up work |
| `list_tasks` | Check current state |
| `edit_task` | Modify existing work |
| `browse_tools` | List available tool categories with descriptions |
| `load_tools` | Load a specific tool category's full definitions |
| `browse_rules` | List active rules for current project and globals |
| `load_rule` | Load a specific rule's full detail |
| `save_rule` | Create or update a rule |
| `delete_rule` | Remove a rule |
| `memory_search` | Search project memory |
| `send_message` | Post to Discord |

### Tool Categories (Loaded On Demand)

| Category | Description | Tools Included |
|----------|-------------|----------------|
| `git` | Branch, commit, push, PR, merge operations | `git_push`, `git_create_pr`, `git_merge`, etc. |
| `project` | Project CRUD, workspace management | `create_project`, `edit_project`, `add_workspace`, etc. |
| `agent` | Agent management, profiles | `create_agent`, `pause_agent`, `view_profile`, etc. |
| `hooks` | Direct hook management (low-level) | `create_hook`, `edit_hook`, `list_hooks`, etc. |
| `memory` | Memory operations beyond search | `compact_memory`, `view_profile`, `memory_note`, etc. |
| `system` | Token usage, config, diagnostics | `get_token_usage`, `run_command`, `read_file`, etc. |

### Navigation Mechanics

**`browse_tools`** returns a list of categories, each with a name, short description of what tasks it handles, and tool count. No tool schemas — just enough for the Supervisor to decide if it needs that category.

**`load_tools`** given a category name, returns the full JSON Schema definitions for all tools in that category. These are added to the active tool set for the remainder of that interaction.

**Rule navigation uses the same pattern.** `browse_rules` returns rule names + summaries. `load_rule` returns the full rule definition. The Supervisor drills into relevant rules on demand rather than holding all rules in context.

### System Prompt Impact

The system prompt shrinks from ~2,150 lines to a compact description of:
- Supervisor role and identity
- The three activation modes
- How to use navigation tools (browse → load pattern)
- Core behavioral guidelines

Detailed per-tool documentation moves into the category payloads returned by `load_tools`.

### Command Execution Path

The tiered tool system only affects which tool **definitions** are sent to the LLM — it does not change how tool **calls** are dispatched. `CommandHandler` remains the single execution backend. When the Supervisor (or any LLM context) calls a tool, the call flows through `CommandHandler.execute(name, args)` exactly as it does today. `browse_tools` and `load_tools` are themselves registered in `CommandHandler`. The only change is that the LLM sees fewer tool definitions per interaction, not that the execution path changes.

### Tool Loading Mechanics

**`browse_tools` response format:**
```json
{
  "categories": [
    {
      "name": "git",
      "description": "Branch, commit, push, PR, and merge operations for project repositories",
      "tool_count": 12
    },
    ...
  ]
}
```
Returned as a tool result (text the LLM reads to decide which category to load).

**`load_tools` response format:**
The loaded tool schemas are **injected into the `tools` parameter** of subsequent LLM API calls within the same interaction. They are NOT returned as text in a tool result (the LLM cannot call tools it only sees as text). The `Supervisor.chat()` method maintains a mutable tool set per interaction — `load_tools` appends to it, and subsequent LLM turns see the expanded `tools` list.

The tool result message for `load_tools` confirms what was loaded:
```json
{
  "loaded": "git",
  "tools_added": ["git_push", "git_create_pr", "git_merge", ...],
  "message": "12 git tools are now available."
}
```

### Agent Profile Integration

When `PromptBuilder` assembles a task execution prompt, it resolves the agent profile through the existing cascade (task → project → default). Profile fields are incorporated as follows:

- `system_prompt_suffix` → injected into the identity layer as additional role instructions
- `model` → passed through to adapter options (not part of the prompt)
- `allowed_tools` → constrains which tools are available in the tools layer
- `mcp_servers` → passed through to adapter options
- `permission_mode` → passed through to adapter options

The profile resolution logic itself stays in the orchestrator (it's configuration lookup, not intelligence). `PromptBuilder` receives the resolved profile as input.

---

## Section 3: Rule System — Memory-Native

Rules are the Supervisor's persistent autonomous behaviors — natural language intentions stored as memory entries, with hooks as a derived execution layer.

### Rule Storage

Rules live as structured markdown files in the memory system:
- Project-scoped: `~/.agent-queue/memory/{project_id}/rules/`
- Global: `~/.agent-queue/memory/global/rules/`

They are semantically indexed like any other memory entry, which means:
- The Supervisor can search for relevant rules naturally
- Rules surface in tiered context when semantically relevant to the current action
- Rules can reference each other via natural language
- The Supervisor can refine rule language over time

### Rule Document Structure

```markdown
---
id: rule-keep-tunnel-open
type: active
project_id: my-game-server
hooks: [hook-abc123, hook-def456]
created: 2026-03-20T10:00:00Z
updated: 2026-03-21T14:30:00Z
---

# Keep Cloudflare tunnel open

## Intent
The game server needs a persistent Cloudflare tunnel. If the tunnel
goes down or the address changes, the game config must be updated
and pushed so clients get the new address.

## Trigger
Check every 5 minutes.

## Logic
1. Run `cloudflared tunnel status` to check if tunnel is alive
2. If down: restart with `cloudflared tunnel run`
3. Capture the new tunnel address
4. If address differs from what's in `game-config.json`: update the
   file, commit, and push
5. Announce the new address in the project channel
```

YAML frontmatter provides structure (id, type, hook references, timestamps). The body is natural language the Supervisor interprets.

### Two Rule Types

**Active rules** have triggers and generate hooks. The Supervisor translates intent + trigger + logic into one or more hooks with appropriate triggers, context steps, and prompt templates. Hooks execute on schedule/event, and when the prompt fires, the Supervisor reasons about what to do.

**Passive rules** have no triggers. They influence how the Supervisor thinks. Example: "When reviewing PRs for this project, always check for SQL injection." These surface through semantic search when the Supervisor is doing related work — evaluating task results, building task prompts, etc.

### Rule Lifecycle

```
User says "always keep the tunnel open"
    → Supervisor creates active rule via save_rule
    → Rule processor generates hooks from rule
    → Hooks execute on schedule via existing hook engine
    → User says "check every 10 minutes instead"
    → Supervisor updates rule via save_rule
    → Rule processor detects change, regenerates hooks
    → Old hooks deleted, new hooks created
```

### Source of Truth

Rules in memory are the canonical definition. Hooks are derived artifacts — disposable and regenerable. The rule's `hooks` frontmatter field tracks which hooks implement it. On system startup, a reconciliation pass scans all rules and verifies their hooks exist, regenerating any that are missing.

### Hook Generation

When a rule is saved or updated:
1. Compare the rule's current trigger/logic against its existing hooks
2. If new or trigger changed → generate new hooks
3. Hook prompt templates reference the rule ID so the Supervisor knows which rule it's executing
4. Hook generation may require an LLM call to translate natural language logic into context steps + prompt template — this happens once at creation/edit, not on every execution

### How Rules Influence Task Prompts

When the `PromptBuilder` assembles context for a task execution, it queries the memory system for relevant entries. Passive rules stored as memory entries surface automatically when semantically relevant to the task description. No special plumbing needed.

### Degradation Without Memsearch

The memory system's semantic search (Milvus via memsearch) is an optional dependency. Rules must work without it:

- **Rule files are always readable from disk.** The `rules/` directory uses plain markdown files. `browse_rules` and `load_rule` read directly from the filesystem, not from Milvus. Rules never become unavailable because a search index is down.
- **What degrades:** Automatic surfacing of relevant rules via semantic search. Without memsearch, `PromptBuilder.load_relevant_rules()` falls back to loading ALL rules for the current project (and globals), filtering by rule type. For projects with few rules (the common case), this is equivalent. For projects with many rules, it means slightly larger context but no loss of functionality.
- **Hook reconciliation is filesystem-based.** The startup reconciliation scan reads rule files from disk and checks hook IDs against the database. No search index needed.
- **`memory_search` core tool degradation.** The `memory_search` tool is always loaded as a core tool. When memsearch/Milvus is unavailable, `memory_search` falls back to substring matching against rule files, task memory files, and the project profile on disk. Results are less relevant than semantic search but functional. If no memory files exist at all, the tool returns an empty result set with a note that the memory system is not configured — it never errors.

### Global Memory Scope

The existing memory system is project-scoped. This design requires a `global/` scope for system-wide rules. This is implemented as a pseudo-project directory `~/.agent-queue/memory/global/` that follows the same structure as project memory directories but is not tied to any project ID. `browse_rules` queries both the current project's rules directory and the global rules directory.

### Rule Tool Contracts

**`save_rule`:**
```
Input: {
  id: string (optional, auto-generated if omitted),
  project_id: string | null (null = global),
  type: "active" | "passive",
  content: string (markdown body: intent, trigger, logic)
}
Output: { success: true, id: string, hooks_generated: string[] }
Error: { success: false, error: string }
```
On success, writes the rule file with YAML frontmatter and triggers hook reconciliation for active rules. If `id` matches an existing rule, it is updated and hooks are regenerated.

**`delete_rule`:**
```
Input: { id: string }
Output: { success: true, hooks_removed: string[] }
Error: { success: false, error: "Rule not found" }
```
Deletes the rule file and removes all associated hooks from the database.

**`browse_rules`:**
```
Input: { project_id: string | null (null = current project) }
Output: {
  rules: [
    { id: string, name: string, type: string, project_id: string | null,
      summary: string (first 200 chars of intent), hook_count: int,
      updated: string }
  ]
}
```
Lists rules from the specified project plus all global rules.

**`load_rule`:**
```
Input: { id: string }
Output: { id: string, type: string, project_id: string | null,
          hooks: string[], content: string (full markdown body),
          created: string, updated: string }
```

### Hook Generation Validation

When the Supervisor generates hooks from a rule (which may involve an LLM call to translate natural language into context steps and prompt templates), the generated hook configuration must pass the same validation as manually-created hooks before being persisted. Invalid configurations (bad step types, malformed cron expressions, missing required fields) are rejected, and the failure is reported back to the Supervisor so it can retry or surface the issue to the user.

---

## Section 4: Reflection System

The Supervisor has an inner reflection loop that wraps every action, plus periodic proactive reflection.

### The Action-Reflect Cycle

```
Trigger (any source)
    ↓
Supervisor reasons + acts
    ↓
Reflection pass:
  1. Did I do what was asked/intended?
  2. Did the actions succeed? (verify results concretely)
  3. Are there relevant rules I should evaluate now?
  4. Did I learn anything that should update memory?
  5. Is there follow-up work needed?
    ↓
If reflection produces actions → loop back
    ↓
Done
```

### Applies to ALL Trigger Types

| Trigger | Example | Reflection checks |
|---------|---------|-------------------|
| User request | "Add a task to fix the login bug" | Task created correctly? Right project/description? Rules about task structure? |
| Hook execution | Tunnel monitor fires | Restart succeeded? Config updated? Notify anyone? |
| Task completion | Agent finishes implementation | Runtime errors? Rules require follow-up? Update project profile? |
| Passive observation | Users discuss a new API endpoint | Memory updated? Rules apply? Suggest creating a task? |
| Periodic sweep | 15-minute project review | Stuck tasks? Orphaned hooks? Services healthy? Rules in sync? |
| Another reflection | Post-task reflection created a task | Task created correctly? Right dependencies? |

### Self-Verification Is Concrete

The Supervisor actually checks its work. If it created a task, it calls `list_tasks` and verifies it exists with the right fields. If it updated a config file, it reads it back. If it pushed code, it checks git status. The tiered tool system supports this — the Supervisor loads whatever tool category it needs to verify results.

### Reflection Depth

Not every action needs the same depth. The Supervisor judges based on context:

| Depth | When | What happens |
|-------|------|-------------|
| Deep | Task completion, hook failures, complex user requests | Full rule scan, memory update, verify all side effects, follow-up evaluation |
| Standard | Successful hook execution, simple user requests | Verify action succeeded, check directly relevant rules, brief memory note |
| Light | Passive observation, uneventful periodic sweep | Update memory if relevant, skip rule evaluation unless notable |

### Configuration

```yaml
supervisor:
  reflection:
    level: full        # full | moderate | minimal | off
    periodic_interval: 900  # seconds between proactive sweeps
```

| Level | Post-action | Periodic | Passive observation |
|-------|-------------|----------|---------------------|
| `full` | Deep rule evaluation + memory update | Every N minutes | Absorb all chat, suggest proactively |
| `moderate` | Rule evaluation, skip memory update | Every 30 min | Absorb direct mentions and keywords |
| `minimal` | Only check rules with explicit triggers | Disabled | Only absorb direct mentions |
| `off` | No reflection | Disabled | Disabled |

### Default Global Rules

Every installation gets two default active rules:

**Post-action reflection** — event-driven, fires after task completion, hook execution, and user request processing. Evaluates results against project rules and determines follow-up.

**Periodic project review** — fires every N minutes (configurable). Checks for stuck tasks, orphaned hooks, rule-hook sync, unresolved suggestions, service health.

These are rules like any other — the Supervisor can customize them per-project.

### Reflection Safety Controls

The action-reflect cycle can theoretically recurse (reflection creates action, action triggers reflection). Hard limits prevent runaway loops:

- **Maximum reflection depth: 3.** A reflection pass that produces actions can trigger at most 2 more nested reflections. After depth 3, remaining follow-up items are queued as pending work for the next cycle rather than executed immediately.
- **Per-cycle token cap.** Configurable maximum tokens per reflection cycle. When exceeded, the Supervisor stops reflecting and queues remaining evaluations. Integrates with the existing `BudgetManager` for tracking.
- **Circuit breaker.** If reflection has consumed more than N tokens in the last hour (configurable), the Supervisor drops to `minimal` reflection level automatically and posts a notification. Prevents runaway cost in pathological scenarios (e.g., a rule that always triggers follow-up work).

```yaml
supervisor:
  reflection:
    level: full
    periodic_interval: 900
    max_depth: 3                    # maximum nested reflection iterations
    per_cycle_token_cap: 10000      # max tokens per reflection cycle
    hourly_token_circuit_breaker: 100000  # drop to minimal if exceeded
```

---

## Section 5: Prompt Architecture

A single `PromptBuilder` replaces all scattered prompt assembly paths.

### Five Prompt Layers

Every LLM call is assembled from these layers:

1. **Base identity** — who am I? (Supervisor role, task agent role, hook executor role)
2. **Project context** — what project? (profile summary from memory)
3. **Relevant rules** — what rules apply? (semantic search against current action)
4. **Specific context** — what am I doing? (task description, hook data, user message, upstream summaries)
5. **Available tools** — what can I do? (core tools + loaded categories)

### PromptBuilder Interface

```python
builder = PromptBuilder(project_id="my-game")

# Layer 1 - identity (loaded from prompt template)
builder.set_identity("supervisor")  # or "task-agent" or "hook-executor"

# Layer 2 - project context (from memory system)
builder.load_project_context()

# Layer 3 - rules (semantic search)
builder.load_relevant_rules(query="tunnel monitoring status check")

# Layer 4 - specific context
builder.add_context("task", task_description)
builder.add_context("upstream", upstream_summaries)

# Layer 5 - tools
builder.set_core_tools(SUPERVISOR_CORE_TOOLS)

# Assemble
system_prompt, tools = builder.build()
```

### What It Replaces

| Current | New |
|---------|-----|
| `chat_agent.py:_build_system_prompt()` | `PromptBuilder.set_identity("supervisor")` |
| `orchestrator.py` ~200 lines of string concat | `PromptBuilder.load_project_context()` + `.add_context()` |
| `agent_prompting.py` 4 prompt builders | `PromptBuilder.set_identity("task-agent")` with context variants |
| `hooks.py:_build_hook_context()` + `_render_prompt()` | `PromptBuilder.set_identity("hook-executor")` + `.add_context("hook", ...)` |
| `adapters/claude.py:_build_prompt()` | Receives assembled prompt from `PromptBuilder` |
| 7 markdown templates + hardcoded Python strings | Templates still exist, loaded by PromptBuilder as identity/layer sources |

### Three Prompt Contexts

| Context | Who | Layers Emphasized |
|---------|-----|-------------------|
| Supervisor | Supervisor responding to any trigger | Role identity, core tools, project context, loaded categories, loaded rules |
| Task execution | Claude Code agent working on a task | Workspace context, execution rules, upstream work, relevant passive rules, memory |
| Hook execution | Supervisor reasoning about hook result | Hook preamble, owning rule, trigger data, context step results |

### Depth-Aware Task Prompting

The existing `agent_prompting.py` has depth-aware logic: root tasks get plan generation instructions, intermediate subtasks get controlled splitting, and max-depth subtasks get execution-only focus. This behavior is preserved in `PromptBuilder` through context variants on the `task-agent` identity:

- `builder.add_context("task_depth", {"depth": 0, "max_depth": 3})` — PromptBuilder selects the appropriate execution rules template based on depth
- Root depth (0): includes plan structure guide, allows plan generation
- Intermediate depths: includes controlled splitting rules
- Max depth: execution-only, no plan generation allowed

These templates are loaded from `src/prompts/` by PromptBuilder — the existing template files (`plan_structure_guide.md`, `controlled_splitting.md`, `execution_focus.md`) are retained and loaded as context-specific content within Layer 4.

### Prompt Registry Disposition

The existing `prompt_registry.py` and `prompt_manager.py` are **absorbed into PromptBuilder**. PromptBuilder takes over template loading, YAML frontmatter parsing, variable substitution, and caching. The `src/prompts/*.md` template files remain as the source content. The registry's public API (`get()`, `render()`) is replaced by PromptBuilder's internal template loading. No external callers need the registry directly — all prompt assembly goes through PromptBuilder.

---

## Section 6: Chat Observation & Absorption

Replaces the ChatAnalyzer with integrated observation in the Supervisor.

### Two-Stage Processing

**Stage 1 — Keyword/pattern filter (deterministic, no LLM):**
- Does the message mention the project name, repo, or technical terms from the project profile?
- Does it contain action words (deploy, release, broken, bug, error, merge)?
- Is it longer than a trivial threshold?
- Has the conversation been active (multiple people, sustained discussion)?

If none trigger, the message is ignored. Zero token cost.

**Stage 2 — Supervisor LLM pass (only for messages passing stage 1):**
- Lightweight call with minimal context: project profile summary + recent conversation buffer + the message
- Supervisor decides: update memory? suggest something? ignore?
- Uses the cheap/local model configured for reflection

### Conversation Batching

Rather than processing individual messages, the Supervisor batches. Buffer messages per channel over a short window (30-60 seconds of quiet, or N messages, whichever comes first), then process the batch as a single conversation snippet.

### What Gets Remembered vs. Suggested

- **Always remember:** Technical decisions, requirements, architecture discussions, bug reports, priorities — anything that makes the Supervisor smarter about the project
- **Suggest only when actionable:** Work items, not parroting. "I could create a task to profile the particle system. Want me to?"
- **Never suggest during active conversation:** Wait for the conversation to settle (the batching window handles this naturally)

### Suggestion UI

Suggestions are posted as Discord embeds with Accept/Dismiss button interactions, reusing the existing `AnalyzerSuggestionView` component from the ChatAnalyzer before it is removed. The view is migrated to `src/discord/views.py` (or equivalent) as a standalone component. Accepted suggestions trigger the Supervisor in direct-address mode with the suggestion content as the instruction. Dismissed suggestions are logged to memory as "user declined this type of suggestion" to help the Supervisor learn what is and isn't helpful.

### Configuration

```yaml
supervisor:
  observation:
    enabled: true
    batch_window_seconds: 60    # wait for quiet before processing
    max_buffer_size: 20         # process after N messages regardless
    stage1_keywords: []         # additional keywords beyond project profile terms
```

---

## Section 7: Supervisor-Orchestrator Boundary

### What Stays in the Orchestrator (Deterministic)

- Task state machine transitions (DEFINED → READY → ASSIGNED → IN_PROGRESS → ...)
- Agent assignment and credit-weight scheduling
- Rate limit detection and backoff
- Workspace preparation (git clone, branch, worktree)
- Agent process management (start, stream, kill)
- Heartbeat monitoring

### What Moves to the Supervisor

| Currently in orchestrator | Moves to |
|---------------------------|----------|
| Plan generation instructions | Passive rules per project |
| `agent_prompting.py` 4 prompt builders | `PromptBuilder` with identity variants |
| Memory context injection | `PromptBuilder.load_project_context()` |
| Profile resolution for task prompting | `PromptBuilder` pulls relevant rules + profile |
| Post-task plan parsing + subtask creation | Supervisor's post-action reflection |

### Task Execution Prompt Flow

1. Orchestrator is about to launch a Claude Code agent for a task
2. Orchestrator calls `PromptBuilder` with the task context
3. `PromptBuilder` pulls project memory, relevant passive rules, upstream summaries
4. `PromptBuilder` returns the assembled prompt
5. Orchestrator passes it to the adapter unchanged

The orchestrator doesn't know what's in the prompt. It asks for one and forwards it.

### Post-Task Flow Change

Today: orchestrator checks for plan files, parses them, creates subtasks.
New: orchestrator emits `task.completed` → Supervisor reflection rule fires → Supervisor reads results, checks for plans, evaluates rules → Supervisor creates follow-up tasks through `create_task`.

---

## Section 8: Migration Plan

### Phase Ordering and Dependencies

```
Phase 1: PromptBuilder              (no dependencies)
    ↓
Phase 2: Rule System                (depends on Phase 1 for prompt assembly)
    ↓
Phase 3: Tiered Tool System         (depends on Phase 1 for prompt assembly)
    ↓
Phase 4: Supervisor Identity +      (hard depends on Phase 1, 2, 3)
         Reflection
    ↓
Phase 5: Chat Observation           (hard depends on Phase 4)
    ↓
Phase 6: Orchestrator Handoff +     (hard depends on Phase 4)
         Cleanup
```

Phases 2 and 3 are independent of each other and can be developed in parallel. Phase 4 requires all three predecessors. Phases 5 and 6 are independent of each other but both require Phase 4.

### Phase 1 — PromptBuilder
- Create `src/prompt_builder.py` with layered assembly
- Wire into existing call sites one at a time (adapter → orchestrator → chat agent → hooks)
- Tests compare old vs. new output for parity
- Absorb `agent_prompting.py`
- No behavioral changes — purely structural

### Phase 2 — Rule System
- Create `rules/` directory structure in memory filesystem
- Create `~/.agent-queue/memory/global/` directory for global scope
- Extend `MemoryManager` with rule file I/O (read/write/list markdown with YAML frontmatter)
- Add `save_rule`, `delete_rule`, `browse_rules`, `load_rule` to CommandHandler
- Add rule → hook reconciliation logic (filesystem scan → hook DB comparison → regeneration)
- Add startup reconciliation (scan all rule files, verify hooks, regenerate missing)
- Migrate hardcoded behaviors into default global rules
- Existing hooks continue working — rules are additive

### Phase 3 — Tiered Tool System
- Split 70+ tools into categories with metadata
- Implement `browse_tools` and `load_tools`
- Define ~10 core tools
- Rewrite system prompt to be compact
- Move tool documentation into category payloads

### Phase 4 — Supervisor Identity + Reflection
- Rename/refactor `ChatAgent` → `Supervisor`
- Add action-reflect cycle to main processing loop
- Wire event-driven reflection for all trigger types
- Add reflection depth configuration
- Self-verification behavior

### Phase 5 — Chat Observation
- Add keyword/pattern filter (stage 1)
- Add conversation batching and buffer
- Wire Supervisor LLM pass for relevant messages (stage 2)
- Memory updates from observed conversations
- Suggestion posting with accept/dismiss

### Phase 6 — Orchestrator Handoff + Cleanup

**Plan parsing migration (the largest sub-task):**

Today the orchestrator handles plan parsing in `_execute_task()` after a task completes (~200 lines):
1. Discovers plan files (`.claude/plan.md`, `plan.md`)
2. Archives previous plans
3. Parses plan into steps (regex + optional LLM)
4. Creates subtasks with dependency chains
5. Handles stored plan approval flow
6. Cleans up plan files

This logic moves to a dedicated Supervisor reflection handler triggered by `task.completed`:
- The Supervisor uses `read_file` to check for plan files in the task's workspace
- Uses the existing `plan_parser` module (retained as a library) to parse steps
- Creates subtasks through `create_task` with dependencies
- The orchestrator's plan-related code is removed from `_execute_task()`
- Plan archiving and cleanup become part of the Supervisor's post-task reflection logic

The `plan_parser.py` and `plan_parser_llm.py` modules are retained as libraries — they are called by the Supervisor rather than the orchestrator.

**Other cleanup:**
- Remove `chat_analyzer.py` — functionality absorbed into Supervisor passive observation
- Remove `agent_prompting.py` — absorbed into PromptBuilder
- Remove `prompt_registry.py` and `prompt_manager.py` — absorbed into PromptBuilder
- Migrate `AnalyzerSuggestionView` to `src/discord/views.py` before removing chat_analyzer
- Clean up orphaned imports and dead code paths

### Config Migration

New config keys are added under a `supervisor` section. The existing `chat_analyzer` config block is deprecated:

```yaml
# Old (deprecated, ignored if supervisor section exists)
chat_analyzer:
  enabled: true
  interval_seconds: 300

# New
supervisor:
  reflection:
    level: full
    periodic_interval: 900
    max_depth: 3
    per_cycle_token_cap: 10000
    hourly_token_circuit_breaker: 100000
  observation:
    enabled: true
    batch_window_seconds: 60
    max_buffer_size: 20
    stage1_keywords: []
```

On startup, if a `chat_analyzer` config exists but no `supervisor` config, the system logs a deprecation warning and applies sensible defaults for the supervisor config. No automatic migration — the old config is simply ignored once the supervisor is active.

### What Does NOT Change
- Database schema for tasks, agents, projects
- Discord bot layer (slash commands, notifications, channel routing)
- Git manager
- Token tracking and budgeting
- Scheduler algorithm
- Claude Code adapter SDK integration

### Risk Mitigation
- Each phase is independently deployable and testable
- Phase 1 validated by diffing old vs. new prompt output
- Phase 3 (tiered tools) is highest risk — extensive testing with real conversations before removing old path
- Keep old `ChatAgent.chat()` working alongside Supervisor until Phase 4 is validated
