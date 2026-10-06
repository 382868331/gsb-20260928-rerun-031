from __future__ import annotations

import json
from pathlib import Path

from .devices import Buffer, DeviceState, MaterialReceiver, MaterialSource, Sink, Station
from .engine import Event, EventQueue, SimulationClock
from .inspection import (
    JOB_ACTIVE,
    JOB_CANCELLED,
    JOB_FINISHED,
    JOB_QUEUED,
    InspectionJob,
    job_order_key,
    plan_inspections,
)
from .material import DispositionRecord, Material, MaterialState, MaterialType


class AssemblyLine:
    def __init__(
        self,
        station_cycle_times: list[float],
        buffer_capacities: list[int],
        initial_time: float = 0.0,
        material_types: list[MaterialType] | None = None,
        release_plan: dict[str, int] | None = None,
        inspection_resources: dict[str, list[str]] | None = None,
        inspection_capacity: int = 1,
    ) -> None:
        if len(station_cycle_times) < 1:
            raise ValueError("at least one station is required")
        if len(buffer_capacities) != len(station_cycle_times) - 1:
            raise ValueError("one buffer capacity is required between each station pair")
        self.clock = SimulationClock(initial_time)
        self.event_queue = EventQueue()
        self.source = MaterialSource()
        self.stations = [
            Station(f"station-{index + 1}", cycle_time)
            for index, cycle_time in enumerate(station_cycle_times)
        ]
        self.buffers = [
            Buffer(f"buffer-{index + 1}", capacity)
            for index, capacity in enumerate(buffer_capacities)
        ]
        self.sink = Sink()
        self.scrapped = 0
        self.ledger: dict[str, str] = {}
        station_names = tuple(station.name for station in self.stations)
        self.material_types: dict[str, MaterialType] = {
            "standard": MaterialType(name="standard", route=station_names)
        }
        for material_type in material_types or []:
            self.register_material_type(material_type)
        self.pending_inspections: dict[str, str] = {}
        self.dispositions: dict[str, DispositionRecord] = {}
        self.inspection_log: list[dict] = []
        self.indeterminate_results: list[dict] = []
        self.scraps: list[dict] = []
        self.scrap_materials: list[Material] = []
        self.completed_materials: list[Material] = []
        self.completed_by_type: dict[str, int] = {}
        self.scrapped_by_type: dict[str, int] = {}
        if inspection_capacity < 1:
            raise ValueError("inspection capacity must be positive")
        self.inspection_capacity = inspection_capacity
        self._station_to_resource: dict[str, str] = {}
        for resource, stations in (inspection_resources or {}).items():
            for station_name in stations:
                self._station_index(station_name)
                self._station_to_resource[station_name] = resource
        self.inspection_outages: dict[str, list[list[float]]] = {}
        self.inspection_jobs: dict[str, InspectionJob] = {}
        self.replan_log: list[dict] = []
        for material_type, count in (release_plan or {}).items():
            self.release(material_type, count)
        self._refresh()

    def register_material_type(self, material_type: MaterialType) -> None:
        station_names = {station.name for station in self.stations}
        unknown = set(material_type.route) | set(material_type.inspect_after)
        unknown |= set(material_type.cycle_times)
        unknown -= station_names
        if unknown:
            raise ValueError(f"unknown stations in material type {material_type.name}: {sorted(unknown)}")
        if material_type.max_reworks < 0:
            raise ValueError("max_reworks must be non-negative")
        self.material_types[material_type.name] = material_type

    def release(self, material_type: str = "standard", count: int = 1) -> None:
        if material_type not in self.material_types:
            raise ValueError(f"unregistered material type: {material_type}")
        self.source.plan_release(material_type, count)
        self._refresh()

    def _type_of(self, material: Material) -> MaterialType:
        return self.material_types.get(material.material_type, self.material_types["standard"])

    @staticmethod
    def _inspection_key(serial: str, station: str, visit: int) -> str:
        return f"{serial}|{station}|{visit}"

    def _schedule(self, time: float, kind: str, source: str, payload: dict | None = None) -> int:
        return self.event_queue.schedule(time, kind, source, payload or {})

    def _cancel(self, uid: int) -> None:
        self.event_queue.cancel(uid)

    def _upstream_for(self, station_index: int):
        if station_index == 0:
            return self.source
        return self.buffers[station_index - 1]

    def _downstream_for(self, station_index: int) -> MaterialReceiver:
        if station_index == len(self.stations) - 1:
            return self.sink
        return self.buffers[station_index]

    def _station_index(self, name: str) -> int:
        for index, station in enumerate(self.stations):
            if station.name == name:
                return index
        raise KeyError(f"unknown station: {name}")

    def _refresh(self) -> None:
        changed = True
        while changed:
            changed = False
            for index in range(len(self.stations) - 1, -1, -1):
                station = self.stations[index]
                if station.state == DeviceState.BLOCKED and station.remaining_time == 0:
                    material = station.material
                    if material is None:
                        continue
                    if material.serial_number in self.pending_inspections:
                        continue
                    serial_number = material.serial_number
                    if station.try_release(self._downstream_for(index)):
                        if index == len(self.stations) - 1:
                            self.ledger[serial_number] = MaterialState.COMPLETED.value
                            material.record(
                                self.clock.time, station.name,
                                material.visits.get(station.name, 0), "completed",
                            )
                            self.completed_materials.append(material)
                            self.completed_by_type[material.material_type] = (
                                self.completed_by_type.get(material.material_type, 0) + 1
                            )
                        else:
                            self.ledger[serial_number] = MaterialState.WAITING.value
                        changed = True

            for index in range(len(self.stations) - 1, -1, -1):
                station = self.stations[index]
                upstream = self._upstream_for(index)
                downstream = self._downstream_for(index)
                if station.is_idle and upstream.can_take() and downstream.can_accept():
                    material = upstream.peek()
                    material_type = self._type_of(material)
                    if station.name in material_type.route:
                        cycle = material_type.cycle_time_for(station.name, station.cycle_time)
                        setup = (
                            material_type.setup_time
                            if station.last_type != material.material_type
                            else 0.0
                        )
                    else:
                        cycle, setup = 0.0, 0.0
                    station.start(upstream, self.clock.time, self._schedule, cycle, setup)
                    if station.material is not None:
                        self.ledger[station.material.serial_number] = MaterialState.IN_PROCESS.value
                    changed = True

    def schedule_fault(
        self,
        start_time: float,
        station_name: str,
        duration: float,
        scrap_current: bool = False,
    ) -> int:
        if duration <= 0:
            raise ValueError("fault duration must be positive")
        return self._schedule(
            start_time,
            "STATION_FAULT",
            station_name,
            {"duration": duration, "scrap_current": scrap_current},
        )

    def _handle_fault(self, event: Event) -> None:
        index = self._station_index(event.source)
        station = self.stations[index]
        completion_time = None
        if station.completion_event is not None:
            completion = self.event_queue.get(station.completion_event)
            completion_time = completion.time if completion else None
        scrapped = station.fail(
            completion_time,
            event.time,
            self._cancel,
            event.payload.get("scrap_current", False),
        )
        if scrapped is not None:
            self.pending_inspections.pop(scrapped.serial_number, None)
            self._record_scrap(scrapped, event.time, event.source, "station_fault")
        self._schedule(
            event.time + event.payload["duration"],
            "STATION_REPAIRED",
            station.name,
        )

    def _record_scrap(self, material: Material, time: float, station_name: str, reason: str) -> None:
        visit = material.visits.get(station_name, 0)
        material.state = MaterialState.SCRAPPED
        material.location = "scrap"
        material.record(time, station_name, visit, "scrap", reason=reason)
        self.scrapped += 1
        self.scrapped_by_type[material.material_type] = (
            self.scrapped_by_type.get(material.material_type, 0) + 1
        )
        self.scraps.append(
            {
                "serial_number": material.serial_number,
                "material_type": material.material_type,
                "station": station_name,
                "time": time,
                "reason": reason,
                "rework_count": material.rework_count,
            }
        )
        self.scrap_materials.append(material)
        self.ledger[material.serial_number] = MaterialState.SCRAPPED.value
        self._retire_remaining_route(material, time, reason)

    def _retire_remaining_route(self, material: Material, time: float, reason: str) -> None:
        """实际失败后只重排受影响的剩余路线：取消该物料未开始/进行中的检测承诺，
        已完成加工与已发生报废保持事实，其余物料的排队不受影响。"""
        affected = []
        for job in self.inspection_jobs.values():
            if job.serial_number != material.serial_number:
                continue
            if job.state == JOB_QUEUED:
                job.state = JOB_CANCELLED
                job.cancel_reason = reason
                job.end_time = time
                affected.append(job.key)
            elif job.state == JOB_ACTIVE:
                job.state = JOB_FINISHED
                job.end_time = time
                job.result = "scrapped"
                affected.append(job.key)
        if affected:
            self.replan_log.append(
                {
                    "time": time,
                    "serial_number": material.serial_number,
                    "reason": reason,
                    "retired_jobs": affected,
                }
            )
        self._maybe_start_inspections(time)

    def _handle_repaired(self, event: Event) -> None:
        index = self._station_index(event.source)
        station = self.stations[index]
        station.repair(event.time, self._downstream_for(index), self._schedule)
        self._refresh()

    def _handle_complete(self, event: Event) -> None:
        index = self._station_index(event.source)
        station = self.stations[index]
        station.complete()
        material = station.material
        if material is not None:
            material_type = self._type_of(material)
            visit = material.visits.get(station.name, 0)
            material.record(event.time, station.name, visit, "processed")
            if station.name in material_type.inspect_after:
                key = self._inspection_key(material.serial_number, station.name, visit)
                material.state = MaterialState.PENDING_INSPECTION
                material.record(event.time, station.name, visit, "await_inspection", key=key)
                self.pending_inspections[material.serial_number] = key
                self.ledger[material.serial_number] = MaterialState.PENDING_INSPECTION.value
                self._enqueue_inspection(material, station.name, visit, key, event.time)
        self._refresh()

    def _resource_for(self, station_name: str) -> str:
        return self._station_to_resource.get(station_name, station_name)

    def _resource_in_outage(self, resource: str, time: float) -> bool:
        return any(
            start <= time < end
            for start, end in self.inspection_outages.get(resource, [])
        )

    def _find_material(self, serial_number: str) -> Material | None:
        for station in self.stations:
            if station.material and station.material.serial_number == serial_number:
                return station.material
        for buffer in self.buffers:
            for material in buffer.items:
                if material.serial_number == serial_number:
                    return material
        return None

    def _enqueue_inspection(
        self, material: Material, station_name: str, visit: int, key: str, now: float,
    ) -> InspectionJob:
        material_type = self._type_of(material)
        job = InspectionJob(
            key=key,
            serial_number=material.serial_number,
            station=station_name,
            visit=visit,
            resource=self._resource_for(station_name),
            priority=material_type.priority,
            due=material_type.due,
            estimated_duration=material_type.inspection_time,
            enqueue_time=now,
        )
        self.inspection_jobs[key] = job
        self._maybe_start_inspections(now)
        return job

    def _maybe_start_inspections(self, now: float) -> None:
        resources = sorted({job.resource for job in self.inspection_jobs.values()})
        for resource in resources:
            while True:
                active = [
                    job for job in self.inspection_jobs.values()
                    if job.resource == resource and job.state == JOB_ACTIVE
                ]
                if len(active) >= self.inspection_capacity:
                    break
                if self._resource_in_outage(resource, now):
                    break
                queued = [
                    job for job in self.inspection_jobs.values()
                    if job.resource == resource and job.state == JOB_QUEUED
                ]
                if not queued:
                    break
                job = min(queued, key=job_order_key)
                job.state = JOB_ACTIVE
                job.start_time = now
                material = self._find_material(job.serial_number)
                if material is not None:
                    material.record(
                        now, job.station, job.visit, "inspection_start",
                        key=job.key, resource=resource,
                    )

    def _finish_inspection_job(self, key: str, time: float, result: str) -> None:
        job = self.inspection_jobs.get(key)
        if job is not None and job.state in (JOB_QUEUED, JOB_ACTIVE):
            job.state = JOB_FINISHED
            job.end_time = time
            job.result = result
        self._maybe_start_inspections(time)

    def add_inspection_outage(self, resource: str, start: float, end: float) -> int:
        if end <= start:
            raise ValueError("outage end must be after start")
        known = {self._resource_for(station.name) for station in self.stations}
        if resource not in known:
            raise ValueError(f"unknown inspection resource: {resource}")
        self.inspection_outages.setdefault(resource, []).append([start, end])
        return self._schedule(end, "INSPECTION_OUTAGE_END", resource)

    def cancel_inspection(self, serial_number: str, station_name: str, visit: int) -> bool:
        """取消尚未开始的检测承诺；已开始的必须等待明确结束，返回 False。"""
        key = self._inspection_key(serial_number, station_name, visit)
        job = self.inspection_jobs.get(key)
        if job is None:
            raise KeyError(f"unknown inspection job: {key}")
        if job.state != JOB_QUEUED:
            return False
        now = self.clock.time
        job.state = JOB_CANCELLED
        job.cancel_reason = "cancelled_by_user"
        job.end_time = now
        self.inspection_log.append(
            {
                "time": now,
                "serial_number": serial_number,
                "station": station_name,
                "visit": visit,
                "result": None,
                "action": "cancelled",
                "reason": "cancelled_by_user",
                "original_key": key,
                "claimed_key": key,
            }
        )
        if self.pending_inspections.get(serial_number) == key:
            del self.pending_inspections[serial_number]
            material = self._find_material(serial_number)
            if material is not None:
                material.record(
                    now, station_name, visit, "inspection_cancelled", key=key,
                )
                material.state = MaterialState.IN_PROCESS
            self.ledger[serial_number] = MaterialState.IN_PROCESS.value
        self._refresh()
        self._maybe_start_inspections(now)
        return True

    def plan_inspections(self, policy: str = "priority") -> dict:
        jobs = [
            job for job in self.inspection_jobs.values()
            if job.state in (JOB_QUEUED, JOB_ACTIVE)
        ]
        return plan_inspections(
            jobs, self.inspection_capacity, self.inspection_outages,
            self.clock.time, policy,
        )

    def compare_inspection_policies(self) -> dict:
        return {
            policy: self.plan_inspections(policy)
            for policy in ("fifo", "priority")
        }

    def inspection_conservation(self) -> dict:
        jobs = list(self.inspection_jobs.values())
        by_state = {JOB_QUEUED: 0, JOB_ACTIVE: 0, JOB_FINISHED: 0, JOB_CANCELLED: 0}
        for job in jobs:
            by_state[job.state] = by_state.get(job.state, 0) + 1
        queue_ok = sum(by_state.values()) == len(jobs)
        resource_ok = True
        outage_ok = True
        resources = sorted({job.resource for job in jobs})
        for resource in resources:
            intervals = [
                (job.start_time, job.end_time)
                for job in jobs
                if job.resource == resource and job.start_time is not None
            ]
            points = sorted({p for pair in intervals for p in pair if p is not None})
            for point in points:
                overlap = sum(
                    1 for start, end in intervals
                    if start <= point and (end is None or point < end)
                )
                if overlap > self.inspection_capacity:
                    resource_ok = False
            active_count = sum(
                1 for job in jobs
                if job.resource == resource and job.state == JOB_ACTIVE
            )
            if active_count > self.inspection_capacity:
                resource_ok = False
            for job in jobs:
                if (
                    job.resource == resource
                    and job.start_time is not None
                    and self._resource_in_outage(resource, job.start_time)
                ):
                    outage_ok = False
        return {
            "jobs_total": len(jobs),
            "by_state": by_state,
            "queue_conserved": queue_ok,
            "resource_conserved": resource_ok,
            "outage_respected": outage_ok,
            "material_conserved": self.accounts_balanced(),
        }

    def schedule_inspection_result(
        self,
        time: float,
        serial_number: str,
        station_name: str,
        result: str,
        visit: int | None = None,
    ) -> int:
        return self._schedule(
            time,
            "INSPECTION_RESULT",
            station_name,
            {"serial": serial_number, "result": result, "visit": visit},
        )

    def deliver_inspection_result(
        self,
        serial_number: str,
        station_name: str,
        result: str,
        visit: int | None = None,
    ) -> None:
        event = Event(
            uid=0,
            time=self.clock.time,
            kind="INSPECTION_RESULT",
            source=station_name,
            payload={"serial": serial_number, "result": result, "visit": visit},
        )
        self._handle_inspection_result(event)
        self._refresh()

    def _log_ignored(
        self, event: Event, key: str | None, reason: str,
        claimed_key: str | None = None,
    ) -> None:
        self.inspection_log.append(
            {
                "time": event.time,
                "serial_number": event.payload["serial"],
                "station": event.source,
                "visit": event.payload.get("visit"),
                "result": event.payload["result"],
                "action": "ignored",
                "reason": reason,
                "original_key": key,
                "claimed_key": claimed_key,
            }
        )

    def _hold_indeterminate(self, event: Event, claimed_key: str | None, reason: str) -> None:
        entry = {
            "time": event.time,
            "serial_number": event.payload["serial"],
            "station": event.source,
            "visit": event.payload.get("visit"),
            "result": event.payload["result"],
            "action": "indeterminate",
            "reason": reason,
            "claimed_key": claimed_key,
        }
        self.indeterminate_results.append(entry)
        self.inspection_log.append(dict(entry))

    def _handle_inspection_result(self, event: Event) -> None:
        serial = event.payload["serial"]
        result = event.payload["result"]
        visit = event.payload.get("visit")
        if result not in ("pass", "rework", "fail", "scrap"):
            raise ValueError(f"unknown inspection result: {result}")
        station_name = event.source
        current_key = self.pending_inspections.get(serial)
        claimed_key = (
            self._inspection_key(serial, station_name, visit)
            if visit is not None
            else None
        )

        if visit is None:
            latest = [
                record
                for key, record in self.dispositions.items()
                if key.startswith(f"{serial}|{station_name}|")
            ]
            if latest:
                self._log_ignored(
                    event, latest[-1].key,
                    "result without inspection round after round finalized", None,
                )
                return
            self._hold_indeterminate(
                event, None,
                "result carries no inspection round; round cannot be verified",
            )
            return

        if current_key != claimed_key:
            if claimed_key in self.dispositions:
                self._log_ignored(
                    event, self.dispositions[claimed_key].key,
                    "round already finalized", claimed_key,
                )
            else:
                self._hold_indeterminate(
                    event, claimed_key,
                    "claimed round is not the current pending inspection",
                )
            return
        if claimed_key in self.dispositions:
            self._log_ignored(
                event, self.dispositions[claimed_key].key,
                "round already finalized", claimed_key,
            )
            return
        job = self.inspection_jobs.get(claimed_key)
        if job is not None and job.state == JOB_QUEUED:
            self._hold_indeterminate(
                event, claimed_key,
                "inspection has not started; resource commitment not active",
            )
            return

        index = self._station_index(station_name)
        station = self.stations[index]
        material = station.material
        if material is None or material.serial_number != serial:
            self._hold_indeterminate(
                event, claimed_key,
                f"material does not occupy {station.name}",
            )
            return
        del self.pending_inspections[serial]
        material_type = self._type_of(material)
        visit = material.visits.get(station.name, 0)
        record = DispositionRecord(claimed_key, event.time, station.name, visit, result)
        self.dispositions[claimed_key] = record
        self._finish_inspection_job(claimed_key, event.time, result)
        if result == "pass":
            record.rework_count_after = material.rework_count
            material.record(event.time, station.name, visit, "inspection_pass", key=claimed_key)
            material.state = MaterialState.IN_PROCESS
            self.ledger[serial] = MaterialState.IN_PROCESS.value
        elif result == "rework" and material.rework_count < material_type.max_reworks:
            material.rework_count += 1
            record.rework_count_after = material.rework_count
            visit = material.visits[station.name] + 1
            material.visits[station.name] = visit
            material.record(
                event.time, station.name, visit, "rework_start",
                key=claimed_key, rework_count=material.rework_count,
            )
            material.state = MaterialState.IN_PROCESS
            cycle = material_type.cycle_time_for(station.name, station.cycle_time)
            station.state = DeviceState.PROCESSING
            station.remaining_time = cycle
            station.completion_event = self._schedule(
                event.time + cycle,
                "PROCESS_COMPLETE",
                station.name,
                {"material": serial, "visit": visit},
            )
            self.ledger[serial] = MaterialState.IN_PROCESS.value
        else:
            record.rework_count_after = material.rework_count
            reason = "rework_limit_exceeded" if result == "rework" else "inspection_failed"
            station.material = None
            station.remaining_time = None
            station.completion_event = None
            station.state = DeviceState.IDLE
            self._record_scrap(material, event.time, station.name, reason)

    def _process_next(self) -> Event:
        event = self.event_queue.pop()
        if event is None:
            raise RuntimeError("no pending events")
        self.clock.advance(event.time)
        if event.kind == "PROCESS_COMPLETE":
            self._handle_complete(event)
        elif event.kind == "STATION_FAULT":
            self._handle_fault(event)
        elif event.kind == "STATION_REPAIRED":
            self._handle_repaired(event)
        elif event.kind == "INSPECTION_RESULT":
            self._handle_inspection_result(event)
            self._refresh()
        elif event.kind == "INSPECTION_OUTAGE_END":
            self._maybe_start_inspections(event.time)
        else:
            raise ValueError(f"unknown event kind: {event.kind}")
        return event

    def run_until(self, end_time: float) -> list[Event]:
        processed = []
        while True:
            next_time = self.event_queue.next_time()
            if next_time is None or next_time > end_time:
                break
            processed.append(self._process_next())
        self.clock.advance(end_time)
        return processed

    def pause_at(self, time: float) -> list[Event]:
        processed = []
        while True:
            next_time = self.event_queue.next_time()
            if next_time is None or next_time >= time:
                break
            processed.append(self._process_next())
        self.clock.advance(time)
        return processed

    def save(self, path: str | Path) -> None:
        target = Path(path)
        if target.parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "AssemblyLine":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        line = cls.__new__(cls)
        line.clock = SimulationClock.from_dict(data["clock"])
        line.event_queue = EventQueue.from_dict(data["event_queue"])
        line.source = MaterialSource.from_dict(data["source"])
        line.stations = [Station.from_dict(item) for item in data["stations"]]
        line.buffers = [Buffer.from_dict(item) for item in data["buffers"]]
        line.sink = Sink.from_dict(data["sink"])
        line.scrapped = data["scrapped"]
        line.ledger = data["ledger"]
        line.material_types = {
            name: MaterialType.from_dict(item)
            for name, item in data.get(
                "material_types",
                {"standard": MaterialType(name="standard", route=tuple(s.name for s in line.stations)).to_dict()},
            ).items()
        }
        line.pending_inspections = dict(data.get("pending_inspections", {}))
        line.dispositions = {
            key: DispositionRecord.from_dict(item)
            for key, item in data.get("dispositions", {}).items()
        }
        line.inspection_log = list(data.get("inspection_log", []))
        line.indeterminate_results = list(data.get("indeterminate_results", []))
        line.scraps = list(data.get("scraps", []))
        line.scrap_materials = [
            Material.from_dict(item) for item in data.get("scrap_materials", [])
        ]
        line.completed_materials = [
            Material.from_dict(item) for item in data.get("completed_materials", [])
        ]
        line.completed_by_type = dict(data.get("completed_by_type", {}))
        line.scrapped_by_type = dict(data.get("scrapped_by_type", {}))
        line.inspection_capacity = data.get("inspection_capacity", 1)
        line._station_to_resource = dict(data.get("station_to_resource", {}))
        line.inspection_outages = {
            key: [list(window) for window in value]
            for key, value in data.get("inspection_outages", {}).items()
        }
        line.inspection_jobs = {
            key: InspectionJob.from_dict(item)
            for key, item in data.get("inspection_jobs", {}).items()
        }
        line.replan_log = list(data.get("replan_log", []))
        return line

    def to_dict(self) -> dict:
        return {
            "clock": self.clock.to_dict(),
            "event_queue": self.event_queue.to_dict(),
            "source": self.source.to_dict(),
            "stations": [station.to_dict() for station in self.stations],
            "buffers": [buffer.to_dict() for buffer in self.buffers],
            "sink": self.sink.to_dict(),
            "scrapped": self.scrapped,
            "ledger": self.ledger,
            "material_types": {
                name: material_type.to_dict()
                for name, material_type in self.material_types.items()
            },
            "pending_inspections": dict(self.pending_inspections),
            "dispositions": {
                key: record.to_dict() for key, record in self.dispositions.items()
            },
            "inspection_log": list(self.inspection_log),
            "indeterminate_results": list(self.indeterminate_results),
            "scraps": list(self.scraps),
            "scrap_materials": [material.to_dict() for material in self.scrap_materials],
            "completed_materials": [material.to_dict() for material in self.completed_materials],
            "completed_by_type": dict(self.completed_by_type),
            "scrapped_by_type": dict(self.scrapped_by_type),
            "inspection_capacity": self.inspection_capacity,
            "station_to_resource": dict(self._station_to_resource),
            "inspection_outages": {
                key: [list(window) for window in value]
                for key, value in self.inspection_outages.items()
            },
            "inspection_jobs": {
                key: job.to_dict() for key, job in self.inspection_jobs.items()
            },
            "replan_log": list(self.replan_log),
        }

    @property
    def released(self) -> int:
        return self.source.released

    @property
    def completed(self) -> int:
        return self.sink.accepted

    @property
    def work_in_progress(self) -> int:
        in_stations = sum(1 for station in self.stations if station.material is not None)
        in_buffers = sum(len(buffer.items) for buffer in self.buffers)
        return in_stations + in_buffers

    def reconcile(self) -> dict[str, int]:
        counts = {
            "released": self.released,
            "completed": self.completed,
            "work_in_progress": self.work_in_progress,
            "scrapped": self.scrapped,
        }
        counts["accounted"] = (
            counts["completed"] + counts["work_in_progress"] + counts["scrapped"]
        )
        return counts

    def accounts_balanced(self) -> bool:
        counts = self.reconcile()
        return (
            counts["released"] == counts["accounted"]
            and len(self.ledger)
            == counts["completed"] + counts["work_in_progress"] + counts["scrapped"]
        )

    def reconcile_by_type(self) -> dict[str, dict[str, int]]:
        wip_by_type: dict[str, int] = {}
        for station in self.stations:
            if station.material is not None:
                key = station.material.material_type
                wip_by_type[key] = wip_by_type.get(key, 0) + 1
        for buffer in self.buffers:
            for material in buffer.items:
                wip_by_type[material.material_type] = (
                    wip_by_type.get(material.material_type, 0) + 1
                )
        names = set(self.source.released_by_type) | set(self.completed_by_type)
        names |= set(self.scrapped_by_type) | set(wip_by_type)
        report = {}
        for name in sorted(names):
            released = self.source.released_by_type.get(name, 0)
            completed = self.completed_by_type.get(name, 0)
            scrapped = self.scrapped_by_type.get(name, 0)
            wip = wip_by_type.get(name, 0)
            report[name] = {
                "released": released,
                "completed": completed,
                "work_in_progress": wip,
                "scrapped": scrapped,
                "balanced": released == completed + wip + scrapped,
            }
        return report

    def material_history(self, serial_number: str) -> list[dict]:
        for station in self.stations:
            if station.material and station.material.serial_number == serial_number:
                return [entry.to_dict() for entry in station.material.history]
        for buffer in self.buffers:
            for material in buffer.items:
                if material.serial_number == serial_number:
                    return [entry.to_dict() for entry in material.history]
        for material in self.scrap_materials:
            if material.serial_number == serial_number:
                return [entry.to_dict() for entry in material.history]
        for material in self.completed_materials:
            if material.serial_number == serial_number:
                return [entry.to_dict() for entry in material.history]
        raise KeyError(f"unknown material: {serial_number}")

    def status(self) -> dict:
        return {
            "time": self.clock.time,
            "stations": [
                {
                    "name": station.name,
                    "state": station.state.value,
                    "material": station.material.serial_number if station.material else None,
                    "remaining_time": station.remaining_time,
                }
                for station in self.stations
            ],
            "buffers": [
                {"name": buffer.name, "occupied": len(buffer.items), "capacity": buffer.capacity}
                for buffer in self.buffers
            ],
            **self.reconcile(),
        }
