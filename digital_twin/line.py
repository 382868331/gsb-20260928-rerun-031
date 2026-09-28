from __future__ import annotations

import json
from pathlib import Path

from .devices import Buffer, DeviceState, MaterialReceiver, MaterialSource, Sink, Station
from .engine import Event, EventQueue, SimulationClock
from .material import MaterialState


class AssemblyLine:
    def __init__(
        self,
        station_cycle_times: list[float],
        buffer_capacities: list[int],
        initial_time: float = 0.0,
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

    def _refresh(self) -> None:
        changed = True
        while changed:
            changed = False
            for index in range(len(self.stations) - 1, -1, -1):
                station = self.stations[index]
                if station.state == DeviceState.BLOCKED and station.remaining_time == 0:
                    serial_number = station.material.serial_number if station.material else None
                    if station.try_release(self._downstream_for(index)):
                        if index == len(self.stations) - 1:
                            self.ledger[serial_number] = MaterialState.COMPLETED.value
                        else:
                            self.ledger[serial_number] = MaterialState.WAITING.value
                        changed = True

            for index in range(len(self.stations) - 1, -1, -1):
                station = self.stations[index]
                upstream = self._upstream_for(index)
                downstream = self._downstream_for(index)
                if station.is_idle and upstream.can_take() and downstream.can_accept():
                    station.start(upstream, self.clock.time, self._schedule)
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
            self.scrapped += 1
            self.ledger[scrapped.serial_number] = MaterialState.SCRAPPED.value
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

    def _handle_complete(self, event: Event) -> None:
        station = self.stations[self._station_index(event.source)]
        station.complete()
        self._refresh()

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
