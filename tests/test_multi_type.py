import tempfile
import unittest
from pathlib import Path

from digital_twin import AssemblyLine, MaterialType


def build_line() -> AssemblyLine:
    line = AssemblyLine(
        [3, 5],
        [1],
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
    line.schedule_inspection_result(11, "M-0001", "station-2", "pass", visit=1)
    line.schedule_inspection_result(15, "M-0002", "station-2", "rework", visit=1)
    line.schedule_inspection_result(16, "M-0002", "station-2", "pass", visit=1)
    line.schedule_inspection_result(20, "M-0002", "station-2", "rework", visit=2)


class MultiTypeLineTest(unittest.TestCase):
    def test_pending_inspection_occupies_station(self) -> None:
        line = build_line()
        schedule_results(line)
        line.pause_at(9)

        station2 = line.stations[1]
        self.assertIsNotNone(station2.material)
        self.assertEqual(station2.material.serial_number, "M-0001")
        self.assertEqual(station2.material.state.value, "pending_inspection")
        self.assertEqual(line.ledger["M-0001"], "pending_inspection")
        self.assertEqual(line.pending_inspections["M-0001"], "M-0001|station-2|1")
        self.assertEqual(len(line.buffers[0].items), 1)
        self.assertEqual(line.buffers[0].items[0].serial_number, "M-0002")
        self.assertTrue(line.stations[0].is_idle)
        self.assertEqual(line.work_in_progress, 2)
        self.assertTrue(line.accounts_balanced())

    def test_full_flow_rework_limit_and_scrap_reason(self) -> None:
        line = build_line()
        schedule_results(line)
        line.run_until(30)

        self.assertEqual(line.released, 3)
        self.assertEqual(line.completed, 2)
        self.assertEqual(line.scrapped, 1)
        self.assertEqual(line.work_in_progress, 0)
        self.assertTrue(line.accounts_balanced())

        scrap = line.scraps[0]
        self.assertEqual(scrap["serial_number"], "M-0002")
        self.assertEqual(scrap["reason"], "rework_limit_exceeded")
        self.assertEqual(scrap["rework_count"], 1)

        history = line.material_history("M-0002")
        actions = [entry["action"] for entry in history]
        self.assertIn("rework_start", actions)
        self.assertEqual(actions.count("processed"), 3)
        self.assertEqual(actions[-1], "scrap")
        self.assertEqual(line.scrap_materials[0].serial_number, "M-0002")

        by_type = line.reconcile_by_type()
        self.assertEqual(by_type["alpha"]["completed"], 1)
        self.assertEqual(by_type["alpha"]["scrapped"], 1)
        self.assertEqual(by_type["beta"]["completed"], 1)
        self.assertTrue(all(entry["balanced"] for entry in by_type.values()))

    def test_duplicate_and_late_results_are_ignored(self) -> None:
        line = build_line()
        schedule_results(line)
        line.run_until(30)

        self.assertEqual(len(line.inspection_log), 2)
        ignored = {(e["serial_number"], e["result"]) for e in line.inspection_log}
        self.assertEqual(ignored, {("M-0001", "pass"), ("M-0002", "pass")})
        for entry in line.inspection_log:
            self.assertEqual(entry["action"], "ignored")
            self.assertIsNotNone(entry["original_key"])
        self.assertEqual(len(line.dispositions), 3)
        self.assertEqual(line.completed, 2)
        self.assertEqual(line.scrapped, 1)

    def test_save_resume_with_pending_inspection_matches_uninterrupted(self) -> None:
        reference = build_line()
        schedule_results(reference)
        reference.run_until(30)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "line.json"
            paused = build_line()
            schedule_results(paused)
            paused.pause_at(9)
            self.assertEqual(paused.pending_inspections, {"M-0001": "M-0001|station-2|1"})
            paused.save(path)

            resumed = AssemblyLine.load(path)
            resumed.run_until(10.5)
            resumed.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
            resumed.run_until(30)

        self.assertEqual(resumed.status(), reference.status())
        self.assertEqual(resumed.ledger, reference.ledger)
        self.assertEqual(resumed.scraps, reference.scraps)
        self.assertEqual(resumed.reconcile_by_type(), reference.reconcile_by_type())
        for serial in ("M-0001", "M-0002", "M-0003"):
            self.assertEqual(resumed.material_history(serial), reference.material_history(serial))

    def test_original_entry_still_releases_standard_material(self) -> None:
        line = AssemblyLine([3, 5], [1])
        line.run_until(16)
        self.assertEqual(line.released, 4)
        self.assertTrue(line.accounts_balanced())
        self.assertEqual(set(line.reconcile_by_type()), {"standard"})


if __name__ == "__main__":
    unittest.main()
