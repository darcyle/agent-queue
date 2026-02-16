import asyncio
import pytest
from src.event_bus import EventBus


class TestEventBus:
    async def test_subscribe_and_emit(self):
        bus = EventBus()
        received = []
        bus.subscribe("task_completed", lambda data: received.append(data))
        await bus.emit("task_completed", {"task_id": "t-1"})
        assert len(received) == 1
        assert received[0]["task_id"] == "t-1"

    async def test_multiple_subscribers(self):
        bus = EventBus()
        received_a = []
        received_b = []
        bus.subscribe("test", lambda d: received_a.append(d))
        bus.subscribe("test", lambda d: received_b.append(d))
        await bus.emit("test", {"x": 1})
        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_no_cross_talk(self):
        bus = EventBus()
        received = []
        bus.subscribe("event_a", lambda d: received.append(d))
        await bus.emit("event_b", {"x": 1})
        assert len(received) == 0

    async def test_wildcard_subscriber(self):
        bus = EventBus()
        received = []
        bus.subscribe("*", lambda d: received.append(d))
        await bus.emit("anything", {"x": 1})
        await bus.emit("something_else", {"y": 2})
        assert len(received) == 2

    async def test_async_handler(self):
        bus = EventBus()
        received = []

        async def handler(data):
            await asyncio.sleep(0)
            received.append(data)

        bus.subscribe("test", handler)
        await bus.emit("test", {"x": 1})
        assert len(received) == 1
