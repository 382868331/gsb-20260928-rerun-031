from __future__ import annotations

import json
from pathlib import Path

from .devices import Buffer, DeviceState, MaterialReceiver, MaterialSource, Sink, Station
from .engine import Event, EventQueue, SimulationClock
from .material import DispositionRecord, Material, MaterialState, MaterialType


class AssemblyLine:
    def __init__(
        self,
        station_cycle_times: list[float],
        buffer_capacities: list[int],
        initial_time: float = 0.0,
        material_types: list[MaterialType] | None = None,
        release_plan: dict[str, int] | None = None,
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
        self.scraps: list[dict] = []
        self.scrap_materials: list[Material] = []
        self.completed_materials: list[Material] = []
        self.completed_by_type: dict[str, int] = {}
        self.scrapped_by_type: dict[str, int] = {}
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
        self._refresh()

    def schedule_inspection_result(
        self,
        time: float,
        serial_number: str,
        station_name: str,
        result: str,
        visit: int,
    ) -> int:
        self._validate_inspection_result(result, visit)
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
        visit: int,
    ) -> None:
        self._validate_inspection_result(result, visit)
        event = Event(
            uid=0,
            time=self.clock.time,
            kind="INSPECTION_RESULT",
            source=station_name,
            payload={"serial": serial_number, "result": result, "visit": visit},
        )
        self._handle_inspection_result(event)
        self._refresh()

    @staticmethod
    def _validate_inspection_result(result: str, visit: int) -> None:
        if result not in ("pass", "rework", "fail", "scrap"):
            raise ValueError(f"unknown inspection result: {result}")
        if not isinstance(visit, int) or isinstance(visit, bool) or visit < 1:
            raise ValueError("inspection visit must be a positive integer")

    def _record_unapplied_inspection(
        self,
        event: Event,
        serial: str,
        action: str,
        reason: str,
        expected_key: str | None,
        received_key: str | None,
    ) -> None:
        self.inspection_log.append(
            {
                "time": event.time,
                "serial_number": serial,
                "station": event.source,
                "result": event.payload["result"],
                "visit": event.payload.get("visit"),
                "action": action,
                "reason": reason,
                "expected_key": expected_key,
                "received_key": received_key,
            }
        )

    def _handle_inspection_result(self, event: Event) -> None:
        serial = event.payload["serial"]
        result = event.payload["result"]
        if result not in ("pass", "rework", "fail", "scrap"):
            raise ValueError(f"unknown inspection result: {result}")
        expected_key = self.pending_inspections.get(serial)
        visit = event.payload.get("visit")
        if not isinstance(visit, int) or isinstance(visit, bool) or visit < 1:
            self._record_unapplied_inspection(
                event, serial, "held", "missing inspection visit",
                expected_key, None,
            )
            return
        received_key = self._inspection_key(serial, event.source, visit)
        if expected_key != received_key:
            existing = self.dispositions.get(received_key)
            if existing is not None and existing.result == result:
                action, reason = "ignored", "duplicate inspection result"
            elif expected_key is not None:
                action, reason = "held", "result does not belong to current inspection"
            else:
                action, reason = "ignored", "no current inspection with this identity"
            self._record_unapplied_inspection(
                event, serial, action, reason, expected_key, received_key,
            )
            return
        key = received_key
        index = self._station_index(event.source)
        station = self.stations[index]
        material = station.material
        if material is None or material.serial_number != serial:
            raise RuntimeError(f"pending material {serial} not found at {station.name}")
        del self.pending_inspections[serial]
        material_type = self._type_of(material)
        visit = material.visits.get(station.name, 0)
        record = DispositionRecord(key, event.time, station.name, visit, result)
        self.dispositions[key] = record
        if result == "pass":
            record.rework_count_after = material.rework_count
            material.record(event.time, station.name, visit, "inspection_pass", key=key)
            material.state = MaterialState.IN_PROCESS
            self.ledger[serial] = MaterialState.IN_PROCESS.value
        elif result == "rework" and material.rework_count < material_type.max_reworks:
            material.rework_count += 1
            record.rework_count_after = material.rework_count
            visit = material.visits[station.name] + 1
            material.visits[station.name] = visit
            material.record(
                event.time, station.name, visit, "rework_start",
                key=key, rework_count=material.rework_count,
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
        line.scraps = list(data.get("scraps", []))
        line.scrap_materials = [
            Material.from_dict(item) for item in data.get("scrap_materials", [])
        ]
        line.completed_materials = [
            Material.from_dict(item) for item in data.get("completed_materials", [])
        ]
        line.completed_by_type = dict(data.get("completed_by_type", {}))
        line.scrapped_by_type = dict(data.get("scrapped_by_type", {}))
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
            "scraps": list(self.scraps),
            "scrap_materials": [material.to_dict() for material in self.scrap_materials],
            "completed_materials": [material.to_dict() for material in self.completed_materials],
            "completed_by_type": dict(self.completed_by_type),
            "scrapped_by_type": dict(self.scrapped_by_type),
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
