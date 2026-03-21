# Tiered Tool System

## Purpose

Reduce the LLM's per-interaction context by presenting only ~11 core tools by default.
All other tools are organized into 6 named categories that can be loaded on demand.
This affects only which tool **definitions** the LLM sees -- the execution path through
CommandHandler is unchanged.

## Concepts

### Core Tools

Core tools are always included in every LLM interaction's `tools` parameter.
They cover the most common operations: task CRUD, tool/rule navigation, memory
search, and messaging. Approximately 11 tools.

### Tool Categories

Six named categories group related tools:

| Category | Description | Approximate Count |
|----------|-------------|-------------------|
| `git` | Branch, commit, push, PR, and merge operations | 11 |
| `project` | Project CRUD, workspace management | 16 |
| `agent` | Agent management, agent profiles | 17 |
| `hooks` | Direct hook management (low-level) | 6 |
| `memory` | Memory operations beyond search, notes, project profiles | 13 |
| `system` | Token usage, config, diagnostics, task lifecycle ops, prompts | 33 |

### ToolRegistry

A singleton-like registry that owns:
- The mapping of tool name to JSON Schema definition
- The mapping of tool name to category name (or "core")
- Category metadata (name, description, tool count)

### Navigation Flow

1. LLM calls `browse_tools` -- receives category list with names, descriptions, tool counts
2. LLM calls `load_tools(category="git")` -- category's tool schemas are injected into
   the `tools` parameter for subsequent LLM turns in the same interaction
3. LLM can now call any git tool

### Mutable Tool Set Per Interaction

`ChatAgent.chat()` starts each interaction with only core tools. When `load_tools`
is called mid-interaction, the loaded category's schemas are appended to the active
tool set. Subsequent `create_message()` calls within the same `chat()` invocation
see the expanded list. The expansion does NOT persist across separate `chat()` calls.

### Response Formats

**browse_tools:**
```json
{
  "categories": [
    {"name": "git", "description": "Branch, commit, push, PR, and merge operations", "tool_count": 11}
  ]
}
```

**load_tools:**
```json
{
  "loaded": "git",
  "tools_added": ["get_git_status", "git_commit", "..."],
  "message": "11 git tools are now available."
}
```
The tool schemas themselves are NOT in this response -- they are injected into the
`tools` parameter of subsequent API calls.

## Invariants

- CommandHandler.execute() dispatches ALL tools regardless of whether they are loaded.
  Tiered loading is purely a context-management concern.
- Core tools cannot be unloaded.
- Loading the same category twice is idempotent (no duplicates).
- Tool names are globally unique across all categories.
- browse_tools and load_tools are themselves core tools.
- Category metadata (descriptions) are static -- defined in code, not configurable.
