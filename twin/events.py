"""事件调度内核与统一事件约定。

事件是一条不可变约定::

    {"time": 浮点数时刻, "type": 事件名, "target": 设备标识, "data": 参数字典}

设备只按事件名处理自己关心的事件，不直接调用其他设备；跨设备协同通过
事件/唤醒信号完成，从而保证新设备可以按同一约定热插拔。
"""

import heapq
import itertools
from typing import Any, Callable, Dict, List, Optional, Tuple

EventHandler = Callable[[str, str, Dict[str, Any]], None]


class Scheduler:
    """最小时间堆；同一时刻按入队次序触发，保证可复现。"""

    def __init__(self, now: float = 0.0):
        self._now = float(now)
        self._seq = itertools.count()
        self._heap: List[Tuple[float, int, str, str, Dict[str, Any]]] = []
        self._handlers: Dict[str, EventHandler] = {}

    @property
    def now(self) -> float:
        return self._now

    def register(self, target: str, handler: EventHandler, replace: bool = False) -> None:
        if target in self._handlers and not replace:
            raise ValueError(f"设备标识重复: {target}")
        self._handlers[target] = handler

    def schedule(
        self,
        delay: float,
        event_type: str,
        target: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        if delay < 0:
            raise ValueError(f"事件时延不能为负: {delay}")
        seq = next(self._seq)
        heapq.heappush(
            self._heap,
            (self._now + float(delay), seq, event_type, target, dict(data or {})),
        )

    def schedule_at(
        self,
        time: float,
        event_type: str,
        target: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.schedule(time - self._now, event_type, target, data)

    def has_events(self) -> bool:
        return bool(self._heap)

    def next_time(self) -> Optional[float]:
        return self._heap[0][0] if self._heap else None

    def pop_next(self):
        if not self._heap:
            return None
        return heapq.heappop(self._heap)

    def dispatch(self, record: Tuple[float, int, str, str, Dict[str, Any]]) -> None:
        time, _seq, event_type, target, data = record
        self._now = time
        handler = self._handlers.get(target)
        if handler is None:
            raise KeyError(f"事件目标未注册: {target}")
        handler(time, event_type, data)

    def run_until(self, end_time: float) -> int:
        """推进到 ``end_time``（不含），返回处理事件数。"""
        count = 0
        while self._heap and self._heap[0][0] < end_time:
            self.dispatch(heapq.heappop(self._heap))
            count += 1
        # 没有更早事件时，后续相对延时以 end_time 为基准，但保留堆内绝对时刻
        if not self._heap or self._heap[0][0] >= end_time:
            self._now = float(end_time)
        return count

    def snapshot(self) -> Dict[str, Any]:
        return {
            "now": self._now,
            "next_seq": next(self._seq),
            "heap": [
                {"time": t, "seq": s, "type": et, "target": tg, "data": d}
                for t, s, et, tg, d in sorted(self._heap)
            ],
        }

    @classmethod
    def restore(cls, state: Dict[str, Any], handlers: Dict[str, EventHandler]) -> "Scheduler":
        sch = cls(state["now"])
        sch._handlers = dict(handlers)
        seqs = [int(item["seq"]) for item in state["heap"]]
        floor = max(seqs, default=-1) + 1
        floor = max(floor, int(state.get("next_seq", floor)))
        sch._seq = itertools.count(floor)
        sch._heap = [
            (
                float(item["time"]),
                int(item["seq"]),
                item["type"],
                item["target"],
                dict(item["data"]),
            )
            for item in state["heap"]
        ]
        heapq.heapify(sch._heap)
        return sch
