from __future__ import annotations

from dataclasses import dataclass, field

from src.models import (
    Agent, AgentState, Project, ProjectStatus, Task, TaskStatus,
)


@dataclass
class AssignAction:
    agent_id: str
    task_id: str
    project_id: str


@dataclass
class SchedulerState:
    projects: list[Project]
    tasks: list[Task]
    agents: list[Agent]
    project_token_usage: dict[str, int]  # project_id -> tokens in window
    project_active_agent_counts: dict[str, int]  # project_id -> count
    tasks_completed_in_window: dict[str, int]  # project_id -> count
    global_budget: int | None = None
    global_tokens_used: int = 0


class Scheduler:
    @staticmethod
    def schedule(state: SchedulerState) -> list[AssignAction]:
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

        # Calculate total weight
        total_weight = sum(p.credit_weight for p in active_projects)
        total_tokens = sum(state.project_token_usage.values()) or 1  # avoid div/0

        # Track assignments made in this scheduling round
        actions: list[AssignAction] = []
        assigned_agents: set[str] = set()
        assigned_tasks: set[str] = set()
        round_agent_counts: dict[str, int] = dict(state.project_active_agent_counts)

        for agent in idle_agents:
            if agent.id in assigned_agents:
                continue

            # Sort projects: min-task-guarantee first, then by deficit
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
