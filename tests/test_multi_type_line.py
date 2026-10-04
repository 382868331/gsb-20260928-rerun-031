from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from digital_twin import AssemblyLine


def build_multi_type_line(release_limit: int = 6, max_reworks: int = 1) -> AssemblyLine:
    return AssemblyLine(
        station_cycle_times=[2, 3, 4],
        buffer_capacities=[1, 2],
        material_types=["alpha", "beta"],
        release_limit=release_limit,
        routes={
            "alpha": ["station-1", "station-2", "station-3"],
            "beta": ["station-1", "station-3"],
        },
        setup_times={"station-1": 1},
        inspection_stations=["station-3"],
        max_reworks=max_reworks,
    )


def current_visit(line: AssemblyLine, serial: str, station_name: str) -> int:
    material = None
    for station in line.stations:
        if station.material is not None and station.material.serial_number == serial:
            material = station.material
            break
    if material is None:
        material = line.archive.get(serial)
    assert material is not None
    return sum(
        1
        for entry in material.history
        if entry.station == station_name
        and entry.action in ("process_start", "rework_start")
    )


def run_with_plan(line: AssemblyLine, end_time: float, plan: dict[str, list[str]]) -> None:
    """推进仿真；每当有物料进入待检，按计划送达抽检结果。"""
    while line.clock.time < end_time:
        deliver_pending(line, plan)
        next_time = line.event_queue.next_time()
        if next_time is None or next_time > end_time:
            break
        line.run_until(next_time)
    line.run_until(end_time)


def deliver_pending(line: AssemblyLine, plan: dict[str, list[str]]) -> None:
    for station in line.stations:
        if not station.awaiting_inspection or station.material is None:
            continue
        serial = station.material.serial_number
        results = plan.setdefault(serial, [])
        if not results:
            continue
        visit = current_visit(line, serial, station.name)
        line.deliver_inspection(serial, station.name, visit, results.pop(0))


