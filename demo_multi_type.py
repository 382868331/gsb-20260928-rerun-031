from __future__ import annotations

import json
from pathlib import Path

from digital_twin import AssemblyLine


STATE_PATH = Path(__file__).with_name("paused_multi_type_line.json")


def current_visit(line: AssemblyLine, serial: str, station_name: str) -> int:
    for station in line.stations:
        material = station.material
        if material is not None and material.serial_number == serial:
            return sum(
                1
                for entry in material.history
                if entry.station == station_name
                and entry.action in ("process_start", "rework_start")
            )
    raise RuntimeError(f"material not found: {serial}")


def deliver_pending(line: AssemblyLine, plan: dict[str, list[str]]) -> None:
    for station in line.stations:
        if station.awaiting_inspection and station.material is not None:
            serial = station.material.serial_number
            if plan.get(serial):
                visit = current_visit(line, serial, station.name)
                line.deliver_inspection(serial, station.name, visit, plan[serial].pop(0))


def run_with_plan(line: AssemblyLine, end_time: float, plan: dict[str, list[str]]) -> None:
    while line.clock.time < end_time:
        deliver_pending(line, plan)
        next_time = line.event_queue.next_time()
        if next_time is None or next_time > end_time:
            break
        line.run_until(next_time)
    line.run_until(end_time)


def build_line() -> AssemblyLine:
    return AssemblyLine(
        station_cycle_times=[2, 3, 4],
        buffer_capacities=[1, 2],
        material_types=["alpha", "beta"],
        release_limit=6,
        routes={
            "alpha": ["station-1", "station-2", "station-3"],
            "beta": ["station-1", "station-3"],
        },
        setup_times={"station-1": 1},
        inspection_stations=["station-3"],
        max_reworks=1,
    )


def main() -> None:
    plan = {
        "M-0001": ["rework", "pass"],
        "M-0002": ["pass"],
        "M-0003": ["scrap"],
        "M-0004": ["pass"],
        "M-0005": ["pass"],
        "M-0006": ["rework", "rework"],
    }

    line = build_line()
    while not any(station.awaiting_inspection for station in line.stations):
        line.run_until(line.event_queue.next_time())
    print("暂停时：有物料在 station-3 等待抽检，工位被真实占用")
    print(json.dumps(line.status(), ensure_ascii=False, indent=2))
    line.save(STATE_PATH)

    resumed = AssemblyLine.load(STATE_PATH)
    pending = next(s for s in resumed.stations if s.awaiting_inspection)
    serial = pending.material.serial_number
    visit = current_visit(resumed, serial, pending.name)
    resumed.deliver_inspection(serial, pending.name, visit, plan[serial].pop(0))
    accepted = resumed.deliver_inspection(serial, pending.name, visit, "pass")
    print(f"重复结果被忽略: {not accepted}")

    run_with_plan(resumed, 200, plan)
    print("结束：按类型账目")
    print(json.dumps(resumed.reconcile_by_type(), ensure_ascii=False, indent=2))
    print("报废原因:", resumed.scrap_reasons)
    print("账目校验:", "通过" if resumed.accounts_balanced() else "失败")
    for serial, material in sorted(resumed.archive.items()):
        visits = [
            f"{e.time:g}:{e.station}:{e.action}" for e in material.history
        ]
        print(f"  {serial} ({material.material_type}): " + " -> ".join(visits))


if __name__ == "__main__":
    main()
