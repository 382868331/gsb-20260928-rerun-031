from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InspectionJob:
    """一次检测承诺：按 序列号|工位|轮次 唯一标识，占用检测工位直到明确结束。"""

    key: str
    serial_number: str
    station: str
    visit: int
    priority: int
    due_time: float | None
    duration: float
    enqueue_time: float
    state: str = "queued"  # queued / in_progress / cancel_pending / completed / cancelled
    start_time: float | None = None
    finish_time: float | None = None
    actual_result: str | None = None
    predicted_result: str | None = None
    cancelled_at: float | None = None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "in_progress", "cancel_pending")

    @property
    def wait_time(self) -> float | None:
        if self.start_time is None:
            return None
        return self.start_time - self.enqueue_time

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "serial_number": self.serial_number,
            "station": self.station,
            "visit": self.visit,
            "priority": self.priority,
            "due_time": self.due_time,
            "duration": self.duration,
            "enqueue_time": self.enqueue_time,
            "state": self.state,
            "start_time": self.start_time,
            "finish_time": self.finish_time,
            "actual_result": self.actual_result,
            "predicted_result": self.predicted_result,
            "cancelled_at": self.cancelled_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InspectionJob":
        return cls(
            key=data["key"],
            serial_number=data["serial_number"],
            station=data["station"],
            visit=data["visit"],
            priority=data.get("priority", 0),
            due_time=data.get("due_time"),
            duration=data.get("duration", 1.0),
            enqueue_time=data.get("enqueue_time", 0.0),
            state=data.get("state", "queued"),
            start_time=data.get("start_time"),
            finish_time=data.get("finish_time"),
            actual_result=data.get("actual_result"),
            predicted_result=data.get("predicted_result"),
            cancelled_at=data.get("cancelled_at"),
        )


class InspectionResource:
    """受限检测工位：容量有限、可临时停用，按交付优先级安排可执行顺序。"""

    def __init__(self, name: str, capacity: int = 1, policy: str = "priority") -> None:
        if capacity < 1:
            raise ValueError("inspection capacity must be positive")
        if policy not in ("priority", "fifo"):
            raise ValueError(f"unknown scheduling policy: {policy}")
        self.name = name
        self.capacity = capacity
        self.policy = policy
        self.outages: list[tuple[float, float]] = []
        self.jobs: dict[str, InspectionJob] = {}

    def add_outage(self, start: float, end: float) -> None:
        if end <= start:
            raise ValueError("outage end must be after start")
        self.outages.append((float(start), float(end)))
        self.outages.sort()

    def submit(self, job: InspectionJob) -> None:
        existing = self.jobs.get(job.key)
        if existing is not None and existing.active:
            raise ValueError(f"duplicate active inspection job: {job.key}")
        self.jobs[job.key] = job

    def _sort_key(self, job: InspectionJob):
        if self.policy == "fifo":
            return (job.enqueue_time, job.key)
        due = job.due_time if job.due_time is not None else float("inf")
        return (-job.priority, due, job.enqueue_time, job.key)

    def executable_order(self) -> list[str]:
        queued = [job for job in self.jobs.values() if job.state == "queued"]
        return [job.key for job in sorted(queued, key=self._sort_key)]

    def _in_outage(self, time: float) -> bool:
        return any(start <= time < end for start, end in self.outages)

    def _fits_window(self, start: float, duration: float) -> bool:
        return all(
            start + duration <= outage_start or start >= outage_end
            for outage_start, outage_end in self.outages
        )

    def _active_count(self) -> int:
        return sum(
            1 for job in self.jobs.values()
            if job.state in ("in_progress", "cancel_pending")
        )

    def start_next(self, now: float) -> InspectionJob | None:
        if self._active_count() >= self.capacity or self._in_outage(now):
            return None
        for key in self.executable_order():
            job = self.jobs[key]
            if not self._fits_window(now, job.duration):
                continue
            job.state = "in_progress"
            job.start_time = now
            return job
        return None

    def predict(self, key: str, result: str) -> bool:
        """记录预测结果；预测不构成放行，不改变任何占用状态。"""
        job = self.jobs.get(key)
        if job is None or not job.active:
            return False
        job.predicted_result = result
        return True

    def finish(self, key: str, now: float, result: str) -> InspectionJob:
        job = self.jobs.get(key)
        if job is None or job.state not in ("in_progress", "cancel_pending"):
            raise RuntimeError(f"inspection job not in progress: {key}")
        job.finish_time = now
        job.actual_result = result
        job.state = "cancelled" if job.state == "cancel_pending" else "completed"
        if job.state == "cancelled":
            job.cancelled_at = now
        return job

    def cancel(self, key: str, now: float) -> str:
        """未开始的检测直接释放承诺；已开始的标记待取消，仍需等待明确结束。"""
        job = self.jobs.get(key)
        if job is None or not job.active:
            return "inactive"
        if job.state == "queued":
            job.state = "cancelled"
            job.cancelled_at = now
            return "released"
        job.state = "cancel_pending"
        return "deferred"

    def cancel_for_serial(self, serial_number: str, now: float) -> dict[str, str]:
        outcomes = {}
        for job in self.jobs.values():
            if job.serial_number == serial_number and job.active:
                outcomes[job.key] = self.cancel(job.key, now)
        return outcomes

    def metrics(self) -> dict:
        started = [job for job in self.jobs.values() if job.start_time is not None]
        completed = [job for job in self.jobs.values() if job.state == "completed"]
        overdue = [
            job for job in completed
            if job.due_time is not None
            and job.finish_time is not None
            and job.finish_time > job.due_time
        ]
        return {
            "submitted": len(self.jobs),
            "queued": sum(1 for job in self.jobs.values() if job.state == "queued"),
            "in_progress": self._active_count(),
            "completed": len(completed),
            "cancelled": sum(1 for job in self.jobs.values() if job.state == "cancelled"),
            "total_wait": sum(job.wait_time or 0.0 for job in started),
            "overdue_count": len(overdue),
        }

    def conserved(self) -> bool:
        metrics = self.metrics()
        return metrics["submitted"] == (
            metrics["queued"] + metrics["in_progress"]
            + metrics["completed"] + metrics["cancelled"]
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "policy": self.policy,
            "outages": [list(outage) for outage in self.outages],
            "jobs": [job.to_dict() for job in self.jobs.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InspectionResource":
        resource = cls(data["name"], data.get("capacity", 1), data.get("policy", "priority"))
        resource.outages = [tuple(outage) for outage in data.get("outages", [])]
        resource.jobs = {
            item["key"]: InspectionJob.from_dict(item) for item in data.get("jobs", [])
        }
        return resource