class MultiTypeLineTest(unittest.TestCase):
    def test_routes_setup_and_conservation(self) -> None:
        line = build_multi_type_line()
        plan = {"M-%04d" % n: ["pass"] for n in range(1, 7)}
        run_with_plan(line, 120, plan)

        self.assertEqual(line.released, 6)
        self.assertTrue(line.accounts_balanced())
        by_type = line.reconcile_by_type()
        self.assertEqual(by_type["alpha"]["released"], 3)
        self.assertEqual(by_type["beta"]["released"], 3)
        self.assertTrue(all(entry["balanced"] for entry in by_type.values()))

        alpha = line.archive["M-0001"]
        beta = line.archive["M-0002"]
        self.assertEqual(alpha.material_type, "alpha")
        self.assertEqual(beta.material_type, "beta")
        alpha_actions = [(e.station, e.action) for e in alpha.history]
        self.assertIn(("station-1", "process_start"), alpha_actions)
        self.assertIn(("station-2", "process_start"), alpha_actions)
        self.assertIn(("station-3", "process_complete"), alpha_actions)
        beta_actions = [(e.station, e.action) for e in beta.history]
        self.assertIn(("station-2", "transit"), beta_actions)
        self.assertNotIn(("station-2", "process_start"), beta_actions)
        setup_entries = [e for e in alpha.history if e.action == "process_start" and e.detail.get("setup_time")]
        self.assertTrue(setup_entries)

    def test_pending_inspection_occupies_station(self) -> None:
        line = build_multi_type_line()
        line.run_until(60)
        pending = [s for s in line.stations if s.awaiting_inspection]
        self.assertTrue(pending, "expected a material waiting for inspection")
        station = pending[0]
        self.assertIsNotNone(station.material)
        completed_before = line.completed
        wip_before = line.work_in_progress
        line.run_until(120)
        self.assertEqual(line.completed, completed_before)
        self.assertEqual(line.work_in_progress, wip_before)
        self.assertIsNotNone(station.material)
        self.assertTrue(line.accounts_balanced())

    def test_duplicate_and_late_results_are_ignored(self) -> None:
        line = build_multi_type_line(release_limit=2)
        plan: dict[str, list[str]] = {}
        while line.clock.time < 120:
            next_time = line.event_queue.next_time()
            if next_time is None or next_time > 120:
                break
            line.run_until(next_time)
            for station in line.stations:
                if station.awaiting_inspection and station.material is not None:
                    serial = station.material.serial_number
                    visit = current_visit(line, serial, station.name)
                    if not plan.get(serial):
                        plan[serial] = ["used"]
                        material = station.material
                        self.assertTrue(
                            line.deliver_inspection(serial, station.name, visit, "pass")
                        )
                        history_len = len(material.history)
                        self.assertFalse(
                            line.deliver_inspection(serial, station.name, visit, "pass")
                        )
                        self.assertFalse(
                            line.deliver_inspection(serial, station.name, visit, "scrap")
                        )
                        self.assertEqual(len(material.history), history_len)
        line.run_until(120)
        self.assertEqual(len(line.ignored_results), 4)
        self.assertTrue(all(r["reason"] == "duplicate_or_late" for r in line.ignored_results))
        self.assertEqual(line.completed, 2)
        self.assertTrue(line.accounts_balanced())

    def test_rework_limit_scraps_with_reason_and_history(self) -> None:
        line = build_multi_type_line(release_limit=1, max_reworks=1)
        plan = {"M-0001": ["rework", "rework"]}
        run_with_plan(line, 120, plan)

        self.assertEqual(line.scrapped, 1)
        self.assertEqual(line.scrap_reasons["M-0001"], "rework_limit_exceeded")
        material = line.archive["M-0001"]
        self.assertEqual(material.rework_count, 1)
        actions = [entry.action for entry in material.history]
        self.assertIn("rework_start", actions)
        self.assertIn("process_complete", actions)
        self.assertEqual(actions[-1], "scrapped")
        self.assertEqual(line.released, 1)
        self.assertTrue(line.accounts_balanced())

    def test_correction_before_application_replaces_result(self) -> None:
        line = build_multi_type_line(release_limit=1)
        line.deliver_inspection("M-0001", "station-3", 1, "scrap")
        line.deliver_inspection("M-0001", "station-3", 1, "pass")
        run_with_plan(line, 120, {})
        self.assertEqual(line.completed, 1)
        self.assertEqual(line.scrapped, 0)
        self.assertTrue(line.accounts_balanced())

    def test_save_resume_pending_inspection_matches_uninterrupted(self) -> None:
        plan_template = {
            "M-0001": ["rework", "pass"],
            "M-0002": ["pass"],
            "M-0003": ["scrap"],
            "M-0004": ["pass"],
            "M-0005": ["pass"],
            "M-0006": ["rework", "pass"],
        }

        reference = build_multi_type_line()
        run_with_plan(reference, 200, {k: list(v) for k, v in plan_template.items()})

        interrupted = build_multi_type_line()
        plan = {k: list(v) for k, v in plan_template.items()}
        while not any(s.awaiting_inspection for s in interrupted.stations):
            next_time = interrupted.event_queue.next_time()
            self.assertIsNotNone(next_time)
            interrupted.run_until(next_time)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            interrupted.save(path)
            resumed = AssemblyLine.load(path)

        self.assertTrue(any(s.awaiting_inspection for s in resumed.stations))
        for station in resumed.stations:
            if station.awaiting_inspection and station.material is not None:
                serial = station.material.serial_number
                visit = current_visit(resumed, serial, station.name)
                result = plan[serial].pop(0)
                self.assertTrue(resumed.deliver_inspection(serial, station.name, visit, result))
                self.assertFalse(resumed.deliver_inspection(serial, station.name, visit, result))
        run_with_plan(resumed, 200, plan)

        self.assertEqual(resumed.status(), reference.status())
        self.assertEqual(resumed.ledger, reference.ledger)
        self.assertEqual(resumed.reconcile_by_type(), reference.reconcile_by_type())
        self.assertEqual(resumed.scrap_reasons, reference.scrap_reasons)
        for serial, material in reference.archive.items():
            self.assertEqual(
                [e.to_dict() for e in resumed.archive[serial].history],
                [e.to_dict() for e in material.history],
                serial,
            )


if __name__ == "__main__":
    unittest.main()
