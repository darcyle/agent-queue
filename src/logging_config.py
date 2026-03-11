"""Structured logging with correlation IDs for per-task tracing.

Provides a ``CorrelationContext`` (backed by Python's ``contextvars``) that
carries task_id, project_id, and agent_id through the call stack.  A
``StructuredFormatter`` automatically injects these IDs into every log
record, enabling easy filtering of log output by task.

Usage::

    from src.logging_config import setup_logging, correlation_context

    setup_logging(level="INFO", json_output=False)

    async with correlation_context(task_id="swift-river", project_id="my-proj"):
        logger.info("Starting task execution")
        # All log lines inside this block include the correlation IDs

For code that processes a specific task over a long period (e.g. the
orchestrator's ``_execute_task``), use ``TaskLogAdapter`` to automatically
tag every log call::

    from src.logging_config import TaskLogAdapter

    task_logger = TaskLogAdapter(logger, task_id="swift-river", project_id="my-proj")
    task_logger.info("Agent started")  # automatically includes task_id
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Correlation context — stored per-asyncio-task via contextvars
# ---------------------------------------------------------------------------

_ctx_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_task_id", default=None
)
_ctx_project_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_project_id", default=None
)
_ctx_agent_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_agent_id", default=None
)


class CorrelationContext:
    """Read-only accessor for the current correlation IDs.

    Use ``correlation_context()`` (the context manager) to set values.
    This class is used by the formatter to read them.
    """

    @staticmethod
    def task_id() -> str | None:
        return _ctx_task_id.get()

    @staticmethod
    def project_id() -> str | None:
        return _ctx_project_id.get()

    @staticmethod
    def agent_id() -> str | None:
        return _ctx_agent_id.get()

    @staticmethod
    def as_dict() -> dict[str, str]:
        """Return non-None correlation IDs as a dict."""
        result: dict[str, str] = {}
        task_id = _ctx_task_id.get()
        if task_id:
            result["task_id"] = task_id
        project_id = _ctx_project_id.get()
        if project_id:
            result["project_id"] = project_id
        agent_id = _ctx_agent_id.get()
        if agent_id:
            result["agent_id"] = agent_id
        return result


@contextlib.asynccontextmanager
async def correlation_context(
    task_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
):
    """Async context manager that sets correlation IDs for the current task.

    IDs are automatically cleared when the context exits. Nesting is safe —
    inner contexts override outer ones and restore the previous values on exit.

    Example::

        async with correlation_context(task_id="swift-river"):
            logger.info("Processing task")  # log includes task_id
    """
    old_task = _ctx_task_id.get()
    old_project = _ctx_project_id.get()
    old_agent = _ctx_agent_id.get()

    if task_id is not None:
        _ctx_task_id.set(task_id)
    if project_id is not None:
        _ctx_project_id.set(project_id)
    if agent_id is not None:
        _ctx_agent_id.set(agent_id)

    try:
        yield
    finally:
        _ctx_task_id.set(old_task)
        _ctx_project_id.set(old_project)
        _ctx_agent_id.set(old_agent)


# ---------------------------------------------------------------------------
# Structured formatter — JSON lines or human-readable
# ---------------------------------------------------------------------------

class StructuredFormatter(logging.Formatter):
    """Log formatter that outputs either JSON lines or human-readable text.

    In JSON mode, each log line is a self-contained JSON object with:
    - ``timestamp``: ISO 8601 UTC
    - ``level``: log level name
    - ``logger``: logger name
    - ``message``: formatted message
    - ``task_id``, ``project_id``, ``agent_id``: correlation IDs (if set)
    - Any extra fields passed via ``extra={}``

    In human-readable mode (the default), the format is::

        2024-01-15T12:34:56Z INFO  [task:swift-river] orchestrator: Starting execution
    """

    def __init__(self, json_output: bool = False):
        super().__init__()
        self._json_output = json_output

    def format(self, record: logging.LogRecord) -> str:
        if self._json_output:
            return self._format_json(record)
        return self._format_human(record)

    def _format_json(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add correlation IDs
        correlation = CorrelationContext.as_dict()
        if correlation:
            entry.update(correlation)

        # Add extra fields (skip internal LogRecord attributes)
        _internal = {
            "name", "msg", "args", "created", "relativeCreated",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "filename", "module", "pathname", "levelno", "levelname",
            "thread", "threadName", "process", "processName",
            "msecs", "message", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in _internal and not key.startswith("_"):
                entry[key] = value

        # Add exception info if present
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str, ensure_ascii=False)

    def _format_human(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        level = record.levelname.ljust(5)

        # Build correlation tag
        parts: list[str] = []
        task_id = CorrelationContext.task_id()
        if task_id:
            parts.append(f"task:{task_id}")
        project_id = CorrelationContext.project_id()
        if project_id:
            parts.append(f"proj:{project_id}")
        agent_id = CorrelationContext.agent_id()
        if agent_id:
            parts.append(f"agent:{agent_id}")

        # Also check record's extra fields
        if not task_id and hasattr(record, "task_id") and record.task_id:
            parts.append(f"task:{record.task_id}")
        if not project_id and hasattr(record, "project_id") and record.project_id:
            parts.append(f"proj:{record.project_id}")

        tag = f" [{','.join(parts)}]" if parts else ""

        message = record.getMessage()

        line = f"{ts} {level}{tag} {record.name}: {message}"

        if record.exc_info and record.exc_info[1]:
            line += "\n" + self.formatException(record.exc_info)

        return line


# ---------------------------------------------------------------------------
# Task-specific log adapter
# ---------------------------------------------------------------------------

class TaskLogAdapter(logging.LoggerAdapter):
    """Logger adapter that automatically includes task correlation IDs.

    Use this in code that handles a specific task over multiple log calls::

        task_logger = TaskLogAdapter(logger, task_id="swift-river", project_id="my-proj")
        task_logger.info("Agent started")
        task_logger.info("Files changed: %d", len(files))
    """

    def __init__(
        self,
        logger: logging.Logger,
        task_id: str = "",
        project_id: str = "",
        agent_id: str = "",
    ):
        extra = {}
        if task_id:
            extra["task_id"] = task_id
        if project_id:
            extra["project_id"] = project_id
        if agent_id:
            extra["agent_id"] = agent_id
        super().__init__(logger, extra)

    def process(
        self, msg: str, kwargs: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        # Merge our extra fields into the log record's extra
        extra = kwargs.get("extra", {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs


# ---------------------------------------------------------------------------
# Setup function
# ---------------------------------------------------------------------------

def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
) -> None:
    """Configure the root logger with structured formatting.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: If True, output JSON lines. Otherwise human-readable.

    This function:
    1. Sets the root logger level
    2. Removes any existing handlers
    3. Adds a stderr handler with the StructuredFormatter
    4. Silences noisy third-party loggers (discord, aiohttp, aiosqlite)
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates on re-init
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Console handler with structured formatter
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(StructuredFormatter(json_output=json_output))
    root.addHandler(console)

    # Silence noisy third-party loggers
    for name in ("discord", "discord.http", "discord.gateway",
                 "aiohttp", "aiosqlite", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
