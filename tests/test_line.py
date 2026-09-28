"""框架行为测试：守恒、堵塞反压、故障冻结/报废、暂停续跑等价性。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twin import AssemblyLine
from twin.material import WIP, FINISHED, SCRAPPED


def make_line(total=10, log=None):
    return AssemblyLine(
        name="测试线",
        station_specs=[
            {"id": "S1", "process_time": 3,
             "failures": [(27, 5, True, "S1 报废故障")]},
            {"id": "S2", "process_time": 8,
             "failures": [(14, 6, False, "S2 停机")]},
        ],
        source_interval=3,
        buffer_capacities=[1, 2],
        total_materials=total,
        log=log,
    )


class TestAssemblyLine(unittest.TestCase):

    def test_conservation_at_every_step(self):
        line = make_line()
        line.start()
        t = 0
        report = None
        while t < 130:
            t += 1
            line.run_until(float(t))
            report = line.conservation()
            self.assertTrue(report["balanced"], report)
        self.assertEqual((report["produced"], report["scrapped"], report["wip"]), (9, 1, 0))

    def test_blockage_backpressure(self):
        """下游堵塞时工位不消耗物料，堵塞传回源头；下游取料后恢复。"""
        line = AssemblyLine(
            name="纯堵塞线",
            station_specs=[{"id": "S1", "process_time": 1},
                           {"id": "S2", "process_time": 10}],
            source_interval=1,
            buffer_capacities=[2, 1],
            total_materials=8,
            log=None,
        )
        line.start()
        line.run_until(4.0)
        # 稳态反压：线内容纳 4 件（S2 在机1 + B1 一1 + B0 两2），源头停投
        self.assertEqual(line.loaded, 4)
        s1, s2 = line.stations
        self.assertEqual(s2.state, "busy")
        # S1 停下：不再从 B0 取料
        self.assertIn(s1.state, ("idle", "blocked"))
        self.assertEqual(line.buffers[0].level, 2)
        loaded_while_blocked = line.loaded
        line.run_until(9.9)
        self.assertEqual(line.loaded, loaded_while_blocked, "堵塞期间不得继续投料")
        # S2 完工后逐级疏通，恢复流动
        line.run_until(12.0)
        self.assertGreater(line.loaded, loaded_while_blocked)
        line.check_conservation()

    def test_failure_freezes_work_and_no_consumption_while_down(self):
        line = AssemblyLine(
            name="故障线",
            station_specs=[{"id": "S1", "process_time": 4,
                            "failures": [(5, 4, False, "停机不报废")]},
                           {"id": "S2", "process_time": 100}],
            source_interval=1,
            buffer_capacities=[2, 2],
            total_materials=5,
            log=None,
        )
        line.start()
        line.run_until(6.0)
        # S1 在 t=4 取料加工（工时4），t=5 故障时已加工1秒，剩余3秒
        self.assertEqual(line.stations[0].state, "down")
        mid = line.stations[0].material.material_id
        self.assertAlmostEqual(line.stations[0].remaining_time, 3.0)
        line.run_until(8.9)
        self.assertEqual(line.stations[0].state, "down")
        self.assertEqual(line.stations[0].material.material_id, mid)
        line.run_until(9.1)
        self.assertEqual(line.stations[0].state, "busy")
        line.check_conservation()

    def test_pause_resume_equivalence(self):
        snapshot = os.path.join(tempfile.gettempdir(), "twin_state_test.json")
        ref = make_line()
        ref.start()
        ref.run_until(130.0)

        a = make_line()
        a.start()
        a.run_until(31.0)
        a.check_conservation()
        mid_report = a.conservation()
        a.save_state(snapshot)

        b = AssemblyLine.from_state(snapshot, log=None)
        self.assertEqual(b.scheduler.now, 31.0)
        self.assertEqual(b.conservation(), mid_report)
        b.run_until(130.0)
        b.check_conservation()

        self.assertEqual((b.produced, b.scrapped, b.loaded),
                         (ref.produced, ref.scrapped, ref.loaded))
        self.assertEqual(b.wip_count(), 0)
        for mid, m in ref.materials.items():
            self.assertEqual(b.materials[mid].status, m.status, mid)

    def test_scrap_material_accounted(self):
        line = make_line()
        line.start()
        line.run_until(40.0)
        scrapped = [m for m in line.materials.values() if m.status == SCRAPPED]
        self.assertEqual(len(scrapped), 1)
        self.assertEqual(scrapped[0].scrap_reason, "S1 报废故障")
        r = line.conservation()
        self.assertEqual(r["loaded"], r["produced"] + r["scrapped"] + r["wip"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
