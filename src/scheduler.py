"""Proportional fair-share scheduler for assigning tasks to idle agents.

Uses a purely deterministic algorithm -- zero LLM calls. Every token budget
is spent on agent work, not on deciding *which* work to do.

The scheduling algorithm runs in two phases each time an idle agent needs
a task:

1. **Min-task guarantee** -- Projects that have completed zero tasks in the
   current scheduling window are prioritized first.  This ensures every
   active project gets at least one task assigned before proportional
   allocation kicks in.

2. **Deficit-based proportional allocation** -- Among projects that already
   have at least one completion, the scheduler picks the project whose
   actual token usage ratio is furthest *below* its target ratio (derived
   from ``credit_weight``).  This gradually converges each project toward
   its fair share of total agent time.

Both phases respect per-project concurrency limits (``max_concurrent_agents``)
and per-project / global budget caps.

See specs/scheduler-and-budget.md for the full specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.models import (
    Agent, AgentState, Project, ProjectStatus, Task, TaskStatus,
)


@dataclass
class AssignAction:
    """A scheduling decision: assign one specific task to one specific agent.

    This is the output type of the scheduler -- a list of these actions is
    returned each scheduling round, one per idle agent that received work.
    The orchestrator is responsible for actually executing the assignment
    (updating the database, starting the agent process, etc.).
    """
    agent_id: str
    task_id: str
    project_id: str


@dataclass
class SchedulerState:
    """A snapshot of all system state the scheduler needs to make decisions.

    The scheduler is a pure function: given a SchedulerState, it returns a
    list of AssignActions with no side effects.  This stateless/functional
    design makes the algorithm easy to test and reason about -- the
    orchestrator builds this snapshot each tick, and the scheduler never
    touches the database or any external resource.
    """

    projects: list[Project]
    tasks: list[Task]
    agents: list[Agent]
    project_token_usage: dict[str, int]  # project_id -> tokens in window
    project_active_agent_counts: dict[str, int]  # project_id -> count
    tasks_completed_in_window: dict[str, int]  # project_id -> count
    project_available_workspaces: dict[str, int] = field(default_factory=dict)
    global_budget: int | None = None
    global_tokens_used: int = 0


class Scheduler:
    @staticmethod
    def schedule(state: SchedulerState) -> list[AssignAction]:
        """Assign READY tasks to idle agents using proportional fair-share.

        Algorithm steps:
        1. Bail out early if the global token budget is exhausted.
        2. Collect idle agents and group READY tasks by project.
        3. For each idle agent (in order), rank active projects by:
           a. Min-task guarantee -- projects with zero completions in the
              window sort first (phase 1).
           b. Deficit -- among the rest, the project whose actual token
              usage is furthest below its ``credit_weight`` share sorts
              first (phase 2).
        4. Walk the ranked project list; skip any project that has hit its
           budget cap or concurrency limit.  Pick the highest-priority
           READY task from the first eligible project.
        5. Record the assignment and move to the next idle agent.

        Returns a list of :class:`AssignAction` -- one per agent that was
        matched with a task.  May be empty if no work can be assigned.
        """
        # Check global budget
        if (
            state.global_budget is not None
            and state.global_tokens_used >= state.global_budget
        ):
            return []

        idle_agents = [a for a in state.agents if a.state == AgentState.IDLE]
        if not idle_agents:
            return []

        # Group ready tasks by project
        ready_by_project: dict[str, list[Task]] = {}
        for task in state.tasks:
            if task.status == TaskStatus.READY:
                ready_by_project.setdefault(task.project_id, []).append(task)

        # Sort tasks within each project by priority then creation order (id as proxy)
        for tasks in ready_by_project.values():
            tasks.sort(key=lambda t: (t.priority, t.id))

        # Filter to active projects with ready tasks
        active_projects = [
            p for p in state.projects
            if p.status == ProjectStatus.ACTIVE and p.id in ready_by_project
        ]
        if not active_projects:
            return []

        # Calculate totals for proportional ratio computation.
        # ``total_weight`` is the denominator for target ratios (each
        # project's target = credit_weight / total_weight).
        # ``total_tokens`` is the denominator for actual ratios (each
        # project's actual = tokens_used / total_tokens).
        # We clamp total_tokens to at least 1 to avoid division by zero
        # during the first scheduling round before any tokens are used.
        total_weight = sum(p.credit_weight for p in active_projects)
        total_tokens = sum(state.project_token_usage.values()) or 1  # avoid div/0

        # Track assignments made in this scheduling round.  These sets
        # prevent double-assignment: an agent or task matched once won't be
        # considered again in the same round.  ``round_agent_counts`` is a
        # mutable copy of the live counts so that assignments within this
        # round are reflected in subsequent concurrency-limit checks.
        actions: list[AssignAction] = []
        assigned_agents: set[str] = set()
        assigned_tasks: set[str] = set()
        round_agent_counts: dict[str, int] = dict(state.project_active_agent_counts)

        for agent in idle_agents:
            if agent.id in assigned_agents:
                continue

            # Sort projects by scheduling priority using a two-level key:
            #
            # Level 1 — Min-task guarantee (binary):
            #   Projects with zero completions in the window sort first
            #   (has_guarantee=0).  This ensures starvation prevention:
            #   every active project gets at least one task before
            #   proportional allocation kicks in.
            #
            # Level 2 — Deficit score (continuous):
            #   Among projects at the same guarantee level, the one whose
            #   actual token usage ratio is furthest *below* its target
            #   ratio (derived from credit_weight) sorts first.  A negative
            #   deficit means the project is under-served relative to its
            #   weight; a positive deficit means over-served.
            #
            # Together these produce a fair ordering: starved projects go
            # first, then under-served projects, then over-served ones.
            def project_sort_key(p: Project) -> tuple[int, float]:
                completed = state.tasks_completed_in_window.get(p.id, 0)
                has_guarantee = 1 if completed > 0 else 0  # 0 = needs guarantee (sorts first)
                target_ratio = p.credit_weight / total_weight
                actual_ratio = state.project_token_usage.get(p.id, 0) / total_tokens
                deficit = actual_ratio - target_ratio  # negative = below target
                return (has_guarantee, deficit)

            sorted_projects = sorted(active_projects, key=project_sort_key)

            for project in sorted_projects:
                # Check per-project budget
                if (
                    project.budget_limit is not None
                    and state.project_token_usage.get(project.id, 0)
                    >= project.budget_limit
                ):
                    continue

                # Check concurrency limit
                current_agents = round_agent_counts.get(project.id, 0)
                if current_agents >= project.max_concurrent_agents:
                    continue

                # Skip projects with no available workspaces
                if (
                    state.project_available_workspaces
                    and state.project_available_workspaces.get(project.id, 0) <= 0
                ):
                    continue

                # Pick highest priority ready task not yet assigned
                available = [
                    t for t in ready_by_project.get(project.id, [])
                    if t.id not in assigned_tasks
                ]
                if not available:
                    continue

                task = available[0]
                actions.append(AssignAction(
                    agent_id=agent.id,
                    task_id=task.id,
                    project_id=project.id,
                ))
                assigned_agents.add(agent.id)
                assigned_tasks.add(task.id)
                round_agent_counts[project.id] = current_agents + 1
                break

        return actions
