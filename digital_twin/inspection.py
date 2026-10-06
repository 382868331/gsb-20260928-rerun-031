from __future__ import annotations

from dataclasses import dataclass


JOB_QUEUED = "queued"
JOB_ACTIVE = "active"
JOB_FINISHED = "finished"
JOB_CANCELLED = "cancelled"


@dataclass
class InspectionJob:
    """一次检测资源占用承诺：排队、开始、结束都有明确记录。

    预测（plan）只读这些记录做排程，不会改动状态；
    实际放行只发生在匹配当轮 key 的检测结果送达时。
    """

    key: str
    serial_number: str
    station: str
    visit: int
    resource: str
    priority: int
    due: float | None
    estimated_duration: float
    enqueue_time: float
    state: str = JOB_QUEUED
    start_time: float | None = None
    end_time: float | None = None
    result: str | None = None
    cancel_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "serial_number": self.serial_number,
            "station": self.station,
            "visit": self.visit,
            "resource": self.resource,
            "priority": self.priority,
            "due": self.due,
            "estimated_duration": self.estimated_duration,
            "enqueue_time": self.enqueue_time,
            "state": self.state,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "result": self.result,
            "cancel_reason": self.cancel_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InspectionJob":
        return cls(
            key=data["key"],
            serial_number=data["serial_number"],
            station=data["station"],
            visit=data["visit"],
            resource=data["resource"],
            priority=data.get("priority", 0),
            due=data.get("due"),
            estimated_duration=data.get("estimated_duration", 1.0),
            enqueue_time=data.get("enqueue_time", 0.0),
            state=data.get("state", JOB_QUEUED),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            result=data.get("result"),
            cancel_reason=data.get("cancel_reason"),
        )


def job_order_key(job: InspectionJob) -> tuple:
    due = job.due if job.due is not None else float("inf")
    return (-job.priority, due, job.enqueue_time, job.key)


def _earliest_start(now: float, busy_ends: list[float], capacity: int,
                    outages: list[list[float]]) -> float:
    candidate = now
    while True:
        blocking = [window for window in outages if window[0] <= candidate < window[1]]
        if blocking:
            candidate = max(window[1] for window in blocking)
            continue
        active = sorted(end for end in busy_ends if end > candidate)
        if len(active) >= capacity:
            candidate = active[len(active) - capacity]
            continue
        return candidate


def plan_inspections(
    jobs: list[InspectionJob],
    capacity: int,
    outages: dict[str, list[list[float]]],
    now: float,
    policy: str = "priority",
) -> dict:
    """对当前排队/进行中的检测做纯预测排程，不改动任何实际状态。

    预测假设每个作业按估计时长结束且通过；这只是计划视图，
    实际放行仍以当轮真实检测结果为准。
    """
    if policy not in ("priority", "fifo"):
        raise ValueError(f"unknown planning policy: {policy}")
    resources = sorted({job.resource for job in jobs})
    busy: dict[str, list[float]] = {name: [] for name in resources}
    planned: list[InspectionJob] = []
    for job in jobs:
        if job.state == JOB_ACTIVE:
            start = job.start_time if job.start_time is not None else now
            end = max(now, start + job.estimated_duration)
            busy[job.resource].append(end)
        elif job.state == JOB_QUEUED:
            planned.append(job)
    if policy == "priority":
        planned.sort(key=job_order_key)
    else:
        planned.sort(key=lambda job: (job.enqueue_time, job.key))
    entries = []
    for job in jobs:
        if job.state == JOB_ACTIVE:
            start = job.start_time if job.start_time is not None else now
            entries.append((job, start, max(now, start + job.estimated_duration)))
    for job in planned:
        start = _earliest_start(
            now, busy[job.resource], capacity, outages.get(job.resource, []),
        )
        end = start + job.estimated_duration
        busy[job.resource].append(end)
        entries.append((job, start, end))
    rows = []
    total_wait = 0.0
    overdue_count = 0
    for job, start, end in entries:
        wait = start - job.enqueue_time
        overdue = job.due is not None and end > job.due
        total_wait += wait
        overdue_count += 1 if overdue else 0
        rows.append(
            {
                "key": job.key,
                "serial_number": job.serial_number,
                "resource": job.resource,
                "state": job.state,
                "planned_start": start,
                "planned_end": end,
                "wait": wait,
                "overdue": overdue,
            }
        )
    return {
        "policy": policy,
        "now": now,
        "order": [row["key"] for row in rows],
        "jobs": rows,
        "total_wait": total_wait,
        "overdue_count": overdue_count,
    }
