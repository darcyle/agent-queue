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
    format_pr_created,
    format_chain_stuck,
    format_stuck_defined_task,
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
        assert "p-1" in result  # project context
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
        assert "p-1" in result  # project context
        assert "1/3" in result
        assert "Syntax error" in result

    def test_format_task_blocked(self):
        task = Task(id="t-1", project_id="p-1", title="Stuck",
                    description="D", max_retries=3)
        result = format_task_blocked(task)
        assert "Blocked" in result
        assert "p-1" in result  # project context
        assert "Manual intervention" in result

    def test_format_agent_question(self):
        task = Task(id="t-1", project_id="p-1", title="Auth", description="D")
        agent = Agent(id="a-1", name="claude-1", agent_type="claude")
        result = format_agent_question(task, agent, "Which auth provider?")
        assert "Question" in result
        assert "p-1" in result  # project context
        assert "claude-1" in result  # agent name
        assert "Which auth provider?" in result

    def test_format_budget_warning(self):
        result = format_budget_warning("alpha", 80000, 100000)
        assert "80%" in result
        assert "alpha" in result

    def test_format_pr_created_includes_project(self):
        task = Task(id="t-1", project_id="p-1", title="Add feature", description="D")
        result = format_pr_created(task, "https://github.com/org/repo/pull/42")
        assert "PR Created" in result
        assert "p-1" in result  # project context
        assert "https://github.com/org/repo/pull/42" in result

    def test_format_chain_stuck_includes_project(self):
        blocked = Task(id="t-1", project_id="p-1", title="Blocker", description="D",
                       status=TaskStatus.BLOCKED)
        stuck = [
            Task(id="t-2", project_id="p-1", title="Downstream 1", description="D",
                 status=TaskStatus.DEFINED),
        ]
        result = format_chain_stuck(blocked, stuck)
        assert "Chain Stuck" in result
        assert "p-1" in result  # project context
        assert "t-2" in result

    def test_format_stuck_defined_task_includes_project(self):
        task = Task(id="t-1", project_id="p-1", title="Waiting", description="D",
                    status=TaskStatus.DEFINED)
        blocking = [("t-0", "Blocker", "BLOCKED")]
        result = format_stuck_defined_task(task, blocking, stuck_hours=3.5)
        assert "Stuck Task" in result
        assert "p-1" in result  # project context
        assert "3.5 hours" in result


class TestProjectContextPrefixing:
    """Verify the bot's static _prepend_project_tag helper."""

    def test_prepend_project_tag(self):
        from src.discord.bot import AgentQueueBot
        result = AgentQueueBot._prepend_project_tag("**Task Started:** `t-1`", "my-project")
        assert result == "[`my-project`] **Task Started:** `t-1`"


