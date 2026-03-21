"""Tests for Supervisor — the single intelligent entity."""
import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_supervisor():
    from src.supervisor import Supervisor
    orch = MagicMock()
    orch.config = MagicMock()
    orch.llm_logger = MagicMock()
    orch.llm_logger._enabled = False
    config = MagicMock()
    config.workspace_dir = "/tmp/test"
    config.chat_provider = MagicMock()
    config.supervisor = MagicMock()
    config.supervisor.reflection = MagicMock()
    config.supervisor.reflection.level = "full"
    config.supervisor.reflection.max_depth = 3
    config.supervisor.reflection.per_cycle_token_cap = 10000
    config.supervisor.reflection.hourly_token_circuit_breaker = 100000
    config.supervisor.reflection.periodic_interval = 900
    return Supervisor(orch, config)


def test_supervisor_class_exists():
    from src.supervisor import Supervisor
    assert Supervisor is not None

def test_supervisor_inherits_chat_behavior():
    sup = _make_supervisor()
    assert hasattr(sup, "chat")
    assert hasattr(sup, "initialize")
    assert hasattr(sup, "summarize")
    assert hasattr(sup, "handler")

def test_supervisor_has_reflection_engine():
    sup = _make_supervisor()
    assert hasattr(sup, "reflection")
    assert sup.reflection is not None

def test_backward_compat_import():
    from src.chat_agent import ChatAgent
    assert ChatAgent is not None

def test_backward_compat_is_supervisor():
    from src.chat_agent import ChatAgent
    from src.supervisor import Supervisor
    assert ChatAgent is Supervisor

def test_set_active_project():
    sup = _make_supervisor()
    sup.set_active_project("my-project")
    assert sup._active_project_id == "my-project"

def test_build_system_prompt_returns_string():
    sup = _make_supervisor()
    prompt = sup._build_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 0

def test_process_hook_llm_exists():
    sup = _make_supervisor()
    assert hasattr(sup, "process_hook_llm")
    assert callable(sup.process_hook_llm)

def test_process_hook_llm_sets_project():
    """process_hook_llm sets active project before processing."""
    sup = _make_supervisor()
    sup._provider = MagicMock()
    mock_resp = MagicMock()
    mock_resp.tool_uses = []
    mock_resp.text_parts = ["Hook processed successfully"]
    sup._provider.create_message = AsyncMock(return_value=mock_resp)

    result = asyncio.get_event_loop().run_until_complete(
        sup.process_hook_llm(
            hook_context="## Hook Context\nProject: test",
            rendered_prompt="Check tunnel status",
            project_id="my-game",
            hook_name="tunnel-monitor",
        )
    )
    assert "Hook processed" in result
    assert sup._active_project_id == "my-game"

def test_chat_triggers_reflection_on_tool_use():
    """After tool use, the Supervisor should attempt reflection."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    # First call: LLM uses a tool
    tool_use = MagicMock()
    tool_use.name = "create_task"
    tool_use.input = {"title": "Fix login"}
    tool_use.id = "tu-1"

    resp_with_tools = MagicMock()
    resp_with_tools.tool_uses = [tool_use]
    resp_with_tools.text_parts = []

    # Second call: LLM responds with text (after tool result)
    resp_text = MagicMock()
    resp_text.tool_uses = []
    resp_text.text_parts = ["Task created."]

    # Third call (reflection): LLM responds with text
    resp_reflect = MagicMock()
    resp_reflect.tool_uses = []
    resp_reflect.text_parts = ["Reflection: task verified."]

    sup._provider.create_message = AsyncMock(
        side_effect=[resp_with_tools, resp_text, resp_reflect]
    )

    # Mock tool execution
    sup.handler.execute = AsyncMock(return_value={"id": "t-123", "title": "Fix login"})

    result = asyncio.get_event_loop().run_until_complete(
        sup.chat("Create a task to fix login", "testuser")
    )
    assert "Task created" in result


def test_chat_skips_reflection_when_off():
    """When reflection level is off, no reflection pass happens."""
    sup = _make_supervisor()
    sup.reflection._config.level = "off"
    sup._provider = MagicMock()

    resp = MagicMock()
    resp.tool_uses = []
    resp.text_parts = ["Done."]
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.get_event_loop().run_until_complete(
        sup.chat("Hello", "testuser")
    )
    # Only 1 LLM call (no reflection)
    assert sup._provider.create_message.call_count == 1


def test_chat_no_reflection_for_simple_text():
    """No tool use = no reflection needed."""
    sup = _make_supervisor()
    sup._provider = MagicMock()

    resp = MagicMock()
    resp.tool_uses = []
    resp.text_parts = ["Hi there!"]
    sup._provider.create_message = AsyncMock(return_value=resp)

    result = asyncio.get_event_loop().run_until_complete(
        sup.chat("Hello", "testuser")
    )
    assert result == "Hi there!"
    assert sup._provider.create_message.call_count == 1
