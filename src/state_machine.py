from __future__ import annotations

from src.models import TaskStatus, TaskEvent


class InvalidTransition(Exception):
    def __init__(self, state: TaskStatus, event: TaskEvent):
        self.state = state
        self.event = event
        super().__init__(f"Invalid transition: ({state.value}, {event.value})")


VALID_TASK_TRANSITIONS: dict[tuple[TaskStatus, TaskEvent], TaskStatus] = {
    (TaskStatus.DEFINED, TaskEvent.DEPS_MET): TaskStatus.READY,
    (TaskStatus.READY, TaskEvent.ASSIGNED): TaskStatus.ASSIGNED,
    (TaskStatus.ASSIGNED, TaskEvent.AGENT_STARTED): TaskStatus.IN_PROGRESS,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_COMPLETED): TaskStatus.VERIFYING,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_FAILED): TaskStatus.FAILED,
    (TaskStatus.IN_PROGRESS, TaskEvent.TOKENS_EXHAUSTED): TaskStatus.PAUSED,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_QUESTION): TaskStatus.WAITING_INPUT,
    (TaskStatus.WAITING_INPUT, TaskEvent.HUMAN_REPLIED): TaskStatus.IN_PROGRESS,
    (TaskStatus.WAITING_INPUT, TaskEvent.INPUT_TIMEOUT): TaskStatus.PAUSED,
    (TaskStatus.PAUSED, TaskEvent.RESUME_TIMER): TaskStatus.READY,
    (TaskStatus.VERIFYING, TaskEvent.VERIFY_PASSED): TaskStatus.COMPLETED,
    (TaskStatus.VERIFYING, TaskEvent.VERIFY_FAILED): TaskStatus.FAILED,
    (TaskStatus.FAILED, TaskEvent.RETRY): TaskStatus.READY,
    (TaskStatus.FAILED, TaskEvent.MAX_RETRIES): TaskStatus.BLOCKED,
}


def task_transition(current: TaskStatus, event: TaskEvent) -> TaskStatus:
    key = (current, event)
    if key not in VALID_TASK_TRANSITIONS:
        raise InvalidTransition(current, event)
    return VALID_TASK_TRANSITIONS[key]


class CyclicDependencyError(Exception):
    def __init__(self, cycle: list[str] | None = None):
        msg = "Cyclic dependency detected"
        if cycle:
            msg += f": {' -> '.join(cycle)}"
        super().__init__(msg)


def validate_dag(deps: dict[str, set[str]]) -> None:
    """Validate that the dependency graph is a DAG (no cycles). Uses DFS."""
    WHITE, GRAY, BLACK = 0, 1, 2
    all_nodes = set(deps.keys())
    for targets in deps.values():
        all_nodes.update(targets)

    color: dict[str, int] = {n: WHITE for n in all_nodes}

    def dfs(node: str) -> None:
        color[node] = GRAY
        for dep in deps.get(node, set()):
            if color[dep] == GRAY:
                raise CyclicDependencyError([node, dep])
            if color[dep] == WHITE:
                dfs(dep)
        color[node] = BLACK

    for node in all_nodes:
        if color[node] == WHITE:
            dfs(node)


def validate_dag_with_new_edge(
    deps: dict[str, set[str]], task_id: str, depends_on: str
) -> None:
    """Validate that adding a new edge doesn't create a cycle."""
    new_deps = {k: set(v) for k, v in deps.items()}
    new_deps.setdefault(task_id, set()).add(depends_on)
    validate_dag(new_deps)
