from __future__ import annotations

import json
from pathlib import Path

from digital_twin import AssemblyLine


STATE_PATH = Path(__file__).with_name("paused_line.json")


def print_status(title: str, line: AssemblyLine) -> None:
    print(title)
    print(json.dumps(line.status(), ensure_ascii=False, indent=2))


def main() -> None:
    line = AssemblyLine(station_cycle_times=[3, 5], buffer_capacities=[1])
    line.schedule_fault(start_time=17, station_name="station-2", duration=6, scrap_current=True)

    line.pause_at(17)
    print_status("暂停时：慢工位使快工位停等，缓冲位已满", line)
    line.save(STATE_PATH)

    resumed = AssemblyLine.load(STATE_PATH)
    resumed.run_until(24)
    print_status("恢复后：station-2 已修复并从缓冲位重新拉料", resumed)

    resumed.run_until(35)
    print_status("结束：产出、在制、报废与已投物料一致", resumed)
    print("账目校验:", "通过" if resumed.accounts_balanced() else "失败")


if __name__ == "__main__":
    main()
