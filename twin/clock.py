"""仿真时钟：所有时间推进都来自这里，墙钟时间不参与仿真。"""


class Clock:
    def __init__(self, now: float = 0.0):
        self._now = float(now)

    @property
    def now(self) -> float:
        return self._now

    def advance_to(self, t: float) -> None:
        if t < self._now:
            raise ValueError(f"时钟不能回拨: {t} < {self._now}")
        self._now = float(t)

    def snapshot(self) -> float:
        return self._now

    @classmethod
    def restore(cls, state: float) -> "Clock":
        return cls(state)
