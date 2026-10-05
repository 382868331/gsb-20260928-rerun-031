import tempfile
import unittest
from pathlib import Path

from digital_twin import AssemblyLine, InspectionJob, InspectionResource, MaterialType


def build_line(policy="priority", outages=None) -> AssemblyLine:
    return AssemblyLine(
        [2, 4, 3],
        [1, 1],
        material_types=[
            MaterialType(
                name="alpha",
                route=("station-1", "station-2", "station-3"),
                inspect_after=("station-2", "station-3"),
                max_reworks=1,
                priority=1,
                due_time=30,
            ),
            MaterialType(
                name="beta",
                route=("station-2", "station-3"),
                cycle_times={"station-2": 2, "station-3": 2},
                inspect_after=("station-3",),
                priority=5,
                due_time=20,
            ),
        ],
        release_plan={"alpha": 2, "beta": 1},
        inspection_capacity=1,
        inspection_duration=2,
        inspection_outages=outages or [],
        inspection_policy=policy,
    )


class InspectionResourceTest(unittest.TestCase):
    def test_priority_order_and_metrics(self) -> None:
        resource = InspectionResource("insp", capacity=1)
        resource.submit(InspectionJob("a|s|1", "a", "s", 1, 1, 10.0, 2.0, 0.0))
        resource.submit(InspectionJob("b|s|1", "b", "s", 1, 9, 20.0, 2.0, 1.0))
        resource.submit(InspectionJob("c|s|2", "c", "s", 2, 5, 30.0, 2.0, 2.0))
        self.assertEqual(resource.executable_order(), ["b|s|1", "c|s|2", "a|s|1"])

        job = resource.start_next(3.0)
        self.assertEqual(job.key, "b|s|1")
        self.assertIsNone(resource.start_next(3.0))
        resource.finish("b|s|1", 25.0, "pass")
        metrics = resource.metrics()
        self.assertEqual(metrics["total_wait"], 2.0)
        self.assertEqual(metrics["overdue_count"], 1)
        self.assertTrue(resource.conserved())

    def test_fifo_policy_orders_by_enqueue_time(self) -> None:
        resource = InspectionResource("insp", policy="fifo")
        resource.submit(InspectionJob("a|s|1", "a", "s", 1, 9, None, 1.0, 5.0))
        resource.submit(InspectionJob("b|s|1", "b", "s", 1, 1, None, 1.0, 2.0))
        self.assertEqual(resource.executable_order(), ["b|s|1", "a|s|1"])

    def test_outage_blocks_start_and_unfit_job_is_skipped(self) -> None:
        resource = InspectionResource("insp")
        resource.add_outage(10.0, 20.0)
        resource.submit(InspectionJob("a|s|1", "a", "s", 1, 5, None, 6.0, 0.0))
        resource.submit(InspectionJob("b|s|1", "b", "s", 1, 1, None, 1.0, 0.0))
        job = resource.start_next(5.0)
        self.assertEqual(job.key, "b|s|1")
        resource.finish("b|s|1", 6.0, "pass")
        self.assertIsNone(resource.start_next(6.0))
        self.assertIsNone(resource.start_next(12.0))
        job = resource.start_next(20.0)
        self.assertEqual(job.key, "a|s|1")
        self.assertTrue(resource.conserved())

    def test_cancel_queued_releases_started_defers_until_explicit_end(self) -> None:
        resource = InspectionResource("insp")
        resource.submit(InspectionJob("a|s|1", "a", "s", 1, 1, None, 2.0, 0.0))
        resource.submit(InspectionJob("b|s|1", "b", "s", 1, 1, None, 2.0, 0.0))
        resource.start_next(1.0)
        self.assertEqual(resource.cancel("b|s|1", 2.0), "released")
        self.assertEqual(resource.cancel("a|s|1", 2.0), "deferred")
        self.assertIsNone(resource.start_next(2.0))
        resource.finish("a|s|1", 4.0, "pass")
        job = resource.jobs["a|s|1"]
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(job.finish_time, 4.0)
        self.assertTrue(resource.conserved())

    def test_prediction_does_not_change_state(self) -> None:
        resource = InspectionResource("insp")
        resource.submit(InspectionJob("a|s|1", "a", "s", 1, 1, None, 2.0, 0.0))
        self.assertTrue(resource.predict("a|s|1", "pass"))
        self.assertEqual(resource.jobs["a|s|1"].state, "queued")
        self.assertEqual(resource.jobs["a|s|1"].predicted_result, "pass")


