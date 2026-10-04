from __future__ import annotations

import json
from pathlib import Path

from .devices import Buffer, DeviceState, MaterialReceiver, MaterialSource, Sink, Station
from .engine import Event, EventQueue, SimulationClock
from .material import DispositionRecord, Material, MaterialState


class AssemblyLine:
    def __init__(
        self,
        station_cycle_times: list[float],
        buffer_capacities: list[int],
        initial_time: float = 0.0,
        material_types: list[str] | None = None,
        release_limit: int | None = None,
        routes: dict[str, list[str]] | None = None,
        setup_times: dict[str, float] | None = None,
        inspection_stations: list[str] | None = None,
        max_reworks: int = 1,
    ) -> None:
        if len(station_cycle_times) < 1:
            raise ValueError("at least one station is required")
        if len(buffer_capacities) != len(station_cycle_times) - 1:
            raise ValueError("one buffer capacity is required between each station pair")
        self.clock = SimulationClock(initial_time)
        self.event_queue = EventQueue()
        self.source = MaterialSource(material_types=material_types, release_limit=release_limit)
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
        self.scrapped_by_type: dict[str, int] = {}
        self.scrap_reasons: dict[str, str] = {}
        self.ledger: dict[str, str] = {}
        self.archive: dict[str, Material] = {}
        station_names = {station.name for station in self.stations}
        self.routes: dict[str, list[str]] = {
            material_type: list(route) for material_type, route in (routes or {}).items()
        }
        for material_type, route in self.routes.items():
            unknown = [name for name in route if name not in station_names]
            if unknown:
                raise ValueError(f"route for {material_type!r} references unknown stations: {unknown}")
        self.setup_times = dict(setup_times or {})
        unknown_setup = [name for name in self.setup_times if name not in station_names]
        if unknown_setup:
            raise ValueError(f"setup times reference unknown stations: {unknown_setup}")
        self.inspection_stations = list(inspection_stations or [])
        unknown_inspection = [name for name in self.inspection_stations if name not in station_names]
        if unknown_inspection:
            raise ValueError(f"inspection references unknown stations: {unknown_inspection}")
        if max_reworks < 0:
            raise ValueError("max reworks cannot be negative")
        self.max_reworks = max_reworks
        self.dispositions: dict[str, DispositionRecord] = {}
        self.disposition_reasons: dict[str, str] = {}
        self.applied_dispositions: list[str] = []
        self.ignored_results: list[dict] = []
        self._refresh()

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

    def _route_for(self, material: Material) -> list[str] | None:
        return self.routes.get(material.material_type)

    def _visit_for(self, material: Material, station_name: str) -> int:
        visits = sum(
            1
            for entry in material.history
            if entry.station == station_name and entry.action in ("process_start", "rework_start")
        )
        return visits + 1

    @staticmethod
    def _inspection_key(serial_number: str, station_name: str, visit: int) -> str:
        return f"{serial_number}@{station_name}#{visit}"

    def _refresh(self) -> None:
        changed = True
        while changed:
            changed = False
            for index in range(len(self.stations) - 1, -1, -1):
                station = self.stations[index]
                if station.state == DeviceState.BLOCKED and station.remaining_time == 0:
                    material = station.material
                    serial_number = material.serial_number if material else None
                    if station.try_release(self._downstream_for(index)):
                        if index == len(self.stations) - 1:
                            self.ledger[serial_number] = MaterialState.COMPLETED.value
                            material.record(
                                self.clock.time, "sink", 0, "completed",
                                material_type=material.material_type,
                            )
                            self.archive[serial_number] = material
                        else:
                            self.ledger[serial_number] = MaterialState.WAITING.value
                            material.record(
                                self.clock.time,
                                self.buffers[index].name,
                                0,
                                "buffered",
                            )
                        changed = True

            for index in range(len(self.stations) - 1, -1, -1):
                station = self.stations[index]
                upstream = self._upstream_for(index)
                downstream = self._downstream_for(index)
                if station.is_idle and upstream.can_take() and downstream.can_accept():
                    self._start_station(index)
                    changed = True

    def _start_station(self, index: int) -> None:
        station = self.stations[index]
        upstream = self._upstream_for(index)
        material = upstream.take()
        route = self._route_for(material)
        transit = route is not None and (
            material.route_step >= len(route) or route[material.route_step] != station.name
        )
        if transit:
            station.start_transit(material)
            material.record(self.clock.time, station.name, 0, "transit")
            self.ledger[material.serial_number] = MaterialState.IN_TRANSIT.value
            return
        setup_time = self.setup_times.get(station.name, 0.0)
        visit = self._visit_for(material, station.name)
        station.start(material, self.clock.time, self._schedule, setup_time)
        material.record(
            self.clock.time,
            station.name,
            visit,
            "process_start",
            setup_time=setup_time,
            cycle_time=station.cycle_time,
        )
        self.ledger[material.serial_number] = MaterialState.IN_PROCESS.value

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

    def deliver_inspection(
        self,
        serial_number: str,
        station_name: str,
        visit: int,
        result: str,
        reason: str | None = None,
    ) -> bool:
        """送达抽检结果。返回 True 表示该结果被接受（首次或更正）。

        同一物料同一工位同一轮次的重复或迟到结果会被忽略并记录，
        不会触发重复加工或重复投料。
        """
        if result not in ("pass", "rework", "scrap"):
            raise ValueError(f"unknown inspection result: {result}")
        key = self._inspection_key(serial_number, station_name, visit)
        if key in self.applied_dispositions:
            self.ignored_results.append(
                {
                    "key": key,
                    "time": self.clock.time,
                    "result": result,
                    "reason": "duplicate_or_late",
                }
            )
            return False
        if key in self.dispositions:
            record = self.dispositions[key]
            record.result = result
            record.time = self.clock.time
        else:
            self.dispositions[key] = DispositionRecord(
                key=key,
                time=self.clock.time,
                station=station_name,
                visit=visit,
                result=result,
            )
        if reason:
            self.disposition_reasons[key] = reason
        self._apply_pending_dispositions()
        return True

    def _apply_pending_dispositions(self) -> None:
        for station in self.stations:
            if not station.awaiting_inspection or station.material is None:
                continue
            material = station.material
            visit = self._visit_for(material, station.name) - 1
            key = self._inspection_key(material.serial_number, station.name, visit)
            record = self.dispositions.get(key)
            if record is None or key in self.applied_dispositions:
                continue
            self._apply_disposition(station, material, visit, record)
        self._refresh()

    def _apply_disposition(
        self,
        station: Station,
        material: Material,
        visit: int,
        record: DispositionRecord,
    ) -> None:
        now = self.clock.time
        self.applied_dispositions.append(record.key)
        if record.result == "pass":
            material.record(now, station.name, visit, "inspection_pass")
            material.state = MaterialState.IN_PROCESS
            station.awaiting_inspection = False
            return
        if record.result == "scrap":
            self._scrap_at_station(
                station,
                material,
                visit,
                self.disposition_reasons.get(record.key, "inspection_failed"),
            )
            return
        if material.rework_count >= self.max_reworks:
            self._scrap_at_station(
                station, material, visit, "rework_limit_exceeded"
            )
            return
        material.rework_count += 1
        record.rework_count_after = material.rework_count
        material.record(
            now,
            station.name,
            visit + 1,
            "rework_start",
            rework_count=material.rework_count,
        )
        material.state = MaterialState.IN_PROCESS
        station.awaiting_inspection = False
        station.state = DeviceState.PROCESSING
        station.remaining_time = station.cycle_time
        station.completion_event = self._schedule(
            now + station.cycle_time,
            "PROCESS_COMPLETE",
            station.name,
            {"material": material.serial_number},
        )

    def _scrap_at_station(
        self,
        station: Station,
        material: Material,
        visit: int,
        reason: str,
    ) -> None:
        now = self.clock.time
        material.record(now, station.name, visit, "scrapped", reason=reason)
        material.state = MaterialState.SCRAPPED
        material.location = "scrap"
        self.scrap_reasons[material.serial_number] = reason
        self.scrapped += 1
        material_type = material.material_type
        self.scrapped_by_type[material_type] = self.scrapped_by_type.get(material_type, 0) + 1
        self.ledger[material.serial_number] = MaterialState.SCRAPPED.value
        self.archive[material.serial_number] = material
        station.material = None
        station.remaining_time = None
        station.completion_event = None
        station.setup_event = None
        station.awaiting_inspection = False
        station.state = DeviceState.IDLE

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
            scrapped.record(event.time, station.name, 0, "scrapped", reason="station_fault")
            self.scrap_reasons[scrapped.serial_number] = "station_fault"
            self.scrapped += 1
            material_type = scrapped.material_type
            self.scrapped_by_type[material_type] = self.scrapped_by_type.get(material_type, 0) + 1
            self.ledger[scrapped.serial_number] = MaterialState.SCRAPPED.value
            self.archive[scrapped.serial_number] = scrapped
        self._schedule(
            event.time + event.payload["duration"],
            "STATION_REPAIRED",
            station.name,
        )

    def _handle_repaired(self, event: Event) -> None:
        index = self._station_index(event.source)
        station = self.stations[index]
        station.repair(event.time, self._downstream_for(index), self._schedule)
        self._refresh()

    def _handle_setup_complete(self, event: Event) -> None:
        station = self.stations[self._station_index(event.source)]
        station.setup_complete(event.time, self._schedule)

    def _handle_complete(self, event: Event) -> None:
        index = self._station_index(event.source)
        station = self.stations[index]
        station.complete()
        material = station.material
        if material is not None and material.state != MaterialState.IN_TRANSIT:
            route = self._route_for(material)
            if (
                route is not None
                and material.route_step < len(route)
                and route[material.route_step] == station.name
            ):
                material.route_step += 1
            visit = self._visit_for(material, station.name) - 1
            material.record(event.time, station.name, visit, "process_complete")
            if station.name in self.inspection_stations:
                material.state = MaterialState.PENDING_INSPECTION
                station.awaiting_inspection = True
                material.record(event.time, station.name, visit, "inspection_pending")
                self.ledger[material.serial_number] = MaterialState.PENDING_INSPECTION.value
        self._apply_pending_dispositions()
        self._refresh()

    def _process_next(self) -> Event:
        event = self.event_queue.pop()
        if event is None:
            raise RuntimeError("no pending events")
        self.clock.advance(event.time)
        if event.kind == "PROCESS_COMPLETE":
            self._handle_complete(event)
        elif event.kind == "SETUP_COMPLETE":
            self._handle_setup_complete(event)
        elif event.kind == "STATION_FAULT":
            self._handle_fault(event)
        elif event.kind == "STATION_REPAIRED":
            self._handle_repaired(event)
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
        line.scrapped_by_type = dict(data.get("scrapped_by_type", {}))
        line.scrap_reasons = dict(data.get("scrap_reasons", {}))
        line.ledger = data["ledger"]
        line.archive = {
            key: Material.from_dict(value)
            for key, value in data.get("archive", {}).items()
        }
        line.routes = {key: list(value) for key, value in data.get("routes", {}).items()}
        line.setup_times = dict(data.get("setup_times", {}))
        line.inspection_stations = list(data.get("inspection_stations", []))
        line.max_reworks = data.get("max_reworks", 1)
        line.dispositions = {
            key: DispositionRecord.from_dict(value)
            for key, value in data.get("dispositions", {}).items()
        }
        line.disposition_reasons = dict(data.get("disposition_reasons", {}))
        line.applied_dispositions = list(data.get("applied_dispositions", []))
        line.ignored_results = list(data.get("ignored_results", []))
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
            "scrapped_by_type": self.scrapped_by_type,
            "scrap_reasons": self.scrap_reasons,
            "ledger": self.ledger,
            "archive": {key: material.to_dict() for key, material in self.archive.items()},
            "routes": self.routes,
            "setup_times": self.setup_times,
            "inspection_stations": self.inspection_stations,
            "max_reworks": self.max_reworks,
            "dispositions": {key: record.to_dict() for key, record in self.dispositions.items()},
            "disposition_reasons": self.disposition_reasons,
            "applied_dispositions": self.applied_dispositions,
            "ignored_results": self.ignored_results,
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

    def _wip_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for station in self.stations:
            if station.material is not None:
                material_type = station.material.material_type
                counts[material_type] = counts.get(material_type, 0) + 1
        for buffer in self.buffers:
            for material in buffer.items:
                counts[material.material_type] = counts.get(material.material_type, 0) + 1
        return counts

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

    def reconcile_by_type(self) -> dict[str, dict[str, int]]:
        material_types = set(self.source.released_by_type)
        material_types.update(self.sink.accepted_by_type)
        material_types.update(self.scrapped_by_type)
        material_types.update(self._wip_by_type())
        wip = self._wip_by_type()
        report: dict[str, dict[str, int]] = {}
        for material_type in sorted(material_types):
            released = self.source.released_by_type.get(material_type, 0)
            completed = self.sink.accepted_by_type.get(material_type, 0)
            in_progress = wip.get(material_type, 0)
            scrapped = self.scrapped_by_type.get(material_type, 0)
            report[material_type] = {
                "released": released,
                "completed": completed,
                "work_in_progress": in_progress,
                "scrapped": scrapped,
                "accounted": completed + in_progress + scrapped,
                "balanced": released == completed + in_progress + scrapped,
            }
        return report

    def accounts_balanced(self) -> bool:
        counts = self.reconcile()
        if counts["released"] != counts["accounted"]:
            return False
        if len(self.ledger) != counts["accounted"]:
            return False
        return all(entry["balanced"] for entry in self.reconcile_by_type().values())

    def status(self) -> dict:
        return {
            "time": self.clock.time,
            "stations": [
                {
                    "name": station.name,
                    "state": station.state.value,
                    "material": station.material.serial_number if station.material else None,
                    "remaining_time": station.remaining_time,
                    "awaiting_inspection": station.awaiting_inspection,
                }
                for station in self.stations
            ],
            "buffers": [
                {"name": buffer.name, "occupied": len(buffer.items), "capacity": buffer.capacity}
                for buffer in self.buffers
            ],
            **self.reconcile(),
        }
