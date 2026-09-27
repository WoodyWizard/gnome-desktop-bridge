"""Bounded in-memory event log used for auditing and real-time diagnostics."""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventBuffer:
    def __init__(self, max_events: int = 2000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._next_id = 1
        self._condition = threading.Condition()
        self._subscribers: Counter[str] = Counter()

    def resize(self, max_events: int) -> None:
        with self._condition:
            if self._events.maxlen == max_events:
                return
            self._events = deque(self._events, maxlen=max_events)

    def emit(
        self,
        event_type: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._condition:
            event = {
                "id": self._next_id,
                "timestamp": _utc_now(),
                "monotonic": round(time.monotonic(), 6),
                "type": event_type,
                "level": level,
                "data": deepcopy(data or {}),
            }
            self._next_id += 1
            self._events.append(event)
            self._condition.notify_all()
        return deepcopy(event)

    def after(self, event_id: int = 0, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._condition:
            selected = [event for event in self._events if event["id"] > event_id]
            return deepcopy(selected[:limit])

    def wait_after(
        self,
        event_id: int,
        *,
        timeout: float = 20.0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not any(event["id"] > event_id for event in self._events):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)
            selected = [event for event in self._events if event["id"] > event_id]
            return deepcopy(selected[: max(1, min(limit, 1000))])

    def latest_id(self) -> int:
        with self._condition:
            return self._next_id - 1

    @contextmanager
    def subscribed(self, kind: str) -> Iterator[None]:
        """Count a live stream consumer, e.g. the on-screen overlay."""

        with self._condition:
            self._subscribers[kind] += 1
        try:
            yield
        finally:
            with self._condition:
                self._subscribers[kind] -= 1
                if self._subscribers[kind] <= 0:
                    del self._subscribers[kind]

    def subscriber_count(self, kind: str) -> int:
        with self._condition:
            return self._subscribers.get(kind, 0)
