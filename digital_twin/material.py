from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MaterialState(str, Enum):
    IN_PROCESS = "in_process"
    WAITING = "waiting"
    SETUP = "setup"
    PENDING_INSPECTION = "pending_inspection"
    IN_TRANSIT = "in_transit"
    COMPLETED = "completed"
    SCRAPPED = "scrapped"


@dataclass
class HistoryEntry:
    """一次工位经历；返工不创建新物料，只在同一身份上追加记录。"""

    time: float
    station: str
    visit: int
    action: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "station": self.station,
            "visit": self.visit,
            "action": self.action,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            time=data["time"],
            station=data["station"],
            visit=data["visit"],
            action=data["action"],
            detail=data.get("detail", {}),
        )


@dataclass
class DispositionRecord:
    """抽检处置记录：pass / rework / scrap，作为后续操作的幂等依据。"""

    key: str
    time: float
    station: str
    visit: int
    result: str
    rework_count_after: int | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "time": self.time,
            "station": self.station,
            "visit": self.visit,
            "result": self.result,
            "rework_count_after": self.rework_count_after,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DispositionRecord":
        return cls(
            key=data["key"],
            time=data["time"],
            station=data["station"],
            visit=data["visit"],
            result=data["result"],
            rework_count_after=data.get("rework_count_after"),
        )


@dataclass
class Material:
    serial_number: str
    state: MaterialState
    location: str
    material_type: str = "standard"
    route_step: int = 0
    rework_count: int = 0
    release_time: float = 0.0
    visits: dict = field(default_factory=dict)
    history: list[HistoryEntry] = field(default_factory=list)

    def record(self, time: float, station: str, visit: int, action: str, **detail) -> None:
        self.history.append(
            HistoryEntry(time, station, visit, action, dict(detail))
        )

    def to_dict(self) -> dict:
        return {
            "serial_number": self.serial_number,
            "material_type": self.material_type,
            "state": self.state.value,
            "location": self.location,
            "route_step": self.route_step,
            "rework_count": self.rework_count,
            "release_time": self.release_time,
            "visits": dict(self.visits),
            "history": [entry.to_dict() for entry in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Material":
        return cls(
            serial_number=data["serial_number"],
            state=MaterialState(data["state"]),
            location=data["location"],
            material_type=data.get("material_type", "standard"),
            route_step=data.get("route_step", 0),
            rework_count=data.get("rework_count", 0),
            release_time=data.get("release_time", 0.0),
            visits=dict(data.get("visits", {})),
            history=[HistoryEntry.from_dict(item) for item in data.get("history", [])],
        )


@dataclass
class MaterialType:
    """一类物料的工艺定义：路线、节拍、准备时间、抽检工位与返工上限。"""

    name: str
    route: tuple = ()
    cycle_times: dict = field(default_factory=dict)
    setup_time: float = 0.0
    inspect_after: tuple = ()
    max_reworks: int = 0
    priority: int = 0
    due_time: float | None = None

    def cycle_time_for(self, station_name: str, default: float) -> float:
        return float(self.cycle_times.get(station_name, default))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "route": list(self.route),
            "cycle_times": dict(self.cycle_times),
            "setup_time": self.setup_time,
            "inspect_after": list(self.inspect_after),
            "max_reworks": self.max_reworks,
            "priority": self.priority,
            "due_time": self.due_time,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MaterialType":
        return cls(
            name=data["name"],
            route=tuple(data.get("route", ())),
            cycle_times=dict(data.get("cycle_times", {})),
            setup_time=data.get("setup_time", 0.0),
            inspect_after=tuple(data.get("inspect_after", ())),
            max_reworks=data.get("max_reworks", 0),
            priority=data.get("priority", 0),
            due_time=data.get("due_time"),
        )
