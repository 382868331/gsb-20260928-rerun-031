"""第一版演示：两个速度不同的工位 + 缓冲位，展示堵塞反压与故障恢复。

线体:
    source -> [B0 容量1] -> S1(快, 工时3) -> [B1 容量2] -> S2(慢, 工时8) -> sink
    投料节拍 3

看点:
  1. S2 慢 -> B1 很快占满 -> S1 加工完放不进 B1 进入 BLOCKED -> B0 占满 ->
     source 停止投料（堵塞逐级传回上游）；S2 取走一件后全线逐级恢复。
  2. t=14 S2 故障停机 6（不报废），修好后继续剩余工时。
  3. t=30 S1 故障停机 5 并报废当前在机物料，恢复后补料继续。
  4. t=22 暂停落盘，t=22 后从快照接续；产出/在制/报废全程守恒。
所有时间均由模拟时钟推进，不使用任何真实睡眠。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twin import AssemblyLine

STATE_FILE = os.path.join(os.path.dirname(__file__), "line_state.json")

STATION_SPECS = [
    {
        "id": "S1",
        "process_time": 3,
        "failures": [(27, 5, True, "S1 卡料，当前件报废")],
    },
    {
        "id": "S2",
        "process_time": 8,
        "failures": [(14, 6, False, "S2 电机过热")],
    },
]


def build_line(log=print):
    return AssemblyLine(
        name="两工位演示线",
        station_specs=STATION_SPECS,
        source_interval=3,
        buffer_capacities=[1, 2],
        total_materials=12,
        log=log,
    )


def main():
    print("=" * 72)
    print("阶段一：从 t=0 跑到暂停点 t=22（展示堵塞反压 + S2 故障/修复）")
    print("=" * 72)
    line = build_line()
    line.start()
    line.run_until(22.0)
    line.check_conservation()
    r1 = line.conservation()
    print("-" * 72)
    print(f"暂停点统计: 投入={r1['loaded']} 产出={r1['produced']} "
          f"报废={r1['scrapped']} 在制={r1['wip']} 守恒={r1['balanced']}")
    print(f"在制分布: {r1['wip_locations']}")
    line.save_state(STATE_FILE)

    print()
    print("=" * 72)
    print("阶段二：从快照接续，跑到 t=70（展示 S1 故障报废 + 恢复 + 全部完工）")
    print("=" * 72)
    line2 = AssemblyLine.from_state(STATE_FILE, log=print)
    line2.run_until(130.0)
    line2.check_conservation()
    r2 = line2.conservation()
    print("-" * 72)
    print(f"终点统计: 投入={r2['loaded']} 产出={r2['produced']} "
          f"报废={r2['scrapped']} 在制={r2['wip']} 守恒={r2['balanced']}")
    print(f"在制分布: {r2['wip_locations']}")
    assert r2["balanced"]
    assert r2["produced"] + r2["scrapped"] == r2["loaded"], "最终应全部离开在制"
    print()
    print("守恒校验通过：投入 = 产出 + 报废 + 在制（终点在制为 0）。")


if __name__ == "__main__":
    main()
