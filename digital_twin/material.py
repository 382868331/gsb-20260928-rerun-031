from __future__ import annotations

from dataclasses import dataclass
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

    def to_dict(self) -> dict:
        return {
            "serial_number": self.serial_number,
            "state": self.state.value,
            "location": self.location,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Material":
        return cls(
            serial_number=data["serial_number"],
            state=MaterialState(data["state"]),
            location=data["location"],
        )
