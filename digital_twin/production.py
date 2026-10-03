from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .engine import EventQueue, SimulationClock
from .material import MaterialState


@dataclass
class MaterialType:
    name: str
    route: list[str]
    max_rework: int = 0
    setup_time: float = 0.0
    setup_times: dict[str, float] = field(default_factory=dict)

    def setup_for(self, station: str) -> float:
        return float(self.setup_times.get(station, self.setup_time))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "route": list(self.route),
            "max_rework": self.max_rework,
            "setup_time": self.setup_time,
            "setup_times": dict(self.setup_times),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MaterialType":
        return cls(
            name=data["name"],
            route=list(data["route"]),
            max_rework=data.get("max_rework", 0),
            setup_time=data.get("setup_time", 0.0),
            setup_times=dict(data.get("setup_times", {})),
        )


@dataclass
class Material2:
    serial_number: str
    type_name: str
    state: MaterialState
    location: str
    next_step: int = 0
    rework_count: int = 0
    inspection_round: int = 0
    history: list[dict] = field(default_factory=list)
    inspections: list[dict] = field(default_factory=list)

    def record(self, time: float, event: str, location: str, **detail) -> None:
        entry: dict = {"time": time, "event": event, "location": location}
        if detail:
            entry["detail"] = {key: value for key, value in detail.items() if value is not None}
        self.history.append(entry)

    def to_dict(self) -> dict:
        return {
            "serial_number": self.serial_number,
            "type_name": self.type_name,
            "state": self.state.value,
            "location": self.location,
            "next_step": self.next_step,
            "rework_count": self.rework_count,
            "inspection_round": self.inspection_round,
            "history": self.history,
            "inspections": self.inspections,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Material2":
        return cls(
            serial_number=data["serial_number"],
            type_name=data["type_name"],
            state=MaterialState(data["state"]),
            location=data["location"],
            next_step=data.get("next_step", 0),
            rework_count=data.get("rework_count", 0),
            inspection_round=data.get("inspection_round", 0),
            history=list(data.get("history", [])),
            inspections=list(data.get("inspections", [])),
        )


@dataclass
class BufferSlot:
    material: Material2
    arrival_time: float

    def to_dict(self) -> dict:
        return {"material": self.material.to_dict(), "arrival_time": self.arrival_time}

    @classmethod
    def from_dict(cls, data: dict) -> "BufferSlot":
        return cls(Material2.from_dict(data["material"]), data["arrival_time"])


class Buffer2:
    def __init__(self, name: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("buffer capacity must be positive")
        self.name = name
        self.capacity = int(capacity)
        self.items: list[BufferSlot] = []

    @property
    def full(self) -> bool:
        return len(self.items) >= self.capacity

    def receive(self, material: Material2, now: float) -> None:
        if self.full:
            raise RuntimeError(f"{self.name} is full")
        material.state = MaterialState.WAITING
        material.location = self.name
        self.items.append(BufferSlot(material, now))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "capacity": self.capacity,
            "items": [slot.to_dict() for slot in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Buffer2":
        buffer = cls(data["name"], data["capacity"])
        buffer.items = [BufferSlot.from_dict(item) for item in data["items"]]
        return buffer


class PStation:
    def __init__(self, name: str, cycle_time: float) -> None:
        if cycle_time <= 0:
            raise ValueError("cycle time must be positive")
        self.name = name
        self.cycle_time = float(cycle_time)
        # idle | setup | processing | blocked
        self.state = "idle"
        self.material: Optional[Material2] = None
        self.prepared_type: Optional[str] = None
        self.setup_event: Optional[int] = None
        self.completion_event: Optional[int] = None
        self.blocked_time = 0.0
        self.busy_time = 0.0
        self.setup_time_used = 0.0
        self._last_time: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cycle_time": self.cycle_time,
            "state": self.state,
            "material": self.material.to_dict() if self.material else None,
            "prepared_type": self.prepared_type,
            "setup_event": self.setup_event,
            "completion_event": self.completion_event,
            "blocked_time": self.blocked_time,
            "busy_time": self.busy_time,
            "setup_time_used": self.setup_time_used,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PStation":
        station = cls(data["name"], data["cycle_time"])
        station.state = data["state"]
        station.material = Material2.from_dict(data["material"]) if data["material"] else None
        station.prepared_type = data.get("prepared_type")
        station.setup_event = data.get("setup_event")
        station.completion_event = data.get("completion_event")
        station.blocked_time = data.get("blocked_time", 0.0)
        station.busy_time = data.get("busy_time", 0.0)
        station.setup_time_used = data.get("setup_time_used", 0.0)
        return station


@dataclass
class InspectionPolicy:
    station: str
    sampling_rate: float = 1.0
    pass_rate: float = 1.0
    duration: float = 0.0
    rework_to: Optional[str] = None
    rework_entry: Optional[str] = None
    fail_decision: str = "scrap"  # rework | scrap
    on_limit: str = "scrap"  # scrap | concession | hold

    def to_dict(self) -> dict:
        return {
            "station": self.station,
            "sampling_rate": self.sampling_rate,
            "pass_rate": self.pass_rate,
            "duration": self.duration,
            "rework_to": self.rework_to,
            "rework_entry": self.rework_entry,
            "fail_decision": self.fail_decision,
            "on_limit": self.on_limit,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InspectionPolicy":
        return cls(
            station=data["station"],
            sampling_rate=data.get("sampling_rate", 1.0),
            pass_rate=data.get("pass_rate", 1.0),
            duration=data.get("duration", 0.0),
            rework_to=data.get("rework_to"),
            rework_entry=data.get("rework_entry"),
            fail_decision=data.get("fail_decision", "scrap"),
            on_limit=data.get("on_limit", "scrap"),
        )


@dataclass
class InspectionHold:
    material: Material2
    station: str
    entered_at: float
    round: int
    sampled: bool
    result: str = "pending"  # pending | pass | fail
    result_event: Optional[int] = None
    result_at: Optional[float] = None
    override: Optional[str] = None  # explicit on-limit disposition

    def to_dict(self) -> dict:
        return {
            "material": self.material.to_dict(),
            "station": self.station,
            "entered_at": self.entered_at,
            "round": self.round,
            "sampled": self.sampled,
            "result": self.result,
            "result_event": self.result_event,
            "result_at": self.result_at,
            "override": self.override,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "InspectionHold":
        return cls(
            material=Material2.from_dict(data["material"]),
            station=data["station"],
            entered_at=data["entered_at"],
            round=data["round"],
            sampled=data["sampled"],
            result=data.get("result", "pending"),
            result_event=data.get("result_event"),
            result_at=data.get("result_at"),
            override=data.get("override"),
        )


class ProductionLine:
    """Multi-type production line with setup, bounded buffers, rework and
    sampling inspection.

    Topology: every station owns an inbound *feed* buffer.  The feed buffer of
    a station may be filled by the material source, by the preceding route
    station, or by a rework loop.  Between two route stations an intermediate
    buffer keeps the classic blocking semantics.
    """

    SOURCE = "source"
    SINK = "sink"
    SCRAP = "scrap"

    def __init__(
        self,
        stations: dict[str, float],
        feed_capacity: int = 2,
        buffer_capacity: int = 2,
        rework_capacity: int = 2,
        seed: Optional[int] = None,
    ) -> None:
        self.clock = SimulationClock()
        self.event_queue = EventQueue()
        self.station_order = list(stations.keys())
        self.stations = {name: PStation(name, cycle) for name, cycle in stations.items()}
        self.types: dict[str, MaterialType] = {}
        self.policies: dict[str, InspectionPolicy] = {}
        self.feed_capacity = feed_capacity
        self.buffer_capacity = buffer_capacity
        self.rework_capacity = rework_capacity
        self.feed_buffers: dict[str, Buffer2] = {
            name: Buffer2(f"feed:{name}", feed_capacity) for name in self.station_order
        }
        self.step_buffers: dict[tuple[str, str], Buffer2] = {}
        self.rework_buffers: dict[str, Buffer2] = {}
        self.materials: dict[str, Material2] = {}
        self.holds: list[InspectionHold] = []
        self.releases: dict[str, int] = {}
        self.released_counts: dict[str, int] = {}
        self.completed_counts: dict[str, int] = {}
        self.scrapped_counts: dict[str, int] = {}
        self.concession_counts: dict[str, int] = {}
        self._next_number = 1
        self._rng = random.Random(seed)
        self._rng_state = self._rng.getstate()
        self._scheduled_releases: set[int] = set()
        self._release_backlog: dict[str, int] = {}
        self._refresh()

    # ------------------------------------------------------------------ setup
    def register_type(self, material_type: MaterialType) -> None:
        for station in material_type.route:
            if station not in self.stations:
                raise ValueError(f"unknown station in route: {station}")
        if material_type.name in self.types:
            raise ValueError(f"type already registered: {material_type.name}")
        self.types[material_type.name] = material_type
        self.releases.setdefault(material_type.name, 0)
        self.released_counts.setdefault(material_type.name, 0)
        self.completed_counts.setdefault(material_type.name, 0)
        self.scrapped_counts.setdefault(material_type.name, 0)
        self.concession_counts.setdefault(material_type.name, 0)
        self._ensure_route_buffers(material_type.route)

    def set_inspection(self, policy: InspectionPolicy) -> None:
        if policy.station not in self.stations:
            raise ValueError(f"unknown inspection station: {policy.station}")
        if policy.fail_decision == "rework" and not policy.rework_to:
            raise ValueError("rework policy needs a rework_to station")
        if policy.rework_to and policy.rework_to not in self.stations:
            raise ValueError(f"unknown rework target: {policy.rework_to}")
        self.policies[policy.station] = policy
        self.rework_buffers.setdefault(
            policy.station,
            Buffer2(f"rework:{policy.station}", self.rework_capacity),
        )

    def _ensure_route_buffers(self, route: list[str]) -> None:
        for previous, current in zip(route, route[1:]):
            key = (previous, current)
            self.step_buffers.setdefault(
                key, Buffer2(f"buffer:{previous}->{current}", self.buffer_capacity)
            )

    # ---------------------------------------------------------------- release
    def release(self, type_name: str, quantity: int = 1, at: Optional[float] = None) -> list[str]:
        """Schedule or immediately release new material.  Repeating a recorded
        release after save/load never creates duplicate materials.
        """
        if type_name not in self.types:
            raise ValueError(f"unknown type: {type_name}")
        if quantity < 0:
            raise ValueError("quantity must be non-negative")
        if at is not None and at > self.clock.time:
            uid = self._schedule(at, "RELEASE", self.SOURCE, {"type": type_name, "qty": quantity})
            self._scheduled_releases.add(uid)
            return []
        created: list[str] = []
        material_type = self.types[type_name]
        first_station = material_type.route[0]
        feed = self.feed_buffers[first_station]
        self._refresh()
        for _ in range(quantity):
            if feed.full:
                self._release_backlog[type_name] = (
                    self._release_backlog.get(type_name, 0) + 1
                )
                continue
            serial = f"{type_name}-{self._next_number:05d}"
            self._next_number += 1
            material = Material2(serial, type_name, MaterialState.WAITING, feed.name, next_step=0)
            material.record(self.clock.time, "released", feed.name)
            self.materials[serial] = material
            self.released_counts[type_name] = self.released_counts.get(type_name, 0) + 1
            feed.receive(material, self.clock.time)
            created.append(serial)
            self._refresh()
        self._refresh()
        return created

    # ------------------------------------------------------------------ events
    def _schedule(self, time: float, kind: str, source: str, payload: dict | None = None) -> int:
        return self.event_queue.schedule(time, kind, source, payload or {})

    def _cancel(self, uid: Optional[int]) -> None:
        if uid is not None:
            self.event_queue.cancel(uid)

    # ------------------------------------------------------------------ flow
    def _policy_for(self, material: Material2, station: str) -> Optional[InspectionPolicy]:
        policy = self.policies.get(station)
        if policy is None:
            return None
        if material.type_name not in self.types:
            return None
        return policy

    def _station_inbound(self, station: str):
        candidates = [self.feed_buffers[station]]
        for key, buffer in self.step_buffers.items():
            if key[1] == station:
                candidates.append(buffer)
        policy = next((item for item in self.policies.values() if item.rework_to == station), None)
        if policy is not None:
            candidates.append(self.rework_buffers[policy.station])
        return candidates

    def _accumulate_station_times(self) -> None:
        now = self.clock.time
        for station in self.stations.values():
            if station._last_time is None:
                station._last_time = now
                continue
            delta = now - station._last_time
            station._last_time = now
            if station.state == "blocked":
                station.blocked_time += delta
            elif station.state == "processing":
                station.busy_time += delta
            elif station.state == "setup":
                station.setup_time_used += delta

    def _start_available(self) -> bool:
        changed = False
        for name in self.station_order:
            station = self.stations[name]
            if station.state != "idle" or station.material is not None:
                continue
            inbound = None
            earliest = None
            for buffer in self._station_inbound(name):
                if buffer.items and (earliest is None or buffer.items[0].arrival_time < earliest):
                    inbound = buffer
                    earliest = buffer.items[0].arrival_time
            if inbound is None:
                continue
            if not self._has_output_capacity(name, inbound.items[0].material):
                continue
            slot = inbound.items[0]
            slot.material.state = MaterialState.IN_PROCESS
            station.material = inbound.items.pop(0).material
            material = station.material
            station.state = "setup"
            material_type = self.types[material.type_name]
            setup_time = 0.0
            if station.prepared_type != material.type_name:
                setup_time = material_type.setup_for(name)
            material.record(self.clock.time, "enter_station", name,
                            step=material.next_step, rework=material.rework_count)
            if setup_time > 0:
                station.prepared_type = material.type_name
                station.setup_event = self._schedule(
                    self.clock.time + setup_time, "SETUP_COMPLETE", name,
                    {"material": material.serial_number},
                )
            else:
                self._begin_processing(station)
            changed = True
        return changed

    def _has_output_capacity(self, station_name: str, material: Material2) -> bool:
        policy = self.policies.get(station_name)
        if policy is not None:
            # Inspection position is the station itself; only one item at a time.
            return not any(hold.station == station_name for hold in self.holds)
        route = self.types[material.type_name].route
        step = material.next_step
        if step == len(route) - 1:
            return True  # sink always accepts
        next_station = route[step + 1]
        return not self.step_buffers[(station_name, next_station)].full

    def _begin_processing(self, station: PStation) -> None:
        station.setup_event = None
        station.state = "processing"
        material = station.material
        station.completion_event = self._schedule(
            self.clock.time + station.cycle_time, "PROCESS_COMPLETE", station.name,
            {"material": material.serial_number},
        )

    def _try_departure(self, station: PStation) -> bool:
        material = station.material
        if material is None or station.state != "blocked":
            return False
        route = self.types[material.type_name].route
        step = material.next_step
        # Inspection gate after this station?
        policy = self.policies.get(station.name)
        if policy is not None:
            hold = next((item for item in self.holds
                         if item.material.serial_number == material.serial_number
                         and item.round == material.inspection_round), None)
            if hold is not None:
                return False  # parked in inspection area, not on the station
        if step == len(route) - 1:
            target = self.SINK
        else:
            next_station = route[step + 1]
            buffer = self.step_buffers[(station.name, next_station)]
            if buffer.full:
                return False
            target = buffer.name
        material.next_step = step + 1
        if target == self.SINK:
            material.state = MaterialState.COMPLETED
            material.location = self.SINK
            self.completed_counts[material.type_name] = (
                self.completed_counts.get(material.type_name, 0) + 1
            )
            material.record(self.clock.time, "completed", self.SINK)
        else:
            buffer.receive(material, self.clock.time)
            material.record(self.clock.time, "move", target)
        station.material = None
        station.completion_event = None
        station.state = "idle"
        return True

    def _refresh(self) -> None:
        changed = True
        while changed:
            changed = False
            changed |= self._resolve_holds()
            for station in self.stations.values():
                if self._try_departure(station):
                    changed = True
            changed |= self._start_available()
            changed |= self._feed_backlog()

    def _feed_backlog(self) -> bool:
        changed = False
        for type_name, remaining in list(self._release_backlog.items()):
            route = self.types[type_name].route
            feed = self.feed_buffers[route[0]]
            while remaining > 0 and not feed.full:
                serial = f"{type_name}-{self._next_number:05d}"
                self._next_number += 1
                material = Material2(serial, type_name, MaterialState.WAITING, feed.name, next_step=0)
                material.record(self.clock.time, "released", feed.name)
                self.materials[serial] = material
                self.released_counts[type_name] = self.released_counts.get(type_name, 0) + 1
                feed.receive(material, self.clock.time)
                remaining -= 1
                changed = True
            self._release_backlog[type_name] = remaining
        return changed

    # ------------------------------------------------------------- inspection
    def _enter_inspection(self, station: PStation) -> None:
        material = station.material
        policy = self.policies[station.name]
        material.inspection_round += 1
        sampled = self._rng.random() < policy.sampling_rate
        location = f"inspection:{station.name}"
        material.state = MaterialState.WAITING
        material.location = location
        hold = InspectionHold(
            material=material,
            station=station.name,
            entered_at=self.clock.time,
            round=material.inspection_round,
            sampled=sampled,
        )
        self.holds.append(hold)
        material.record(self.clock.time, "inspection_enter", location,
                        round=hold.round, sampled=sampled)
        if not sampled:
            hold.result = "pass"
            material.inspections.append({
                "station": station.name, "time": self.clock.time,
                "sampled": False, "result": "pass", "round": hold.round,
            })
            return
        material.inspections.append({
            "station": station.name, "time": self.clock.time,
            "sampled": True, "result": "pending", "round": hold.round,
        })
        if policy.duration > 0:
            passed = self._rng.random() < policy.pass_rate
            hold.result_event = self._schedule(
                self.clock.time + policy.duration, "INSPECTION_RESULT", station.name,
                {"material": material.serial_number, "round": hold.round,
                 "passed": passed},
            )

    def _resolve_holds(self) -> bool:
        changed = False
        remaining: list[InspectionHold] = []
        for hold in self.holds:
            if hold.result == "pending":
                remaining.append(hold)
                continue
            done = self._apply_hold_decision(hold)
            if not done:
                remaining.append(hold)
            else:
                changed = True
        self.holds = remaining
        return changed

    def _apply_hold_decision(self, hold: InspectionHold) -> bool:
        material = hold.material
        station = self.stations[hold.station]
        policy = self.policies[hold.station]
        location = f"inspection:{hold.station}"
        if hold.result == "pass":
            self._mark_inspection_result(material, hold, "pass")
            material.state = MaterialState.IN_PROCESS
            material.location = hold.station
            station.state = "blocked"
            return True
        # failed inspection
        limit_reached = material.rework_count >= self.types[material.type_name].max_rework
        decision = policy.fail_decision
        if limit_reached:
            decision = hold.override or policy.on_limit
        if decision == "hold":
            return False  # wait for explicit disposition
        if decision == "rework":
            target = policy.rework_to
            rework_buffer = self.rework_buffers[hold.station]
            if rework_buffer.full:
                return False  # keep occupying the inspection position: blocks upstream
            self._mark_inspection_result(material, hold, "fail")
            material.rework_count += 1
            route = self.types[material.type_name].route
            material.next_step = route.index(target)
            rework_buffer.receive(material, self.clock.time)
            material.record(self.clock.time, "rework_start", rework_buffer.name,
                            rework=material.rework_count, return_to=target)
            station.material = None
            station.completion_event = None
            station.state = "idle"
            return True
        if decision == "concession":
            self._mark_inspection_result(material, hold, "concession")
            self._finish_with_concession(station, material)
            return True
        # scrap
        self._mark_inspection_result(material, hold, "fail")
        material.state = MaterialState.SCRAPPED
        material.location = self.SCRAP
        self.scrapped_counts[material.type_name] = (
            self.scrapped_counts.get(material.type_name, 0) + 1
        )
        material.record(self.clock.time, "scrapped", self.SCRAP,
                        reason="inspection_fail", rework=material.rework_count)
        station.material = None
        station.completion_event = None
        station.state = "idle"
        return True

    def _finish_with_concession(self, station: PStation, material: Material2) -> None:
        material.state = MaterialState.COMPLETED
        material.location = self.SINK
        material.next_step = len(self.types[material.type_name].route)
        self.concession_counts[material.type_name] = (
            self.concession_counts.get(material.type_name, 0) + 1
        )
        self.completed_counts[material.type_name] = (
            self.completed_counts.get(material.type_name, 0) + 1
        )
        material.record(self.clock.time, "concession_release", self.SINK,
                        station=station.name)
        station.material = None
        station.completion_event = None
        station.state = "idle"

    @staticmethod
    def _mark_inspection_result(material: Material2, hold: InspectionHold, result: str) -> None:
        for entry in reversed(material.inspections):
            if entry.get("round") == hold.round and entry.get("station") == hold.station:
                if entry.get("result") == "pending":
                    entry["result"] = result
                break
        else:
            material.inspections.append({
                "station": hold.station, "time": hold.entered_at,
                "sampled": True, "result": result, "round": hold.round,
            })

    def record_inspection(self, serial_number: str, passed: bool) -> None:
        """Manually resolve a sampled item (duration == 0 policies).
        Repeating the same decision is ignored, so re-reading a saved file
        never triggers a second inspection.
        """
        hold = next((item for item in self.holds
                     if item.material.serial_number == serial_number
                     and item.result == "pending" and item.sampled), None)
        if hold is None:
            return
        if hold.result_event is not None:
            return  # auto result already scheduled
        hold.result = "pass" if passed else "fail"
        material = hold.material
        material.record(self.clock.time, "inspection_decision", material.location,
                        round=hold.round, result=hold.result)
        self._refresh()

    def dispose(self, serial_number: str, decision: str) -> None:
        """Explicit disposition (scrap/concession/rework) for an item parked by
        an ``on_limit='hold'`` policy."""
        if decision not in {"scrap", "concession", "rework"}:
            raise ValueError("decision must be scrap, concession or rework")
        hold = next((item for item in self.holds
                     if item.material.serial_number == serial_number), None)
        if hold is None or hold.result == "pass":
            return
        material = hold.material
        if decision == "rework":
            if material.rework_count >= self.types[material.type_name].max_rework:
                raise ValueError("rework limit reached; choose scrap or concession")
            hold.result = "fail"
            hold.override = "rework"
        elif decision == "scrap":
            hold.result = "fail"
            hold.override = "scrap"
        else:
            hold.result = "fail"
            hold.override = "concession"
        self._refresh()

    # ------------------------------------------------------------ run / events
    def run_until(self, end_time: float) -> None:
        while True:
            next_time = self.event_queue.next_time()
            if next_time is None or next_time > end_time:
                break
            event = self.event_queue.pop()
            self._accumulate_station_times()
            self.clock.advance(event.time)
            self._dispatch(event)
            self._refresh()
        self._accumulate_station_times()
        self.clock.advance(end_time)
        self._refresh()

    def _dispatch(self, event) -> None:
        if event.kind == "SETUP_COMPLETE":
            self._begin_processing(self.stations[event.source])
        elif event.kind == "PROCESS_COMPLETE":
            station = self.stations[event.source]
            station.busy_time += 0.0
            station.state = "blocked"
            if event.source in self.policies:
                self._enter_inspection(station)
        elif event.kind == "INSPECTION_RESULT":
            hold = next((item for item in self.holds
                         if item.material.serial_number == event.payload["material"]
                         and item.round == event.payload["round"]), None)
            if hold is not None and hold.result == "pending":
                hold.result = "pass" if event.payload["passed"] else "fail"
                hold.result_at = event.time
        elif event.kind == "RELEASE":
            uid = event.uid
            self._scheduled_releases.discard(uid)
            self.release(event.payload["type"], event.payload["qty"])
        else:
            raise ValueError(f"unknown event kind: {event.kind}")

    # ------------------------------------------------------------- accounting
    @property
    def released(self) -> int:
        return sum(self.released_counts.values())

    @property
    def completed(self) -> int:
        return sum(self.completed_counts.values())

    @property
    def scrapped(self) -> int:
        return sum(self.scrapped_counts.values())

    @property
    def concessions(self) -> int:
        return sum(self.concession_counts.values())

    @property
    def work_in_progress(self) -> int:
        total = 0
        held = {hold.material.serial_number for hold in self.holds}
        for station in self.stations.values():
            if station.material is not None and station.material.serial_number not in held:
                total += 1
        total += sum(len(buffer.items) for buffer in self.feed_buffers.values())
        total += sum(len(buffer.items) for buffer in self.step_buffers.values())
        total += sum(len(buffer.items) for buffer in self.rework_buffers.values())
        total += len(self.holds)
        return total

    def reconcile(self) -> dict:
        result = {}
        held = {hold.material.serial_number: hold.material.type_name for hold in self.holds}
        for name, material_type in self.types.items():
            in_progress = 0
            for station in self.stations.values():
                if (
                    station.material is not None
                    and station.material.type_name == name
                    and station.material.serial_number not in held
                ):
                    in_progress += 1
            for buffers in (self.feed_buffers.values(), self.step_buffers.values(),
                            self.rework_buffers.values()):
                for buffer in buffers:
                    in_progress += sum(
                        1 for slot in buffer.items if slot.material.type_name == name
                    )
            in_progress += sum(1 for hold in self.holds if hold.material.type_name == name)
            result[name] = {
                "released": self.released_counts.get(name, 0),
                "completed": self.completed_counts.get(name, 0),
                "work_in_progress": in_progress,
                "scrapped": self.scrapped_counts.get(name, 0),
                "concessions": self.concession_counts.get(name, 0),
            }
            result[name]["accounted"] = (
                result[name]["completed"]
                + result[name]["work_in_progress"]
                + result[name]["scrapped"]
            )
        totals = {
            "released": self.released,
            "completed": self.completed,
            "work_in_progress": self.work_in_progress,
            "scrapped": self.scrapped,
            "concessions": self.concessions,
        }
        totals["accounted"] = (
            totals["completed"] + totals["work_in_progress"] + totals["scrapped"]
        )
        result["total"] = totals
        return result

    def accounts_balanced(self) -> bool:
        counts = self.reconcile()
        if counts["total"]["released"] != counts["total"]["accounted"]:
            return False
        for name in self.types:
            if counts[name]["released"] != counts[name]["accounted"]:
                return False
        # Every material is counted exactly once.
        live = [m for m in self.materials.values()
                if m.state not in (MaterialState.COMPLETED, MaterialState.SCRAPPED)]
        if len(live) != self.work_in_progress:
            return False
        done = [m for m in self.materials.values() if m.state == MaterialState.COMPLETED]
        if len(done) != self.completed:
            return False
        gone = [m for m in self.materials.values() if m.state == MaterialState.SCRAPPED]
        return len(gone) == self.scrapped

    def location_of(self, serial_number: str) -> dict:
        material = self.materials[serial_number]
        return {
            "serial_number": serial_number,
            "type": material.type_name,
            "state": material.state.value,
            "location": material.location,
            "next_step": material.next_step,
            "rework_count": material.rework_count,
        }

    def pending_inspections(self) -> list[dict]:
        return [
            {
                "serial_number": hold.material.serial_number,
                "station": hold.station,
                "location": hold.material.location,
                "sampled": hold.sampled,
                "result": hold.result,
                "entered_at": hold.entered_at,
                "round": hold.round,
            }
            for hold in self.holds
        ]

    def trace(self, serial_number: str) -> dict:
        material = self.materials[serial_number]
        return {
            "serial_number": serial_number,
            "type": material.type_name,
            "state": material.state.value,
            "location": material.location,
            "rework_count": material.rework_count,
            "history": list(material.history),
            "inspections": list(material.inspections),
        }

    def status(self) -> dict:
        return {
            "time": self.clock.time,
            "stations": [station.to_dict() for station in self.stations.values()],
            "holds": self.pending_inspections(),
            **self.reconcile(),
        }

    def metrics(self) -> dict:
        return {
            "time": self.clock.time,
            "released": self.released,
            "completed": self.completed,
            "scrapped": self.scrapped,
            "concessions": self.concessions,
            "work_in_progress": self.work_in_progress,
            "throughput": self.completed / self.clock.time if self.clock.time else 0.0,
            "blocked_time": {name: s.blocked_time for name, s in self.stations.items()},
            "busy_time": {name: s.busy_time for name, s in self.stations.items()},
            "setup_time": {name: s.setup_time_used for name, s in self.stations.items()},
            "waiting_inspection": len(self.holds),
        }

    # --------------------------------------------------------------- compare
    @staticmethod
    def compare_scenarios(build, scenarios: dict, end_time: float) -> dict:
        """Run the same build() with different inspection scenarios.

        ``build(policy_overrides)`` must return a fresh, un-run ProductionLine.
        """
        report = {}
        for name, overrides in scenarios.items():
            line = build(overrides)
            line.run_until(end_time)
            report[name] = line.metrics()
        return report

    # ------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        rng = self._rng.getstate()
        return {
            "clock": self.clock.to_dict(),
            "event_queue": self.event_queue.to_dict(),
            "station_order": self.station_order,
            "stations": {name: station.to_dict() for name, station in self.stations.items()},
            "types": {name: item.to_dict() for name, item in self.types.items()},
            "policies": {name: item.to_dict() for name, item in self.policies.items()},
            "capacities": {
                "feed": self.feed_capacity,
                "buffer": self.buffer_capacity,
                "rework": self.rework_capacity,
            },
            "feed_buffers": {name: item.to_dict() for name, item in self.feed_buffers.items()},
            "step_buffers": {f"{a}>{b}": item.to_dict() for (a, b), item in self.step_buffers.items()},
            "rework_buffers": {name: item.to_dict() for name, item in self.rework_buffers.items()},
            "materials": {serial: material.to_dict() for serial, material in self.materials.items()},
            "holds": [hold.to_dict() for hold in self.holds],
            "released_counts": self.released_counts,
            "completed_counts": self.completed_counts,
            "scrapped_counts": self.scrapped_counts,
            "concession_counts": self.concession_counts,
            "release_backlog": self._release_backlog,
            "next_number": self._next_number,
            "rng": {"version": rng[0], "state": list(rng[1]),
                    "gauss": list(rng[2]) if rng[2] is not None else None},
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        if target.parent:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ProductionLine":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        line = cls.__new__(cls)
        line.clock = SimulationClock.from_dict(data["clock"])
        line.event_queue = EventQueue.from_dict(data["event_queue"])
        line.station_order = list(data["station_order"])
        line.stations = {name: PStation.from_dict(item) for name, item in data["stations"].items()}
        line.types = {name: MaterialType.from_dict(item) for name, item in data["types"].items()}
        line.policies = {name: InspectionPolicy.from_dict(item)
                         for name, item in data.get("policies", {}).items()}
        caps = data["capacities"]
        line.feed_capacity = caps["feed"]
        line.buffer_capacity = caps["buffer"]
        line.rework_capacity = caps["rework"]
        line.feed_buffers = {name: Buffer2.from_dict(item)
                             for name, item in data["feed_buffers"].items()}
        line.step_buffers = {}
        for key, item in data["step_buffers"].items():
            previous, current = key.split(">")
            line.step_buffers[(previous, current)] = Buffer2.from_dict(item)
        line.rework_buffers = {name: Buffer2.from_dict(item)
                               for name, item in data.get("rework_buffers", {}).items()}
        line.materials = {serial: Material2.from_dict(item)
                          for serial, item in data["materials"].items()}
        line.holds = [InspectionHold.from_dict(item) for item in data.get("holds", [])]
        line.released_counts = dict(data.get("released_counts", {}))
        line.completed_counts = dict(data.get("completed_counts", {}))
        line.scrapped_counts = dict(data.get("scrapped_counts", {}))
        line.concession_counts = dict(data.get("concession_counts", {}))
        line._release_backlog = dict(data.get("release_backlog", {}))
        line._next_number = data.get("next_number", 1)
        rng_data = data.get("rng")
        line._rng = random.Random()
        if rng_data is not None:
            line._rng.setstate((
                rng_data["version"],
                tuple(rng_data["state"]),
                tuple(rng_data["gauss"]) if rng_data.get("gauss") is not None else None,
            ))
        line._scheduled_releases = {
            event.uid for event in line.event_queue._events.values() if event.kind == "RELEASE"
        }
        for station in line.stations.values():
            station._last_time = line.clock.time
        # Rebind every material reference to the single ledger instance so a
        # resumed part keeps one identity across stations, buffers and holds.
        for station in line.stations.values():
            if station.material is not None:
                station.material = line.materials[station.material.serial_number]
        for buffer in list(line.feed_buffers.values()) + list(line.step_buffers.values()) \
                + list(line.rework_buffers.values()):
            for slot in buffer.items:
                slot.material = line.materials[slot.material.serial_number]
        for hold in line.holds:
            hold.material = line.materials[hold.material.serial_number]
        return line
