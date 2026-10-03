import tempfile
import unittest
from pathlib import Path

from digital_twin.production import (
    InspectionPolicy,
    MaterialType,
    ProductionLine,
)


def build_two_type_line(**policy_kwargs):
    line = ProductionLine(
        {"S1": 3, "S2": 4, "S3": 2},
        feed_capacity=1,
        buffer_capacity=1,
        rework_capacity=1,
        seed=7,
    )
    line.register_type(
        MaterialType("A", ["S1", "S2", "S3"], max_rework=2,
                     setup_times={"S1": 1, "S2": 1})
    )
    line.register_type(
        MaterialType("B", ["S1", "S3"], max_rework=0,
                     setup_times={"S1": 2})
    )
    kwargs = {
        "sampling_rate": 1.0,
        "pass_rate": 1.0,
        "duration": 1.0,
        "rework_to": "S1",
        "fail_decision": "rework",
        "on_limit": "scrap",
    }
    kwargs.update(policy_kwargs)
    line.set_inspection(InspectionPolicy("S2", **kwargs))
    return line


class MultiTypeTests(unittest.TestCase):
    def test_multi_type_setup_blocking_and_accounts(self) -> None:
        line = build_two_type_line()
        line.release("A", 3)
        line.release("B", 2)
        line.run_until(100)
        counts = line.reconcile()
        self.assertEqual(counts["A"]["released"], 3)
        self.assertEqual(counts["B"]["released"], 2)
        self.assertEqual(counts["total"]["completed"], 5)
        self.assertTrue(line.accounts_balanced())
        # Type switch at S1 paid setup time for both types.
        self.assertGreater(line.stations["S1"].setup_time_used, 0)

    def test_buffer_full_blocks_upstream(self) -> None:
        line = ProductionLine({"S1": 1, "S2": 10}, feed_capacity=1, buffer_capacity=1)
        line.register_type(MaterialType("A", ["S1", "S2"]))
        line.release("A", 5)
        line.run_until(4)
        # S2 is slow: the inter-station buffer stays full, so the line cannot
        # admit more units than the bounded WIP allows; the rest stay upstream.
        self.assertEqual(len(line.step_buffers[("S1", "S2")].items), 1)
        self.assertLessEqual(line.work_in_progress, 3)
        self.assertEqual(line.released + line._release_backlog.get("A", 0), 5)
        line.run_until(40)
        self.assertEqual(line.released, 5)
        self.assertTrue(line.accounts_balanced())
        self.assertTrue(line.accounts_balanced())

    def test_pending_inspection_keeps_position_and_blocks(self) -> None:
        line = build_two_type_line(duration=0.0)
        line.release("A", 3)
        line.run_until(20)
        pending = line.pending_inspections()
        self.assertTrue(pending)
        item = pending[0]
        # The material still occupies a concrete inspection position.
        located = line.location_of(item["serial_number"])
        self.assertEqual(located["location"], item["location"])
        self.assertEqual(located["state"], "waiting")
        before_wip = line.work_in_progress
        self.assertEqual(line.location_of(item["serial_number"])["state"], "waiting")
        # Decision can be recorded later; repeated call does not double-act.
        line.record_inspection(item["serial_number"], True)
        line.record_inspection(item["serial_number"], True)
        self.assertEqual(line.work_in_progress, before_wip)
        # Manual inspection is required for every sampled item (duration == 0).
        deadline = 0
        while line.completed < 3 and deadline < 100:
            line.run_until(20 + deadline)
            for pending in line.pending_inspections():
                line.record_inspection(pending["serial_number"], True)
            deadline += 1
        self.assertTrue(line.accounts_balanced())
        self.assertEqual(line.completed, 3)

    def test_rework_reuses_identity_and_history(self) -> None:
        line = build_two_type_line(pass_rate=0.0, on_limit="scrap")
        serials = line.release("A", 1)
        serial = serials[0]
        line.run_until(200)
        material = line.materials[serial]
        self.assertEqual(material.rework_count, 2)
        self.assertEqual(material.state.value, "scrapped")
        events = [entry["event"] for entry in material.history]
        self.assertEqual(events.count("rework_start"), 2)
        # Rework never created a new release.
        self.assertEqual(line.released, 1)
        self.assertEqual(line.scrapped, 1)
        self.assertTrue(line.accounts_balanced())

    def test_rework_limit_explicit_hold_disposition(self) -> None:
        line = build_two_type_line(pass_rate=0.0, on_limit="hold")
        serial = line.release("A", 1)[0]
        line.run_until(200)
        material = line.materials[serial]
        self.assertEqual(material.rework_count, 2)
        self.assertTrue(line.pending_inspections())
        # Cannot ask for another rework past the cap.
        with self.assertRaises(ValueError):
            line.dispose(serial, "rework")
        line.dispose(serial, "concession")
        self.assertEqual(line.completed, 1)
        self.assertEqual(line.concessions, 1)
        self.assertTrue(line.accounts_balanced())

    def test_pending_inspection_blocks_upstream_wip(self) -> None:
        line = ProductionLine({"S1": 2, "S2": 5}, feed_capacity=1, buffer_capacity=1)
        line.register_type(MaterialType("A", ["S1", "S2"]))
        line.set_inspection(InspectionPolicy("S2", duration=0.0))
        line.release("A", 5)
        line.run_until(10)
        # The undecided item physically occupies S2's inspection position.
        self.assertEqual(line.stations["S2"].state, "blocked")
        self.assertEqual(len(line.pending_inspections()), 1)
        self.assertEqual(line.work_in_progress, 3)
        self.assertEqual(line._release_backlog["A"], 2)
        self.assertTrue(line.accounts_balanced())

    def test_failed_inspection_can_be_scrapped(self) -> None:
        line = build_two_type_line(duration=0.0, fail_decision="scrap",
                                   on_limit="scrap")
        serial = line.release("A", 1)[0]
        line.run_until(20)
        line.record_inspection(serial, False)
        line.run_until(40)
        self.assertEqual(line.scrapped, 1)
        self.assertEqual(line.materials[serial].state.value, "scrapped")
        self.assertTrue(line.accounts_balanced())

    def test_save_resume_during_setup_inspection_rework(self) -> None:
        line = build_two_type_line(duration=0.0)
        line.release("A", 4)
        line.run_until(12)  # likely inside setup/processing
        with tempfile.TemporaryDirectory() as directory:
            setup_path = Path(directory) / "setup.json"
            line.save(setup_path)
            resumed = ProductionLine.load(setup_path)
            self.assertEqual(resumed.status(), line.status())
            resumed.run_until(22)
            def drain_manual(candidate, horizon=200):
                step = 0
                while candidate.completed < 4 and step < 40:
                    candidate.run_until(22 + step * 5)
                    for pending in candidate.pending_inspections():
                        candidate.record_inspection(pending["serial_number"], True)
                    step += 1
            drain_manual(resumed)
            hold_path = Path(directory) / "hold.json"
            resumed.save(hold_path)
            again = ProductionLine.load(hold_path)
            released_before = again.released
            drain_manual(again)
            self.assertEqual(again.released, released_before)
            self.assertEqual(again.released, released_before)
            self.assertEqual(again.completed, 4)
            self.assertTrue(again.accounts_balanced())

    def test_repeated_release_after_load_is_idempotent_for_scheduled(self) -> None:
        line = build_two_type_line()
        line.release("A", 1, at=10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.json"
            line.save(path)
            resumed = ProductionLine.load(path)
            resumed.run_until(100)
            self.assertEqual(resumed.released, 1)
            self.assertEqual(resumed.completed, 1)

    def test_compare_scenarios_shows_blocking_and_output(self) -> None:
        def build(overrides):
            return build_two_type_line(pass_rate=overrides["pass_rate"],
                                       sampling_rate=overrides["sampling_rate"])
        scenarios = {
            "no_inspection": {"sampling_rate": 0.0, "pass_rate": 1.0},
            "full_strict": {"sampling_rate": 1.0, "pass_rate": 0.5},
        }
        def seeded_build(overrides):
            candidate = build(overrides)
            candidate.release("A", 10)
            return candidate
        report = ProductionLine.compare_scenarios(seeded_build, scenarios, 120)
        self.assertIn("no_inspection", report)
        self.assertGreaterEqual(report["no_inspection"]["completed"],
                                report["full_strict"]["completed"])


if __name__ == "__main__":
    unittest.main()
