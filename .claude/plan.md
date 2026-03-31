---
auto_tasks: true
---

# Plan: Expose All CommandHandler Commands via MCP Server

## Background & Motivation

The agent-queue system has a unified `CommandHandler` with 80+ commands (`_cmd_*` methods) that serve as the single execution layer for both Discord slash commands and the supervisor's LLM tool-use loop. There is also an existing MCP server at `packages/mcp_server/mcp_server.py` that manually defines ~20 tools (task CRUD, project pause/resume, dependencies, workspaces, agents, monitoring) with hand-written FastMCP `@mcp.tool()` decorated functions.

**The problem:** The existing MCP server only exposes a small subset of commands, each manually reimplementing logic by directly hitting the Database layer. When new commands are added to CommandHandler, they don't automatically appear in the MCP server. This means Claude agents connected via MCP can't access the full set of capabilities (hooks, memory, git, files, profiles, system diagnostics, etc.).

**The goal:** Refactor the MCP server to automatically expose **all** CommandHandler commands as MCP tools, using the tool definitions already maintained in `_ALL_TOOL_DEFINITIONS` from `src/tool_registry.py`. This gives Claude agents (working via MCP) the same feature parity that the supervisor already has. A configuration mechanism allows excluding specific commands.

## Architecture Decisions

### Approach: CommandHandler-Delegating MCP Server

Instead of the current approach (each MCP tool reimplements logic by directly calling `Database`), the new MCP server will:

