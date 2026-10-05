from __future__ import annotations

import json
from pathlib import Path

from digital_twin import AssemblyLine, MaterialType


STATE_PATH = Path(__file__).with_name("paused_multi_type.json")


def build_line() -> AssemblyLine:
    line = AssemblyLine(
        station_cycle_times=[3, 5],
        buffer_capacities=[1],
        material_types=[
            MaterialType(
                name="alpha",
                route=("station-1", "station-2"),
                cycle_times={"station-1": 2, "station-2": 4},
                setup_time=1,
                inspect_after=("station-2",),
                max_reworks=1,
            ),
            MaterialType(
                name="beta",
                route=("station-2",),
                cycle_times={"station-2": 3},
            ),
        ],
        release_plan={"alpha": 2, "beta": 1},
    )
    return line


def schedule_results(line: AssemblyLine) -> None:
    line.schedule_inspection_result(10, "M-0001", "station-2", "pass", visit=1)
    line.schedule_inspection_result(11, "M-0001", "station-2", "pass", visit=1)  # duplicate, ignored
    line.schedule_inspection_result(15, "M-0002", "station-2", "rework", visit=1)
    line.schedule_inspection_result(16, "M-0002", "station-2", "pass", visit=1)  # late correction, ignored
    line.schedule_inspection_result(20, "M-0002", "station-2", "rework", visit=2)  # over limit, scrap


def print_status(title: str, line: AssemblyLine) -> None:
    print(title)
    print(json.dumps(line.status(), ensure_ascii=False, indent=2))


def main() -> None:
    line = build_line()
    schedule_results(line)

    line.pause_at(9)
    print_status("paused: M-0001 pending inspection occupies station-2", line)
    line.save(STATE_PATH)

    resumed = AssemblyLine.load(STATE_PATH)
    resumed.run_until(10.5)
    resumed.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)  # duplicate, ignored
    resumed.run_until(30)
    print_status("finished: rework limit scrapped M-0002, others completed", resumed)

    print("by type:", json.dumps(resumed.reconcile_by_type(), ensure_ascii=False))
    print("scraps:", json.dumps(resumed.scraps, ensure_ascii=False))
    print("inspection log:", json.dumps(resumed.inspection_log, ensure_ascii=False))
    print("indeterminate:", json.dumps(resumed.indeterminate_results, ensure_ascii=False))
    print("M-0002 history:", json.dumps(resumed.material_history("M-0002"), ensure_ascii=False))
    print("accounts balanced:", resumed.accounts_balanced())


if __name__ == "__main__":
    main()
