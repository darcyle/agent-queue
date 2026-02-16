from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from typing import Any, Callable


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    async def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        data["_event_type"] = event_type
        handlers = list(self._handlers.get(event_type, []))
        handlers.extend(self._handlers.get("*", []))
        for handler in handlers:
            if inspect.iscoroutinefunction(handler):
                await handler(data)
            else:
                handler(data)
