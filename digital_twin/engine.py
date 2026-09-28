from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Event:
    uid: int
    time: float
    kind: str
    source: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "time": self.time,
            "kind": self.kind,
            "source": self.source,
            "payload": self.payload,
        }


EventHandler = Callable[[Event], None]


class EventQueue:
    def __init__(self) -> None:
        self._next_uid = 1
        self._heap: list[tuple[float, int]] = []
        self._events: dict[int, Event] = {}

    def schedule(
        self,
        time: float,
        kind: str,
        source: str,
        payload: dict | None = None,
    ) -> int:
        uid = self._next_uid
        self._next_uid += 1
        event = Event(uid, time, kind, source, payload or {})
        self._events[uid] = event
        heapq.heappush(self._heap, (event.time, event.uid))
        return uid

    def cancel(self, uid: int) -> None:
        self._events.pop(uid, None)

    def get(self, uid: int) -> Event | None:
        return self._events.get(uid)

    def next_time(self) -> float | None:
        while self._heap:
            time, uid = self._heap[0]
            if uid in self._events:
                return time
            heapq.heappop(self._heap)
        return None

    def pop(self) -> Event | None:
        while self._heap:
            _, uid = heapq.heappop(self._heap)
            event = self._events.pop(uid, None)
            if event is not None:
                return event
        return None

    def to_dict(self) -> dict:
        return {
            "next_uid": self._next_uid,
            "pending": [event.to_dict() for event in self._events.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EventQueue":
        queue = cls()
        queue._next_uid = data["next_uid"]
        for item in data["pending"]:
            event = Event(
                uid=item["uid"],
                time=item["time"],
                kind=item["kind"],
                source=item["source"],
                payload=item.get("payload", {}),
            )
            queue._events[event.uid] = event
            heapq.heappush(queue._heap, (event.time, event.uid))
        return queue


class SimulationClock:
    def __init__(self, initial_time: float = 0.0) -> None:
        self.time = float(initial_time)

    def advance(self, time: float) -> None:
        if time < self.time:
            raise ValueError("simulation time cannot move backwards")
        self.time = float(time)

    def to_dict(self) -> dict:
        return {"time": self.time}

    @classmethod
    def from_dict(cls, data: dict) -> "SimulationClock":
        return cls(data["time"])
