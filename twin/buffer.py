"""缓冲位（工位之间的有界队列）。

缓冲位本身不加工物料，只负责占用/释放容量；堵塞通过容量占满沿
“下游 → 上游”逐级传回。
"""

from typing import Any, Callable, Dict, List, Optional

from .material import Material


class Buffer:
    def __init__(
        self,
        buffer_id: str,
        capacity: int,
        on_space: Optional[Callable[[], None]] = None,
        on_material: Optional[Callable[[], None]] = None,
    ):
        if capacity <= 0:
            raise ValueError("缓冲容量必须为正")
        self.buffer_id = buffer_id
        self.capacity = int(capacity)
        self._slots: List[Material] = []
        # 出现空位时通知上游；有料时通知下游（支持多个监听者）
        self._space_listeners: List[Callable[[], None]] = []
        self._material_listeners: List[Callable[[], None]] = []
        if on_space:
            self._space_listeners.append(on_space)
        if on_material:
            self._material_listeners.append(on_material)

    @property
    def on_space_available(self):
        return self._space_listeners[0] if self._space_listeners else None

    @on_space_available.setter
    def on_space_available(self, callback):
        self._space_listeners.append(callback)

    @property
    def on_material_available(self):
        return self._material_listeners[0] if self._material_listeners else None

    @on_material_available.setter
    def on_material_available(self, callback):
        self._material_listeners.append(callback)

    @property
    def level(self) -> int:
        return len(self._slots)

    def is_full(self) -> bool:
        return len(self._slots) >= self.capacity

    def is_empty(self) -> bool:
        return not self._slots

    def can_put(self) -> bool:
        return not self.is_full()

    def put(self, material: Material) -> None:
        if self.is_full():
            raise RuntimeError(f"缓冲位 {self.buffer_id} 已满")
        self._slots.append(material)
        material.move(self.buffer_id)
        for cb in list(self._material_listeners):
            cb()

    def take(self) -> Material:
        if not self._slots:
            raise RuntimeError(f"缓冲位 {self.buffer_id} 已空")
        was_full = self.is_full()
        material = self._slots.pop(0)
        if was_full and self.on_space_available:
            for cb in list(self._space_listeners):
                cb()
        return material

    def peek(self) -> Optional[Material]:
        return self._slots[0] if self._slots else None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "buffer_id": self.buffer_id,
            "capacity": self.capacity,
            "slots": [m.material_id for m in self._slots],
        }
