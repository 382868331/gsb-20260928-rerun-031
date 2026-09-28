"""物料（在制品）实体。"""

from typing import Any, Dict, Optional

# 物料生命周期状态
WIP = "wip"          # 在制
FINISHED = "finished"  # 已产出
SCRAPPED = "scrapped"  # 已报废


class Material:
    def __init__(self, material_id: str, created_at: float):
        self.material_id = material_id
        self.created_at = float(created_at)
        self.status = WIP
        self.finished_at: Optional[float] = None
        self.scrapped_at: Optional[float] = None
        self.scrap_reason: Optional[str] = None
        self.location: str = "source"
        self.route: list = ["source"]  # 经过的工位/缓冲，便于追溯

    def mark_finished(self, time: float) -> None:
        if self.status != WIP:
            raise ValueError(f"物料 {self.material_id} 状态为 {self.status}，不能产出")
        self.status = FINISHED
        self.finished_at = float(time)

    def mark_scrapped(self, time: float, reason: str) -> None:
        if self.status != WIP:
            raise ValueError(f"物料 {self.material_id} 状态为 {self.status}，不能报废")
        self.status = SCRAPPED
        self.scrapped_at = float(time)
        self.scrap_reason = reason

    def move(self, location: str) -> None:
        self.location = location
        self.route.append(location)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "material_id": self.material_id,
            "created_at": self.created_at,
            "status": self.status,
            "finished_at": self.finished_at,
            "scrapped_at": self.scrapped_at,
            "scrap_reason": self.scrap_reason,
            "location": self.location,
            "route": list(self.route),
        }

    @classmethod
    def restore(cls, state: Dict[str, Any]) -> "Material":
        m = cls(state["material_id"], state["created_at"])
        m.status = state["status"]
        m.finished_at = state["finished_at"]
        m.scrapped_at = state["scrapped_at"]
        m.scrap_reason = state["scrap_reason"]
        m.location = state["location"]
        m.route = list(state["route"])
        return m