1. **Instantiate a `CommandHandler`** during lifespan (requires `Orchestrator` and `AppConfig`)
2. **Auto-generate MCP tools** from `_ALL_TOOL_DEFINITIONS` in tool_registry — each tool simply calls `command_handler.execute(name, args)`
3. **Keep existing resources** (they're read-only views that are useful as-is)
4. **Support an exclusion list** via config or environment variable to hide specific commands

### Why CommandHandler, not direct DB?

- CommandHandler already handles validation, authorization, error formatting
- Feature parity by construction — same code path as Discord and supervisor
- No duplication of business logic
- New commands automatically appear as MCP tools

### Initialization Challenge

CommandHandler requires an `Orchestrator` instance. The MCP server currently only initializes a `Database` and `EventBus`. We have two options:

- **Option A (Recommended):** Create a lightweight `Orchestrator` in the MCP server lifespan. The orchestrator's `initialize()` sets up DB, event bus, scheduler, git manager — everything CommandHandler needs. The MCP server just won't call `orchestrator.run()` (no scheduling loop). This gives full command support.
- **Option B:** Create a `CommandHandler` with a mock/minimal orchestrator that only has DB + config. Some commands that touch orchestrator state (stop_task, restart_daemon) would fail gracefully.

We'll go with **Option A** — initialize a real Orchestrator but don't run its scheduling loop. This gives maximum command coverage.

### Exclusion Configuration

Add an `mcp_server` section to the config YAML:

```yaml
mcp_server:
  excluded_commands:
    - shutdown
    - restart_daemon
    - update_and_restart
    - run_command  # dangerous for external MCP clients
```

Default exclusions: `shutdown`, `restart_daemon`, `update_and_restart`, `run_command` (destructive/dangerous commands). Everything else exposed by default.

### Tool Registration Strategy

Use FastMCP's programmatic tool registration. For each tool definition in `_ALL_TOOL_DEFINITIONS` that isn't excluded, dynamically register a tool function that calls `command_handler.execute(tool_name, args)` and returns the JSON result.

### Key Files Reference

- `packages/mcp_server/mcp_server.py` — Existing MCP server with ~20 hand-written tools and resources
- `packages/mcp_server/mcp_interfaces.py` — Serialization helpers for resources
- `src/tool_registry.py` — `_ALL_TOOL_DEFINITIONS` list (~80 tool JSON Schema dicts), `_TOOL_CATEGORIES` mapping, `CATEGORIES` metadata
- `src/command_handler.py` — `execute(name, args) -> dict` dispatches to `_cmd_{name}()` methods (~80+ commands)
- `src/orchestrator.py` — Central orchestrator with `initialize()` / `close()` lifecycle
- `src/config.py` — `AppConfig` loading from YAML

---

## Phase 1: Refactor MCP Server Lifespan to Initialize CommandHandler

**Goal:** Replace the current DB-only lifespan with one that creates a full `Orchestrator` + `CommandHandler`, making all commands available for delegation.

**Files to modify:**
- `packages/mcp_server/mcp_server.py` — rewrite `server_lifespan()` to init Orchestrator + CommandHandler

**Changes:**
1. In `server_lifespan()`:
   - Load `AppConfig` from the standard config path (or `--config` CLI arg)
   - Create `Orchestrator(config)` and call `await orchestrator.initialize()` (sets up DB, event bus, git manager)
   - Create `CommandHandler(orchestrator, config)` and wire it to the orchestrator via `orchestrator.set_command_handler()`
   - Store `command_handler` in lifespan context alongside existing `db` and `event_bus`
   - On shutdown, call `await orchestrator.close()`
2. Add `_get_command_handler()` helper (like existing `_get_db()`)
3. Update CLI args to accept `--config` path
4. Keep existing `_get_db()` and `_get_event_bus()` working (resources still use them)

---

## Phase 2: Auto-Register All Commands as MCP Tools

**Goal:** Dynamically register MCP tools from `_ALL_TOOL_DEFINITIONS`, delegating execution to `CommandHandler.execute()`.

**Files to modify:**
- `packages/mcp_server/mcp_server.py` — add dynamic tool registration logic, remove hand-written tools

**Changes:**
1. Import `_ALL_TOOL_DEFINITIONS` from `src.tool_registry`
2. Define `DEFAULT_EXCLUDED_COMMANDS` constant:
   ```python
   DEFAULT_EXCLUDED_COMMANDS = {
       "shutdown", "restart_daemon", "update_and_restart",
       "run_command",  # dangerous for external MCP clients
       "browse_tools", "load_tools",  # meta-tools for LLM context management, not MCP
   }
   ```
3. Create a `register_command_tools(mcp_server, excluded)` function that iterates `_ALL_TOOL_DEFINITIONS` and for each non-excluded tool:
   - Creates a closure that calls `command_handler.execute(name, args)` and returns `json.dumps(result)`
   - Registers it with FastMCP using the tool's `name`, `description`, and `input_schema` from the definition
   - Uses FastMCP's programmatic `mcp.add_tool()` or equivalent API for dynamic registration
4. Call `register_command_tools()` after MCP server creation (at module level, or during lifespan)
5. **Remove all existing hand-written `@mcp.tool()` functions** (~15 functions, ~300 lines) — they'll be replaced by auto-registered equivalents
6. Keep all `@mcp.resource()` functions (read-only views) and `@mcp.prompt()` templates

---

## Phase 3: Add Exclusion Configuration Support

**Goal:** Allow configuring which commands are hidden from MCP via config YAML and environment variables.

**Files to modify:**
- `packages/mcp_server/mcp_server.py` — read exclusion config during startup
- `src/config.py` — add `mcp_server` config section (if config model exists and it makes sense)

**Changes:**
1. Read exclusions from config YAML at `mcp_server.excluded_commands` (list of command names)
2. Support `AGENT_QUEUE_MCP_EXCLUDED` environment variable (comma-separated) as override/addition
3. Merge `DEFAULT_EXCLUDED_COMMANDS` + config + env into final exclusion set
4. Log which commands are exposed vs excluded at startup (info level)
5. Make the exclusion set accessible for testing/introspection

---

## Phase 4: Update Tests and Documentation

**Goal:** Ensure the refactored MCP server works correctly and is well-documented.

**Files to modify:**
- `packages/mcp_server/test/test_mcp_server.py` — update tests for new architecture
- `specs/mcp-server.md` — create spec documenting the MCP server architecture
- `profile.md` — update quick reference

**Changes:**
1. Update tests to verify:
   - All non-excluded commands from `_ALL_TOOL_DEFINITIONS` are registered as MCP tools
   - Excluded commands are NOT registered
   - Tool execution delegates to CommandHandler.execute() and returns JSON results
   - Resources still work correctly
   - Exclusion configuration (defaults, config, env) merges correctly
2. Add a drift-detection test: compare registered MCP tools against `_ALL_TOOL_DEFINITIONS` to catch tools that get added to the registry but not exposed
3. Create `specs/mcp-server.md` documenting:
   - Architecture (CommandHandler delegation vs. direct DB access)
   - All exposed tools (auto-generated from tool_registry)
   - Exclusion configuration (YAML, env var, defaults)
   - Resource URIs available
   - How to connect Claude agents via MCP
   - Entry point usage (`agent-queue-mcp --config PATH`)
4. Update `profile.md` to mention MCP server exposes all CommandHandler commands
5. Verify `pyproject.toml` entry point `agent-queue-mcp` still works with updated CLI args
