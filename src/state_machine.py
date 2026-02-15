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
