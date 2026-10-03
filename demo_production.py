"""Multi-type line demo: setup changeovers, bounded buffers, rework and sampling."""
from __future__ import annotations

import json
from pathlib import Path

from digital_twin import InspectionPolicy, MaterialType, ProductionLine


STATE_PATH = Path(__file__).with_name("production_paused.json")


def build_line() -> ProductionLine:
    line = ProductionLine(
        {"S1": 3, "S2": 4, "S3": 2},
        feed_capacity=2,
        buffer_capacity=1,
        rework_capacity=1,
        seed=42,
    )
    line.register_type(
        MaterialType("A", ["S1", "S2", "S3"], max_rework=2,
                     setup_times={"S1": 2, "S2": 1})
    )
    line.register_type(
        MaterialType("B", ["S1", "S3"], max_rework=1, setup_times={"S1": 2})
    )
    line.set_inspection(InspectionPolicy(
        station="S2",
        sampling_rate=1.0,
        pass_rate=0.6,
        duration=1.0,
        rework_to="S1",
        fail_decision="rework",
        on_limit="scrap",
    ))
    line.release("A", 6)
    line.release("B", 3)
    return line


def main() -> None:
    line = build_line()
    line.run_until(20)
    print("暂停于准备/加工/待检/返工混合状态：")
    print(json.dumps(line.status(), ensure_ascii=False, indent=2))
    line.save(STATE_PATH)

    resumed = ProductionLine.load(STATE_PATH)
    resumed.run_until(120)
    print("恢复并跑完：")
    print(json.dumps(resumed.metrics(), ensure_ascii=False, indent=2))
    print("分类型账目：")
    print(json.dumps(resumed.reconcile(), ensure_ascii=False, indent=2))
    print("账目校验:", "通过" if resumed.accounts_balanced() else "失败")


def compare() -> None:
    def build(overrides):
        candidate = ProductionLine(
            {"S1": 3, "S2": 4, "S3": 2},
            feed_capacity=2, buffer_capacity=1, rework_capacity=1, seed=42,
        )
        candidate.register_type(MaterialType(
            "A", ["S1", "S2", "S3"], max_rework=2, setup_times={"S1": 2}))
        candidate.set_inspection(InspectionPolicy(
            station="S2", duration=1.0, rework_to="S1",
            fail_decision="rework", on_limit="scrap", **overrides))
        candidate.release("A", 10)
        return candidate

    report = ProductionLine.compare_scenarios(build, {
        "no_sampling": {"sampling_rate": 0.0, "pass_rate": 1.0},
        "half_sampling": {"sampling_rate": 0.5, "pass_rate": 0.8},
        "full_strict": {"sampling_rate": 1.0, "pass_rate": 0.5},
    }, end_time=120)
    print("抽检方案对比：")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
    compare()
