from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MaterialState(str, Enum):
    IN_PROCESS = "in_process"
    WAITING = "waiting"
    COMPLETED = "completed"
    SCRAPPED = "scrapped"


@dataclass
class Material:
    serial_number: str
    state: MaterialState
    location: str
    material_type: str = "default"
    rework_count: int = 0
    released: bool = False
    history: list[dict] = field(default_factory=list)

    def record(self, time: float, event: str, location: str, **detail) -> None:
        entry = {"time": float(time), "event": event, "location": location}
        entry.update(detail)
        self.history.append(entry)

    def to_dict(self) -> dict:
        return {
            "serial_number": self.serial_number,
            "state": self.state.value,
            "location": self.location,
            "material_type": self.material_type,
            "rework_count": self.rework_count,
            "released": self.released,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Material":
        material = cls(
            serial_number=data["serial_number"],
            state=MaterialState(data["state"]),
            location=data["location"],
            material_type=data.get("material_type", "default"),
            rework_count=data.get("rework_count", 0),
            released=data.get("released", True),
        )
        material.history = list(data.get("history", []))
        return material
