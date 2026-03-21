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
