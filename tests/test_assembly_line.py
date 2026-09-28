import tempfile
import unittest
from pathlib import Path

from digital_twin import AssemblyLine


class AssemblyLineTest(unittest.TestCase):
    def test_blocking_stops_upstream_consumption(self) -> None:
        line = AssemblyLine([3, 5], [1])
        line.run_until(16)

        self.assertEqual(line.stations[0].state.value, "idle")
        self.assertIsNone(line.stations[0].material)
        self.assertEqual(len(line.buffers[0].items), 1)
        self.assertEqual(line.released, 4)
        self.assertEqual(line.completed, 2)
        self.assertEqual(line.work_in_progress, 2)
        self.assertTrue(line.accounts_balanced())

    def test_fault_recovery_scrap_and_resume(self) -> None:
        line = AssemblyLine([3, 5], [1])
        line.schedule_fault(17, "station-2", 6, scrap_current=True)
        line.run_until(17)

        self.assertEqual(line.stations[1].state.value, "down")
        self.assertEqual(line.scrapped, 1)
        self.assertEqual(len(line.buffers[0].items), 1)
        self.assertTrue(line.accounts_balanced())

        line.run_until(35)
        self.assertNotEqual(line.stations[1].state.value, "down")
        self.assertEqual(line.released, line.completed + line.work_in_progress + line.scrapped)
        self.assertTrue(line.accounts_balanced())

    def test_saved_state_can_continue_without_losing_events_or_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "line.json"
            line = AssemblyLine([3, 5], [1])
            line.schedule_fault(17, "station-2", 6, scrap_current=True)
            line.pause_at(17)
            before = line.status()
            line.save(path)

            resumed = AssemblyLine.load(path)
            self.assertEqual(resumed.status(), before)
            resumed.run_until(35)

            self.assertEqual(resumed.clock.time, 35.0)
            self.assertEqual(resumed.scrapped, 1)
            self.assertEqual(
                resumed.released,
                resumed.completed + resumed.work_in_progress + resumed.scrapped,
            )
            self.assertTrue(resumed.accounts_balanced())


if __name__ == "__main__":
    unittest.main()