class InspectionSchedulingLineTest(unittest.TestCase):
    def test_shared_resource_queue_metrics_and_conservation(self) -> None:
        line = build_line(outages=[(12, 16)])
        line.run_until(8)
        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        line.run_until(12)
        self.assertEqual(line.pending_inspections["M-0001"], "M-0001|station-3|1")
        self.assertEqual(line.pending_inspections["M-0002"], "M-0002|station-2|1")
        self.assertEqual(
            line.inspection_order(),
            ["M-0001|station-3|1", "M-0002|station-2|1"],
        )
        resource = line.inspection_resource
        self.assertEqual(resource.jobs["M-0001|station-2|1"].state, "completed")
        self.assertEqual(resource.jobs["M-0001|station-3|1"].state, "queued")

        line.run_until(16)
        self.assertEqual(resource.jobs["M-0001|station-3|1"].state, "in_progress")
        self.assertEqual(resource.jobs["M-0001|station-3|1"].start_time, 16)

        line.run_until(18)
        line.deliver_inspection_result("M-0001", "station-3", "pass", visit=1)
        line.run_until(20)
        line.deliver_inspection_result("M-0002", "station-2", "pass", visit=1)
        line.run_until(25)
        line.deliver_inspection_result("M-0002", "station-3", "pass", visit=1)
        line.run_until(29)
        line.deliver_inspection_result("M-0003", "station-3", "pass", visit=1)
        line.run_until(40)

        self.assertEqual(line.completed, 3)
        self.assertEqual(line.scrapped, 0)
        metrics = line.inspection_metrics()
        self.assertEqual(metrics["completed"], 5)
        self.assertEqual(metrics["total_wait"], 11.0)
        self.assertEqual(metrics["overdue_count"], 1)
        self.assertTrue(resource.conserved())
        self.assertTrue(line.accounts_balanced())
        self.assertTrue(
            all(entry["balanced"] for entry in line.reconcile_by_type().values())
        )

    def test_predicted_pass_is_not_actual_release(self) -> None:
        line = build_line()
        line.run_until(6)
        self.assertTrue(
            line.predict_inspection_result("M-0001", "station-2", "pass", visit=1)
        )
        line.run_until(10)
        station2 = line.stations[1]
        self.assertIsNotNone(station2.material)
        self.assertEqual(station2.material.state.value, "pending_inspection")
        self.assertEqual(line.completed, 0)
        self.assertEqual(len(line.dispositions), 0)

    def test_result_before_resource_start_is_held(self) -> None:
        line = build_line(outages=[(0, 10)])
        line.run_until(8)
        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        self.assertEqual(line.completed, 0)
        self.assertEqual(
            line.indeterminate_results[-1]["reason"],
            "inspection not started on resource",
        )
        line.run_until(10)
        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        line.run_until(12)
        self.assertEqual(line.stations[2].material.serial_number, "M-0001")

    def test_old_round_feedback_does_not_release_current_occupancy(self) -> None:
        line = build_line()
        line.run_until(6)
        line.deliver_inspection_result("M-0001", "station-2", "rework", visit=1)
        line.run_until(10)
        self.assertEqual(line.pending_inspections["M-0001"], "M-0001|station-2|2")
        resource = line.inspection_resource
        self.assertEqual(resource.jobs["M-0001|station-2|2"].state, "in_progress")

        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        station2 = line.stations[1]
        self.assertIsNotNone(station2.material)
        self.assertEqual(station2.material.state.value, "pending_inspection")
        self.assertEqual(resource.jobs["M-0001|station-2|2"].state, "in_progress")
        self.assertEqual(line.completed, 0)

        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=2)
        self.assertEqual(resource.jobs["M-0001|station-2|2"].state, "completed")
        self.assertTrue(line.accounts_balanced())

    def test_scrap_cancels_queued_commitment_started_waits_explicit_end(self) -> None:
        line = build_line()
        line.run_until(8)
        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        line.run_until(12)
        resource = line.inspection_resource
        self.assertEqual(resource.jobs["M-0001|station-3|1"].state, "in_progress")
        self.assertEqual(resource.jobs["M-0002|station-2|1"].state, "queued")

        self.assertEqual(
            line.cancel_inspection("M-0002", "station-2", 1), "released",
        )
        self.assertEqual(
            line.cancel_inspection("M-0001", "station-3", 1), "deferred",
        )
        self.assertIsNone(resource.start_next(line.clock.time))

        line.deliver_inspection_result("M-0001", "station-3", "fail", visit=1)
        self.assertEqual(line.scrapped, 1)
        self.assertEqual(line.scraps[0]["reason"], "inspection_failed")
        self.assertEqual(resource.jobs["M-0001|station-3|1"].state, "cancelled")
        self.assertEqual(resource.jobs["M-0001|station-3|1"].finish_time, 12.0)
        self.assertTrue(resource.conserved())
        self.assertTrue(line.accounts_balanced())

        history = line.material_history("M-0001")
        actions = [entry["action"] for entry in history]
        self.assertIn("processed", actions)
        self.assertEqual(actions[-1], "scrap")

    def test_fault_scrap_cancels_only_affected_remaining_route(self) -> None:
        line = build_line()
        line.run_until(8)
        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        line.run_until(12)
        resource = line.inspection_resource
        line.schedule_fault(13, "station-2", 3, scrap_current=True)
        line.run_until(13)
        self.assertEqual(line.scrapped, 1)
        self.assertEqual(line.scraps[0]["serial_number"], "M-0002")
        self.assertEqual(resource.jobs["M-0002|station-2|1"].state, "cancelled")
        self.assertEqual(resource.jobs["M-0001|station-3|1"].state, "in_progress")
        self.assertTrue(resource.conserved())
        self.assertTrue(line.accounts_balanced())

        line.deliver_inspection_result("M-0001", "station-3", "pass", visit=1)
        line.run_until(21)
        line.deliver_inspection_result("M-0003", "station-3", "pass", visit=1)
        line.run_until(40)
        self.assertEqual(line.completed, 2)
        self.assertTrue(line.accounts_balanced())

    def test_save_resume_preserves_resource_state(self) -> None:
        line = build_line(outages=[(12, 16)])
        line.run_until(8)
        line.deliver_inspection_result("M-0001", "station-2", "pass", visit=1)
        line.pause_at(12)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "line.json"
            line.save(path)
            resumed = AssemblyLine.load(path)
        self.assertEqual(
            resumed.inspection_resource.to_dict(),
            line.inspection_resource.to_dict(),
        )
        self.assertEqual(resumed.inspection_order(), line.inspection_order())
        resumed.run_until(16)
        resource = resumed.inspection_resource
        self.assertEqual(resource.jobs["M-0001|station-3|1"].state, "in_progress")


if __name__ == "__main__":
    unittest.main()
