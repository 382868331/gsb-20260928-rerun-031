from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable, Optional

from .material import Material, MaterialState


class DeviceState(str, Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    BLOCKED = "blocked"
    DOWN = "down"


class MaterialProvider(ABC):
    name: str

    @abstractmethod
    def can_take(self) -> bool: ...

    @abstractmethod
    def take(self) -> Material: ...


class MaterialReceiver(ABC):
    name: str

    @abstractmethod
    def can_accept(self) -> bool: ...

    @abstractmethod
    def receive(self, material: Material) -> None: ...


class MaterialSource(MaterialProvider):
    def __init__(self, name: str = "source") -> None:
        self.name = name
        self._next_number = 1

    def can_take(self) -> bool:
        return True

    def take(self) -> Material:
        serial = f"M-{self._next_number:04d}"
        self._next_number += 1
        return Material(serial, MaterialState.IN_PROCESS, self.name)

    @property
    def released(self) -> int:
        return self._next_number - 1

    def to_dict(self) -> dict:
        return {"name": self.name, "next_number": self._next_number}

    @classmethod
    def from_dict(cls, data: dict) -> "MaterialSource":
        source = cls(data["name"])
        source._next_number = data["next_number"]
        return source


class Buffer(MaterialProvider, MaterialReceiver):
    def __init__(self, name: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("buffer capacity must be positive")
        self.name = name
        self.capacity = capacity
        self.items: list[Material] = []

    def can_accept(self) -> bool:
        return len(self.items) < self.capacity

    def can_take(self) -> bool:
        return bool(self.items)

    def receive(self, material: Material) -> None:
        if not self.can_accept():
            raise RuntimeError(f"{self.name} is full")
        material.state = MaterialState.WAITING
        material.location = self.name
        self.items.append(material)

    def take(self) -> Material:
        if not self.items:
            raise RuntimeError(f"{self.name} is empty")
        return self.items.pop(0)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Buffer":
        buffer = cls(data["name"], data["capacity"])
        buffer.items = [Material.from_dict(item) for item in data["items"]]
        return buffer


class Sink(MaterialReceiver):
    def __init__(self, name: str = "sink") -> None:
        self.name = name
        self.accepted = 0

    def can_accept(self) -> bool:
        return True

    def receive(self, material: Material) -> None:
        material.state = MaterialState.COMPLETED
        material.location = self.name
        self.accepted += 1

    def to_dict(self) -> dict:
        return {"name": self.name, "accepted": self.accepted}

    @classmethod
    def from_dict(cls, data: dict) -> "Sink":
        sink = cls(data["name"])
        sink.accepted = data["accepted"]
        return sink


class Station:
    def __init__(self, name: str, cycle_time: float) -> None:
        if cycle_time <= 0:
            raise ValueError("cycle time must be positive")
        self.name = name
        self.cycle_time = float(cycle_time)
        self.state = DeviceState.IDLE
        self.material: Optional[Material] = None
        self.remaining_time: Optional[float] = None
        self.completion_event: Optional[int] = None

    @property
    def is_idle(self) -> bool:
        return self.state == DeviceState.IDLE and self.material is None

    def start(
        self,
        upstream: MaterialProvider,
        now: float,
        schedule_event: Callable[..., int],
    ) -> None:
        material = upstream.take()
        material.state = MaterialState.IN_PROCESS
        material.location = self.name
        self.material = material
        self.remaining_time = self.cycle_time
        self.state = DeviceState.PROCESSING
        self.completion_event = schedule_event(
            now + self.cycle_time,
            "PROCESS_COMPLETE",
            self.name,
            {"material": material.serial_number},
        )

    def complete(self) -> None:
        self.completion_event = None
        self.remaining_time = 0.0
        self.state = DeviceState.BLOCKED

    def fail(
        self,
        completion_time: Optional[float],
        now: float,
        cancel_event: Callable[[int], None],
        scrap_current: bool,
    ) -> Optional[Material]:
        if self.completion_event is not None:
            cancel_event(self.completion_event)
            self.completion_event = None
            if completion_time is not None:
                self.remaining_time = max(0.0, completion_time - now)
        scrapped = None
        if scrap_current and self.material is not None:
            scrapped = self.material
            scrapped.state = MaterialState.SCRAPPED
            scrapped.location = "scrap"
            self.material = None
            self.remaining_time = None
        self.state = DeviceState.DOWN
        return scrapped

    def repair(
        self,
        now: float,
        downstream: MaterialReceiver,
        schedule_event: Callable[..., int],
    ) -> None:
        if self.material is None:
            self.state = DeviceState.IDLE
            self.remaining_time = None
            return
        if self.remaining_time is not None and self.remaining_time > 0:
            self.state = DeviceState.PROCESSING
            self.completion_event = schedule_event(
                now + self.remaining_time,
                "PROCESS_COMPLETE",
                self.name,
                {"material": self.material.serial_number},
            )
            return
        self.state = DeviceState.BLOCKED

    def try_release(self, downstream: MaterialReceiver) -> bool:
        if self.material is None or self.state == DeviceState.DOWN:
            return False
        if not downstream.can_accept():
            self.state = DeviceState.BLOCKED
            return False
        downstream.receive(self.material)
        self.material = None
        self.remaining_time = None
        self.completion_event = None
        self.state = DeviceState.IDLE
        return True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cycle_time": self.cycle_time,
            "state": self.state.value,
            "material": self.material.to_dict() if self.material else None,
            "remaining_time": self.remaining_time,
            "completion_event": self.completion_event,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Station":
        station = cls(data["name"], data["cycle_time"])
        station.state = DeviceState(data["state"])
        station.material = Material.from_dict(data["material"]) if data["material"] else None
        station.remaining_time = data["remaining_time"]
        station.completion_event = data["completion_event"]
        return station
