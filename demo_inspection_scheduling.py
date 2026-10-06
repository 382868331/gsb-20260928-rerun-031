from __future__ import annotations

import json
from pathlib import Path

from digital_twin import AssemblyLine, MaterialType


STATE_PATH = Path(__file__).with_name("paused_inspection.json")


def build_line() -> AssemblyLine:
    return AssemblyLine(
        [2, 2],
        [1],
        material_types=[
            MaterialType(
                name="alpha",
                route=("station-1", "station-2"),
                cycle_times={"station-1": 2, "station-2": 2},
                inspect_after=("station-1", "station-2"),
                max_reworks=1,
                priority=2,
                due=30.0,
                inspection_time=2.0,
            ),
            MaterialType(
                name="beta",
                route=("station-1", "station-2"),
                cycle_times={"station-1": 2, "station-2": 2},
                inspect_after=("station-1",),
                max_reworks=1,
                priority=1,
                due=12.0,
                inspection_time=3.0,
            ),
        ],
        release_plan={"alpha": 1, "beta": 1},
        inspection_resources={"inspector-1": ["station-1", "station-2"]},
    )


def main() -> None:
    line = build_line()
    line.add_inspection_outage("inspector-1", 0, 5)

    line.pause_at(8)
    print("停用区间内：检测承诺排队，资源未占用")
    print(json.dumps(line.plan_inspections("priority"), ensure_ascii=False, indent=2))
    line.save(STATE_PATH)

    resumed = AssemblyLine.load(STATE_PATH)
    resumed.run_until(9)
    print("停用结束：按优先级开始检测，预测不改实际状态")
    print(json.dumps(resumed.compare_inspection_policies(), ensure_ascii=False, indent=2))

    resumed.deliver_inspection_result("M-0001", "station-1", "pass", visit=1)
    resumed.run_until(12)
    resumed.deliver_inspection_result("M-0001", "station-2", "fail", visit=1)
    print("实际失败：只退役受影响物料的剩余检测承诺")
    print("replan log:", json.dumps(resumed.replan_log, ensure_ascii=False))
    resumed.run_until(20)

    print("conservation:", json.dumps(resumed.inspection_conservation(), ensure_ascii=False))
    print("scraps:", json.dumps(resumed.scraps, ensure_ascii=False))
    print("accounts balanced:", resumed.accounts_balanced())


if __name__ == "__main__":
    main()
