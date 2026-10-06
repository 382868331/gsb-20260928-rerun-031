import tempfile
import unittest
from pathlib import Path

from digital_twin import AssemblyLine, MaterialType


def build_shared_inspector_line() -> AssemblyLine:
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


class InspectionSchedulingTest(unittest.TestCase):
    def test_shared_resource_queues_and_starts_by_priority(self) -> None:
        line = build_shared_inspector_line()
        line.run_until(4)
        jobs = line.inspection_jobs
        self.assertEqual(len(jobs), 1)
        first = next(iter(jobs.values()))
        self.assertEqual(first.state, "active")
        self.assertEqual(first.resource, "inspector-1")

        line.run_until(6)
        states = {job.key: job.state for job in line.inspection_jobs.values()}
        self.assertEqual(len(states), 1)

        line.deliver_inspection_result("M-0001", "station-1", "pass", visit=1)
        line.run_until(8)
        queued = [
            job for job in line.inspection_jobs.values() if job.state == "queued"
        ]
        active = [
            job for job in line.inspection_jobs.values() if job.state == "active"
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(queued), 1)
        self.assertEqual(active[0].serial_number, "M-0001")
        self.assertEqual(queued[0].serial_number, "M-0002")
        conservation = line.inspection_conservation()
        self.assertTrue(conservation["queue_conserved"])
        self.assertTrue(conservation["resource_conserved"])
        self.assertTrue(conservation["material_conserved"])

    def test_plan_is_prediction_only_and_compares_policies(self) -> None:
        line = build_shared_inspector_line()
        line.run_until(8)
        before_pending = dict(line.pending_inspections)
        before_ledger = dict(line.ledger)
        comparison = line.compare_inspection_policies()
        self.assertEqual(set(comparison), {"fifo", "priority"})
        for plan in comparison.values():
            self.assertIn("total_wait", plan)
            self.assertIn("overdue_count", plan)
            self.assertTrue(plan["order"])
        self.assertEqual(line.pending_inspections, before_pending)
        self.assertEqual(line.ledger, before_ledger)
        self.assertEqual(line.completed, 0)

    def test_late_old_round_result_does_not_release_current_occupancy(self) -> None:
        line = build_shared_inspector_line()
        line.run_until(4)
        line.deliver_inspection_result("M-0001", "station-1", "rework", visit=1)
        line.run_until(8)
        self.assertEqual(line.pending_inspections["M-0001"], "M-0001|station-1|2")
        active_key = next(
            job.key for job in line.inspection_jobs.values() if job.state == "active"
        )
        self.assertEqual(active_key, "M-0001|station-1|2")
        line.deliver_inspection_result("M-0001", "station-1", "pass", visit=1)
        self.assertEqual(line.pending_inspections["M-0001"], "M-0001|station-1|2")
        self.assertEqual(
            next(
                job.key for job in line.inspection_jobs.values()
                if job.state == "active"
            ),
            "M-0001|station-1|2",
        )
        self.assertEqual(line.completed, 0)

    def test_cancel_releases_commitment_only_before_start(self) -> None:
        line = build_shared_inspector_line()
        line.run_until(4)
        self.assertFalse(line.cancel_inspection("M-0001", "station-1", 1))
        active = next(
            job for job in line.inspection_jobs.values() if job.state == "active"
        )
        self.assertEqual(active.key, "M-0001|station-1|1")
        self.assertEqual(line.pending_inspections["M-0001"], "M-0001|station-1|1")

        line.deliver_inspection_result("M-0001", "station-1", "pass", visit=1)
        line.run_until(8)
        queued = next(
            (job for job in line.inspection_jobs.values() if job.state == "queued"),
            None,
        )
        self.assertIsNotNone(queued)
        serial, station, visit = queued.serial_number, queued.station, queued.visit
        self.assertTrue(line.cancel_inspection(serial, station, visit))
        self.assertNotIn(serial, line.pending_inspections)
        self.assertEqual(queued.state, "cancelled")
        self.assertTrue(line.inspection_conservation()["queue_conserved"])
        self.assertTrue(line.accounts_balanced())

    def test_outage_blocks_start_until_window_ends(self) -> None:
        line = build_shared_inspector_line()
        line.add_inspection_outage("inspector-1", 0, 10)
        line.run_until(8)
        self.assertTrue(
            all(job.state == "queued" for job in line.inspection_jobs.values())
        )
        line.deliver_inspection_result("M-0001", "station-1", "pass", visit=1)
        self.assertEqual(len(line.indeterminate_results), 1)
        self.assertEqual(
            line.indeterminate_results[0]["reason"],
            "inspection has not started; resource commitment not active",
        )
        line.run_until(10)
        active = [
            job for job in line.inspection_jobs.values() if job.state == "active"
        ]
        self.assertEqual(len(active), 1)
        self.assertGreaterEqual(active[0].start_time, 10)
        conservation = line.inspection_conservation()
        self.assertTrue(conservation["outage_respected"])
        self.assertTrue(conservation["resource_conserved"])

    def test_failure_retires_only_affected_remaining_route(self) -> None:
        line = build_shared_inspector_line()
        line.run_until(4)
        line.deliver_inspection_result("M-0001", "station-1", "pass", visit=1)
        line.run_until(8)
        line.deliver_inspection_result("M-0001", "station-2", "fail", visit=1)
        self.assertEqual(line.scrapped, 1)
        self.assertEqual(line.scraps[0]["reason"], "inspection_failed")
        self.assertEqual(len(line.replan_log), 0)
        m1_jobs = [
            job for job in line.inspection_jobs.values()
            if job.serial_number == "M-0001"
        ]
        self.assertTrue(all(job.state == "finished" for job in m1_jobs))
        history = line.material_history("M-0001")
        actions = [entry["action"] for entry in history]
        self.assertIn("processed", actions)
        self.assertEqual(actions[-1], "scrap")
        self.assertTrue(line.accounts_balanced())

    def test_save_resume_preserves_jobs_outages_and_conservation(self) -> None:
        line = build_shared_inspector_line()
        line.add_inspection_outage("inspector-1", 0, 10)
        line.run_until(8)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inspection.json"
            line.save(path)
            resumed = AssemblyLine.load(path)
        self.assertEqual(resumed.inspection_outages, line.inspection_outages)
        self.assertEqual(
            {k: j.to_dict() for k, j in resumed.inspection_jobs.items()},
            {k: j.to_dict() for k, j in line.inspection_jobs.items()},
        )
        resumed.run_until(10)
        active = [
            job for job in resumed.inspection_jobs.values() if job.state == "active"
        ]
        self.assertEqual(len(active), 1)
        conservation = resumed.inspection_conservation()
        self.assertTrue(all(
            conservation[key]
            for key in (
                "queue_conserved", "resource_conserved",
                "outage_respected", "material_conserved",
            )
        ))


if __name__ == "__main__":
    unittest.main()
