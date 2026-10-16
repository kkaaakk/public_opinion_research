"""Bounded snapshot notifications; readers never own the workflow lifecycle."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any


class LiveBus:
    """Publish replaceable snapshots without blocking callbacks on a slow browser."""

    def __init__(self):
        """Keep only live subscribers; durable events remain in the ledger."""
        self.subscribers: set[asyncio.Queue] = set()
        self.closed = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: int | None = None

    def publish(self, packet: dict[str, Any]) -> None:
        """Coalesce overflow; a full snapshot restores any dropped notification."""
        if self.loop is not None and self.loop_thread != threading.get_ident():
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self._deliver, packet)
            return
        self._deliver(packet)

    def _deliver(self, packet: dict[str, Any]) -> None:
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(packet)

    def close(self) -> None:
        """Wake connected readers with a terminal marker."""
        self.closed = True
        self.publish({"type": "done"})

    async def listen(self, initial: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Subscribe before yielding the current snapshot, preventing a replay/live gap."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.subscribers.add(queue)
        try:
            yield initial
            if self.closed:
                yield {"type": "done"}
                return
            while True:
                try:
                    packet = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
                    continue
                yield packet
                if packet["type"] == "done":
                    return
        finally:
            self.subscribers.discard(queue)
