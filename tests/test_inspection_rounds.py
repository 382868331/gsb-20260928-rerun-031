import tempfile
import unittest
from pathlib import Path

from digital_twin import AssemblyLine, MaterialType


def build_line(release_plan=None) -> AssemblyLine:
    return AssemblyLine(
        [2, 4],
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
        release_plan=release_plan if release_plan is not None else {"alpha": 1},
    )


class InspectionRoundTest(unittest.TestCase):
    def test_late_and_corrected_old_round_results_do_not_release(self) -> None:
        line = build_line()
        line.schedule_inspection_result(9, "M-0001", "station-2", "rework", visit=1)
        line.schedule_inspection_result(13.5, "M-0001", "station-2", "pass", visit=1)
        line.schedule_inspection_result(14.5, "M-0001", "station-2", "rework", visit=1)
        line.schedule_inspection_result(15.5, "M-0001", "station-2", "pass", visit=2)
        line.schedule_inspection_result(16.5, "M-0001", "station-2", "pass", visit=2)

        line.run_until(14)
        station2 = line.stations[1]
        self.assertIsNotNone(station2.material)
        self.assertEqual(station2.material.serial_number, "M-0001")
        self.assertEqual(station2.material.state.value, "pending_inspection")
        self.assertEqual(line.pending_inspections, {"M-0001": "M-0001|station-2|2"})
        self.assertEqual(line.completed, 0)
        old_round = [
            entry for entry in line.inspection_log
            if entry["action"] == "ignored" and entry["visit"] == 1
        ]
        self.assertEqual(len(old_round), 1)
        self.assertEqual(old_round[0]["reason"], "round already finalized")
        self.assertEqual(old_round[0]["original_key"], "M-0001|station-2|1")
        self.assertEqual(len(line.indeterminate_results), 0)

        line.run_until(15)
        ignored_correction = [
            entry for entry in line.inspection_log
            if entry["action"] == "ignored" and entry["visit"] == 1
        ]
        self.assertEqual(len(ignored_correction), 2)
        self.assertEqual(ignored_correction[0]["reason"], "round already finalized")
        self.assertEqual(line.completed, 0)
        self.assertIsNotNone(station2.material)

        line.run_until(16)
        self.assertEqual(line.completed, 1)
        self.assertIsNone(station2.material)

        line.run_until(17)
        self.assertEqual(line.completed, 1)
        duplicate = [
            entry for entry in line.inspection_log
            if entry["action"] == "ignored" and entry["visit"] == 2
        ]
        self.assertEqual(len(duplicate), 1)
        self.assertEqual(line.scrapped, 0)
        self.assertTrue(line.accounts_balanced())

    def test_second_route_type_uses_own_cycle_time_after_inspection_clears(self) -> None:
        line = build_line()
        line.schedule_inspection_result(9, "M-0001", "station-2", "pass", visit=1)
        line.run_until(10)
        line.release("beta", 1)
        line.run_until(20)

        self.assertEqual(line.completed, 2)
        beta_history = line.material_history("M-0002")
        station2_start = next(
            entry for entry in beta_history
            if entry["action"] == "start" and entry["station"] == "station-2"
        )
        self.assertEqual(station2_start["detail"]["cycle_time"], 3.0)
        by_type = line.reconcile_by_type()
        self.assertTrue(all(entry["balanced"] for entry in by_type.values()))

    def test_old_round_pass_cannot_bypass_rework_limit_scrap(self) -> None:
        line = build_line()
        line.schedule_inspection_result(9, "M-0001", "station-2", "rework", visit=1)
        line.schedule_inspection_result(13.5, "M-0001", "station-2", "pass", visit=1)
        line.schedule_inspection_result(14.5, "M-0001", "station-2", "rework", visit=2)
        line.run_until(20)

        self.assertEqual(line.completed, 0)
        self.assertEqual(line.scrapped, 1)
        scrap = line.scraps[0]
        self.assertEqual(scrap["serial_number"], "M-0001")
        self.assertEqual(scrap["reason"], "rework_limit_exceeded")
        self.assertEqual(scrap["rework_count"], 1)
        self.assertEqual(len(line.indeterminate_results), 0)
        old_round = [
            entry for entry in line.inspection_log
            if entry["action"] == "ignored" and entry["visit"] == 1
        ]
        self.assertEqual(len(old_round), 1)
        self.assertTrue(line.accounts_balanced())

    def test_result_without_round_is_held_not_applied_to_current_round(self) -> None:
        line = build_line()
        line.run_until(9)
        line.deliver_inspection_result("M-0001", "station-2", "pass")
        station2 = line.stations[1]
        self.assertIsNotNone(station2.material)
        self.assertEqual(station2.material.state.value, "pending_inspection")
        self.assertEqual(line.completed, 0)
        self.assertEqual(len(line.indeterminate_results), 1)

        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        line.run_until(20)
        self.assertEqual(line.completed, 1)
        self.assertTrue(line.accounts_balanced())

    def test_save_resume_during_round_wait_matches_uninterrupted(self) -> None:
        def scheduled() -> AssemblyLine:
            line = build_line()
            line.schedule_inspection_result(9, "M-0001", "station-2", "rework", visit=1)
            line.schedule_inspection_result(13.5, "M-0001", "station-2", "pass", visit=1)
            line.schedule_inspection_result(14.5, "M-0001", "station-2", "rework", visit=1)
            line.schedule_inspection_result(15.5, "M-0001", "station-2", "pass", visit=2)
            line.schedule_inspection_result(16.5, "M-0001", "station-2", "pass", visit=2)
            return line

        reference = scheduled()
        reference.run_until(20)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rounds.json"
            paused = scheduled()
            paused.pause_at(14)
            self.assertEqual(paused.pending_inspections, {"M-0001": "M-0001|station-2|2"})
            self.assertEqual(
                [e["action"] for e in paused.inspection_log], ["ignored"],
            )
            paused.save(path)

            resumed = AssemblyLine.load(path)
            self.assertEqual(resumed.pending_inspections, paused.pending_inspections)
            resumed.run_until(20)

        self.assertEqual(resumed.status(), reference.status())
        self.assertEqual(resumed.ledger, reference.ledger)
        self.assertEqual(resumed.scraps, reference.scraps)
        self.assertEqual(resumed.inspection_log, reference.inspection_log)
        self.assertEqual(resumed.indeterminate_results, reference.indeterminate_results)
        self.assertEqual(resumed.reconcile_by_type(), reference.reconcile_by_type())
        self.assertEqual(resumed.material_history("M-0001"), reference.material_history("M-0001"))

    def test_result_for_wrong_station_or_unknown_round_is_held(self) -> None:
        line = build_line()
        line.run_until(9)
        line.deliver_inspection_result("M-0001", "station-1", "pass", visit=1)
        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=9)
        station2 = line.stations[1]
        self.assertIsNotNone(station2.material)
        self.assertEqual(station2.material.state.value, "pending_inspection")
        self.assertEqual(line.completed, 0)
        self.assertEqual(len(line.indeterminate_results), 2)
        reasons = {entry["reason"] for entry in line.indeterminate_results}
        self.assertIn("claimed round is not the current pending inspection", reasons)

        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        line.run_until(20)
        self.assertEqual(line.completed, 1)
        self.assertTrue(line.accounts_balanced())


if __name__ == "__main__":
    unittest.main()
