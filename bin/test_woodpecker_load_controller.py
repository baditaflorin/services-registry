#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("woodpecker_load_controller.py")
SPEC = importlib.util.spec_from_file_location("woodpecker_load_controller", MODULE_PATH)
assert SPEC and SPEC.loader
controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)


METRICS = """
node_cpu_seconds_total{cpu="0",mode="idle"} 80
node_cpu_seconds_total{cpu="0",mode="user"} 10
node_cpu_seconds_total{cpu="0",mode="system"} 10
node_memory_MemAvailable_bytes 300
node_memory_MemTotal_bytes 1000
node_filesystem_avail_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 200
node_filesystem_size_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 1000
"""


class MetricsTests(unittest.TestCase):
    def test_snapshot_percentages(self):
        snapshot = controller.node_snapshot(METRICS)
        self.assertEqual(snapshot.cpu_idle_seconds, 80)
        self.assertEqual(snapshot.cpu_total_seconds, 100)
        self.assertEqual(snapshot.memory_available_pct, 30)
        self.assertEqual(snapshot.disk_available_pct, 20)

    def test_cpu_delta_and_pressure(self):
        previous = controller.NodeSnapshot(80, 100, 30, 20)
        current = controller.NodeSnapshot(81, 110, 14, 9)
        pressure = controller.evaluate_pressure(
            current,
            previous,
            {
                "cpu_stop_pct": 85,
                "cpu_resume_pct": 60,
                "memory_stop_pct": 15,
                "memory_resume_pct": 30,
                "disk_stop_pct": 10,
                "disk_resume_pct": 15,
            },
        )
        self.assertAlmostEqual(pressure.cpu_pct, 90)
        self.assertEqual(pressure.breaches, ("cpu", "memory", "disk"))
        self.assertFalse(pressure.recovered)


class HysteresisTests(unittest.TestCase):
    def setUp(self):
        self.overloaded = controller.Pressure(90, 10, 8, ("cpu", "memory", "disk"), False)
        self.recovered = controller.Pressure(20, 60, 50, (), True)

    def test_drains_only_after_threshold(self):
        state = {}
        self.assertIsNone(controller.update_hysteresis(state, self.overloaded, False, 2, 3))
        self.assertEqual(controller.update_hysteresis(state, self.overloaded, False, 2, 3), "drain")

    def test_never_restores_manual_drain(self):
        state = {"managed_no_schedule": False}
        for _ in range(4):
            action = controller.update_hysteresis(state, self.recovered, True, 2, 3)
        self.assertIsNone(action)

    def test_restores_controller_managed_drain(self):
        state = {"managed_no_schedule": True}
        self.assertIsNone(controller.update_hysteresis(state, self.recovered, True, 2, 2))
        self.assertEqual(controller.update_hysteresis(state, self.recovered, True, 2, 2), "restore")

    def test_unknown_cpu_does_not_restore(self):
        state = {"managed_no_schedule": True}
        unknown = controller.Pressure(None, 60, 50, (), False)
        for _ in range(5):
            action = controller.update_hysteresis(state, unknown, True, 2, 2)
        self.assertIsNone(action)

    def test_keeps_minimum_schedulable_capacity(self):
        self.assertTrue(controller.drain_allowed(3, 1))
        self.assertFalse(controller.drain_allowed(1, 1))
        self.assertFalse(controller.drain_allowed(2, 2))

    def test_drain_priority_prefers_more_severe_pressure(self):
        cpu_only = controller.Pressure(99, 35, 30, ("cpu",), False)
        cpu_and_disk = controller.Pressure(98, 75, 9, ("cpu", "disk"), False)
        self.assertGreater(
            controller.drain_priority(cpu_and_disk),
            controller.drain_priority(cpu_only),
        )


if __name__ == "__main__":
    unittest.main()
