import pytest
from src.models import (
    Task, Agent, AgentOutput, AgentResult, TaskStatus, AgentState,
)
from src.discord.notifications import (
    format_task_completed,
    format_task_failed,
    format_task_blocked,
    format_agent_question,
    format_budget_warning,
)

class TestNotificationFormatting:
    def test_format_task_completed(self):
        task = Task(id="t-1", project_id="p-1", title="Fix bug", description="D")
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        output = AgentOutput(result=AgentResult.COMPLETED, summary="Fixed it",
                             tokens_used=5000, files_changed=["src/foo.py"])
        result = format_task_completed(task, agent, output)
        assert "t-1" in result
        assert "Fix bug" in result
        assert "claude-1" in result
        assert "5,000" in result
        assert "src/foo.py" in result

    def test_format_task_failed(self):
        task = Task(id="t-1", project_id="p-1", title="Broken",
                    description="D", retry_count=1, max_retries=3)
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        output = AgentOutput(result=AgentResult.FAILED,
                             error_message="Syntax error in foo.py")
        result = format_task_failed(task, agent, output)
        assert "Failed" in result
        assert "1/3" in result
        assert "Syntax error" in result

    def test_format_task_blocked(self):
        task = Task(id="t-1", project_id="p-1", title="Stuck",
                    description="D", max_retries=3)
        result = format_task_blocked(task)
        assert "Blocked" in result
        assert "Manual intervention" in result

    def test_format_agent_question(self):
        task = Task(id="t-1", project_id="p-1", title="Auth", description="D")
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        result = format_agent_question(task, agent, "Which auth provider?")
        assert "Question" in result
        assert "Which auth provider?" in result

    def test_format_budget_warning(self):
        result = format_budget_warning("alpha", 80000, 100000)
        assert "80%" in result
        assert "alpha" in result


