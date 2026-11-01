#!/usr/bin/env python3

import importlib.util
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


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

PRESSURE_METRICS = """
node_cpu_seconds_total{cpu="0",mode="idle"} 81
node_cpu_seconds_total{cpu="0",mode="user"} 19
node_cpu_seconds_total{cpu="0",mode="system"} 10
node_memory_MemAvailable_bytes 100
node_memory_MemTotal_bytes 1000
node_filesystem_avail_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 80
node_filesystem_size_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 1000
"""

RECOVERED_METRICS = """
node_cpu_seconds_total{cpu="0",mode="idle"} 109
node_cpu_seconds_total{cpu="0",mode="user"} 20
node_cpu_seconds_total{cpu="0",mode="system"} 11
node_memory_MemAvailable_bytes 600
node_memory_MemTotal_bytes 1000
node_filesystem_avail_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 500
node_filesystem_size_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 1000
"""

RECOVERED_METRICS_NEXT = """
node_cpu_seconds_total{cpu="0",mode="idle"} 139
node_cpu_seconds_total{cpu="0",mode="user"} 21
node_cpu_seconds_total{cpu="0",mode="system"} 12
node_memory_MemAvailable_bytes 600
node_memory_MemTotal_bytes 1000
node_filesystem_avail_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 500
node_filesystem_size_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 1000
"""

RECOVERED_METRICS_NEXT2 = """
node_cpu_seconds_total{cpu="0",mode="idle"} 169
node_cpu_seconds_total{cpu="0",mode="user"} 22
node_cpu_seconds_total{cpu="0",mode="system"} 13
node_memory_MemAvailable_bytes 600
node_memory_MemTotal_bytes 1000
node_filesystem_avail_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 500
node_filesystem_size_bytes{device="/dev/vda1",fstype="ext4",mountpoint="/"} 1000
"""

BACKING_LOW_BY_FLOOR = """
node_filesystem_avail_bytes{device="/dev/md2",fstype="ext4",mountpoint="/"} 10
node_filesystem_size_bytes{device="/dev/md2",fstype="ext4",mountpoint="/"} 1000
node_filesystem_avail_bytes{device="/dev/md3",fstype="ext4",mountpoint="/"} 200
node_filesystem_size_bytes{device="/dev/md3",fstype="ext4",mountpoint="/"} 1000
node_filesystem_avail_bytes{device="/dev/md3",fstype="ext4",mountpoint="/var"} 1
node_filesystem_size_bytes{device="/dev/md3",fstype="ext4",mountpoint="/var"} 1000
"""

BACKING_HEALTHY = """
node_filesystem_avail_bytes{device="/dev/md2",fstype="ext4",mountpoint="/"} 1
node_filesystem_size_bytes{device="/dev/md2",fstype="ext4",mountpoint="/"} 1000
node_filesystem_avail_bytes{device="/dev/md3",fstype="ext4",mountpoint="/"} 500
node_filesystem_size_bytes{device="/dev/md3",fstype="ext4",mountpoint="/"} 1000
"""

BACKING_MISMATCHED = """
node_filesystem_avail_bytes{device="/dev/md2",fstype="ext4",mountpoint="/"} 5
node_filesystem_size_bytes{device="/dev/md2",fstype="ext4",mountpoint="/"} 1000
node_filesystem_avail_bytes{device="/dev/md3",fstype="ext4",mountpoint="/var"} 500
node_filesystem_size_bytes{device="/dev/md3",fstype="ext4",mountpoint="/var"} 1000
"""


def backing_storage_config():
    return {
        "metrics_url": "http://pve-host.example.internal:9100/metrics",
        "filesystem_mountpoint": "/",
        "filesystem_device": "/dev/md3",
        "disk_stop_pct": 10,
        "disk_resume_pct": 15,
        "minimum_free_bytes": 300,
    }


THRESHOLDS = {
    "cpu_stop_pct": 85,
    "cpu_resume_pct": 60,
    "memory_stop_pct": 15,
    "memory_resume_pct": 30,
    "disk_stop_pct": 10,
    "disk_resume_pct": 15,
}


def config_v2():
    return {
        "version": 2,
        "woodpecker_url": "https://ci.example.com",
        "interval_seconds": 30,
        "minimum_schedulable_nodes": 1,
        "stop_samples": 1,
        "resume_samples": 1,
        "thresholds": copy.deepcopy(THRESHOLDS),
        "nodes": [
            {
                "name": "builder-a",
                "agent_names": ["builder-agent"],
                "metrics_url": "http://builder-a/metrics",
                "filesystem_mountpoint": "/",
            },
            {
                "name": "builder-b",
                "agent_names": ["remote-agent"],
                "metrics_url": "http://builder-b/metrics",
                "filesystem_mountpoint": "/",
            },
        ],
    }


def config_v1():
    return {
        "version": 1,
        "woodpecker_url": "https://ci.example.com",
        "interval_seconds": 30,
        "minimum_schedulable_agents": 1,
        "stop_samples": 10,
        "resume_samples": 2,
        "thresholds": copy.deepcopy(THRESHOLDS),
        "agents": [
            {
                "name": "builder-agent",
                "metrics_url": "http://builder-a/metrics",
                "filesystem_mountpoint": "/",
                "backing_storage": backing_storage_config(),
            },
            {
                "name": "remote-agent",
                "metrics_url": "http://builder-b/metrics",
                "filesystem_mountpoint": "/",
            },
        ],
    }


class FakeClient:
    def __init__(self, agents):
        self.remote_agents = copy.deepcopy(agents)
        self.changed = []

    def agents(self):
        return self.remote_agents

    def queue(self):
        return {"pending": [], "running": [], "paused": False}

    def set_no_schedule(self, agent, value):
        self.changed.append((agent["id"], value))
        next(item for item in self.remote_agents if item["id"] == agent["id"])["no_schedule"] = value


class HTTPClientTests(unittest.TestCase):
    def test_agents_collects_all_entries_across_api_capped_pages(self):
        client = controller.HTTPClient("https://ci.example.com", "unused")
        first_page = [{"id": agent_id} for agent_id in range(1, 51)]
        second_page = [{"id": agent_id} for agent_id in range(51, 53)]

        with patch.object(
            client,
            "_request",
            side_effect=[first_page, second_page],
        ) as request:
            agents = client.agents()

        self.assertEqual([agent["id"] for agent in agents], list(range(1, 53)))
        self.assertEqual(
            [call.args[0] for call in request.call_args_list],
            ["/api/agents?page=1&perPage=50", "/api/agents?page=2&perPage=50"],
        )

    def test_agents_rejects_non_array_page(self):
        client = controller.HTTPClient("https://ci.example.com", "unused")
        with patch.object(client, "_request", return_value={"agents": []}):
            with self.assertRaisesRegex(ValueError, "JSON array"):
                client.agents()


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


class HostAdmissionTests(unittest.TestCase):
    def test_config_v2_requires_unique_node_and_agent_names(self):
        config = config_v2()
        controller.validate_config(config)
        config["nodes"][1]["agent_names"] = ["builder-agent"]
        with self.assertRaisesRegex(ValueError, "exactly one node"):
            controller.validate_config(config)

    def test_drains_every_live_duplicate_agent_for_a_pressured_host(self):
        config = config_v2()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 2, "name": "builder-agent", "no_schedule": False},
                {"id": 3, "name": "remote-agent", "no_schedule": False},
            ]
        )
        state = {
            "nodes": {
                "builder-a": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "builder-b": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }
        with patch.object(
            controller,
            "fetch_text",
            side_effect=lambda url: PRESSURE_METRICS if "builder-a" in url else METRICS,
        ):
            changed = controller.run_cycle_v2(config, state, client, apply=True)
        self.assertTrue(changed)
        self.assertEqual(client.changed, [(1, True), (2, True)])
        self.assertEqual(state["nodes"]["builder-a"]["managed_agent_ids"], ["1", "2"])

    def test_drain_preserves_operator_paused_agent(self):
        config = config_v2()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 2, "name": "builder-agent", "no_schedule": True},
                {"id": 3, "name": "remote-agent", "no_schedule": False},
            ]
        )
        state = {
            "nodes": {
                "builder-a": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "builder-b": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }
        with patch.object(
            controller,
            "fetch_text",
            side_effect=lambda url: PRESSURE_METRICS if "builder-a" in url else METRICS,
        ):
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1, True)])
        self.assertTrue(next(agent for agent in client.remote_agents if agent["id"] == 2)["no_schedule"])

    def test_restore_only_reenables_ids_managed_by_controller(self):
        config = config_v2()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": True},
                {"id": 2, "name": "builder-agent", "no_schedule": True},
                {"id": 3, "name": "remote-agent", "no_schedule": False},
            ]
        )
        state = {
            "nodes": {
                "builder-a": {
                    "previous_snapshot": controller.NodeSnapshot(80, 100, 10, 8).__dict__,
                    "managed_agent_ids": ["1"],
                    "managed_no_schedule": True,
                },
                "builder-b": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }
        with patch.object(controller, "fetch_text", return_value=RECOVERED_METRICS):
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1, False)])
        self.assertFalse(next(agent for agent in client.remote_agents if agent["id"] == 1)["no_schedule"])
        self.assertTrue(next(agent for agent in client.remote_agents if agent["id"] == 2)["no_schedule"])

    def test_controller_keeps_one_schedulable_host(self):
        config = config_v2()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 3, "name": "remote-agent", "no_schedule": True},
            ]
        )
        state = {
            "nodes": {
                "builder-a": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "builder-b": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }
        with patch.object(
            controller,
            "fetch_text",
            side_effect=lambda url: PRESSURE_METRICS if "builder-a" in url else METRICS,
        ):
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [])



class BackingStorageTests(unittest.TestCase):
    def test_filesystem_metrics_require_exact_mount_and_device(self):
        snapshot = controller.filesystem_snapshot(BACKING_LOW_BY_FLOOR, "/", "/dev/md3")
        self.assertEqual(snapshot.available_bytes, 200)
        self.assertEqual(snapshot.size_bytes, 1000)
        self.assertEqual(snapshot.available_pct, 20)
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            controller.filesystem_snapshot(BACKING_MISMATCHED, "/", "/dev/md3")

    def test_backing_byte_floor_drains_only_v2_node_even_at_minimum_capacity(self):
        config = config_v2()
        config["stop_samples"] = 10
        config["nodes"][0]["backing_storage"] = backing_storage_config()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 2, "name": "builder-agent", "no_schedule": False},
                {"id": 3, "name": "remote-agent", "no_schedule": True},
            ]
        )
        state = {
            "nodes": {
                "builder-a": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "builder-b": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }

        def fetch(url):
            if "pve-host" in url:
                return BACKING_LOW_BY_FLOOR
            return METRICS

        with patch.object(controller, "fetch_text", side_effect=fetch):
            changed = controller.run_cycle_v2(config, state, client, apply=True)

        self.assertTrue(changed)
        self.assertEqual(client.changed, [(1, True), (2, True)])
        self.assertTrue(next(agent for agent in client.remote_agents if agent["id"] == 3)["no_schedule"])
        self.assertEqual(state["nodes"]["builder-a"]["managed_agent_ids"], ["1", "2"])

    def test_backing_threshold_is_immediate_and_handles_duplicate_v1_registrations(self):
        config = config_v1()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 2, "name": "builder-agent", "no_schedule": False},
                {"id": 4, "name": "builder-agent", "no_schedule": False},
                {"id": 5, "name": "builder-agent", "no_schedule": False},
                {"id": 6, "name": "builder-agent", "no_schedule": False},
                {"id": 3, "name": "remote-agent", "no_schedule": True},
            ]
        )
        state = {
            "agents": {
                "builder-agent": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "remote-agent": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }

        def fetch(url):
            if "pve-host" in url:
                return BACKING_LOW_BY_FLOOR
            return METRICS

        with patch.object(controller, "fetch_text", side_effect=fetch):
            changed = controller.run_cycle_v1(config, state, client, apply=True)

        self.assertTrue(changed)
        self.assertEqual(client.changed, [(1, True), (2, True), (4, True), (5, True), (6, True)])
        self.assertEqual(state["agents"]["builder-agent"]["managed_agent_ids"], ["1", "2", "4", "5", "6"])
        self.assertTrue(next(agent for agent in client.remote_agents if agent["id"] == 3)["no_schedule"])

    def test_missing_or_unparseable_v2_metrics_quarantine_only_gated_node(self):
        for failed_url in ("http://pve-host.example.internal:9100/metrics", "http://builder-a/metrics"):
            with self.subTest(failed_url=failed_url):
                config = config_v2()
                config["nodes"][0]["backing_storage"] = backing_storage_config()
                client = FakeClient(
                    [
                        {"id": 1, "name": "builder-agent", "no_schedule": False},
                        {"id": 2, "name": "remote-agent", "no_schedule": True},
                    ]
                )
                state = {
                    "nodes": {
                        "builder-a": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                        "builder-b": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                    }
                }

                def fetch(url):
                    if url == failed_url:
                        if "pve-host" in url:
                            return BACKING_MISMATCHED
                        return "node_memory_MemTotal_bytes 1000"
                    return METRICS

                with patch.object(controller, "fetch_text", side_effect=fetch):
                    changed = controller.run_cycle_v2(config, state, client, apply=True)

                self.assertTrue(changed)
                self.assertEqual(client.changed, [(1, True)])
                self.assertTrue(next(agent for agent in client.remote_agents if agent["id"] == 2)["no_schedule"])
                self.assertEqual(
                    state["nodes"]["builder-a"]["managed_agent_ids"],
                    ["1"],
                )
                self.assertIsNone(state["nodes"]["builder-a"]["previous_snapshot"])

    def test_v1_telemetry_failure_bypasses_minimum_and_preserves_manual_hold(self):
        config = config_v1()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 2, "name": "builder-agent", "no_schedule": True},
                {"id": 3, "name": "remote-agent", "no_schedule": True},
            ]
        )
        state = {
            "agents": {
                "builder-agent": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "remote-agent": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }

        def fetch(url):
            if "pve-host" in url:
                return BACKING_MISMATCHED
            return METRICS

        with patch.object(controller, "fetch_text", side_effect=fetch):
            changed = controller.run_cycle_v1(config, state, client, apply=True)

        self.assertTrue(changed)
        self.assertEqual(client.changed, [(1, True)])
        self.assertTrue(next(agent for agent in client.remote_agents if agent["id"] == 2)["no_schedule"])
        self.assertEqual(state["agents"]["builder-agent"]["managed_agent_ids"], ["1"])

    def test_restore_waits_for_guest_and_backing_recovery_and_keeps_manual_hold(self):
        config = config_v1()
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": True},
                {"id": 2, "name": "builder-agent", "no_schedule": True},
                {"id": 3, "name": "remote-agent", "no_schedule": False},
            ]
        )
        state = {
            "agents": {
                "builder-agent": {
                    "previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__,
                    "managed_agent_ids": ["1"],
                    "managed_no_schedule": True,
                },
                "remote-agent": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }

        samples = iter(
            [
                (RECOVERED_METRICS, BACKING_LOW_BY_FLOOR),
                (RECOVERED_METRICS_NEXT, BACKING_HEALTHY),
                (RECOVERED_METRICS_NEXT2, BACKING_HEALTHY),
            ]
        )

        def fetch(url):
            guest, backing = current_samples[0]
            if "pve-host" in url:
                return backing
            return guest

        for expected_changes in ([], [], [(1, False)]):
            current_samples = [next(samples)]
            with patch.object(controller, "fetch_text", side_effect=fetch):
                controller.run_cycle_v1(config, state, client, apply=True)
            self.assertEqual(client.changed, expected_changes)
            client.changed.clear()

        self.assertFalse(next(agent for agent in client.remote_agents if agent["id"] == 1)["no_schedule"])
        self.assertTrue(next(agent for agent in client.remote_agents if agent["id"] == 2)["no_schedule"])


IO_THRESHOLDS = dict(THRESHOLDS, io_pressure_stop_pct=40, io_pressure_resume_pct=15)


def io_metrics(waiting_seconds, idle=80):
    """METRICS plus the pressure counter node-exporter exposes."""
    return METRICS.replace('mode="idle"} 80', f'mode="idle"}} {idle}') + (
        f"node_pressure_io_waiting_seconds_total {waiting_seconds}\n"
        "node_pressure_io_stalled_seconds_total 5\n"
    )


class IOPressureTests(unittest.TestCase):
    def snapshots(self, waited, wall):
        previous = controller.node_snapshot(io_metrics(100), taken_at=1000.0)
        # The CPU idle counter moves too, so CPU is known and low: only I/O can hold a recovery back.
        current = controller.node_snapshot(io_metrics(100 + waited, idle=110), taken_at=1000.0 + wall)
        return current, previous

    def test_snapshot_reads_the_waiting_counter_and_not_the_stalled_one(self):
        snapshot = controller.node_snapshot(io_metrics(204975.5), taken_at=12.0)
        self.assertEqual(snapshot.io_waiting_seconds, 204975.5)
        self.assertEqual(snapshot.taken_at, 12.0)
        self.assertIsNone(controller.node_snapshot(METRICS, taken_at=12.0).io_waiting_seconds)

    def test_io_pressure_is_the_waiting_share_of_the_wall_time(self):
        current, previous = self.snapshots(waited=15, wall=30)
        pressure = controller.evaluate_pressure(current, previous, IO_THRESHOLDS)
        self.assertAlmostEqual(pressure.io_pressure_pct, 50.0)
        self.assertIn("io", pressure.breaches)
        self.assertFalse(pressure.recovered)

    def test_below_the_stop_threshold_is_not_a_breach(self):
        current, previous = self.snapshots(waited=9, wall=30)  # 30%
        pressure = controller.evaluate_pressure(current, previous, IO_THRESHOLDS)
        self.assertNotIn("io", pressure.breaches)
        self.assertFalse(pressure.recovered, "30% is above the resume threshold of 15%")

    def test_recovers_once_at_or_below_the_resume_threshold(self):
        current, previous = self.snapshots(waited=3, wall=30)  # 10%
        pressure = controller.evaluate_pressure(current, previous, IO_THRESHOLDS)
        self.assertEqual(pressure.breaches, ())
        self.assertTrue(pressure.recovered)
        current, previous = self.snapshots(waited=4.5, wall=30)  # exactly the 15% resume threshold
        self.assertTrue(controller.evaluate_pressure(current, previous, IO_THRESHOLDS).recovered)

    def test_without_io_thresholds_behaviour_is_unchanged(self):
        current, previous = self.snapshots(waited=30, wall=30)  # 100% stalled
        pressure = controller.evaluate_pressure(current, previous, THRESHOLDS)
        self.assertNotIn("io", pressure.breaches)
        self.assertEqual(pressure.breaches, ())

    def test_unknown_io_pressure_never_drains_and_never_blocks_a_restore(self):
        previous = controller.node_snapshot(METRICS, taken_at=1000.0)
        current = controller.node_snapshot(
            METRICS.replace('mode="idle"} 80', 'mode="idle"} 110'), taken_at=1030.0
        )
        pressure = controller.evaluate_pressure(current, previous, IO_THRESHOLDS)
        self.assertIsNone(pressure.io_pressure_pct)
        self.assertNotIn("io", pressure.breaches)
        self.assertTrue(pressure.recovered)

    def test_counter_reset_and_too_short_an_interval_are_ignored(self):
        previous = controller.node_snapshot(io_metrics(500), taken_at=1000.0)
        reset = controller.node_snapshot(io_metrics(3), taken_at=1030.0)
        self.assertIsNone(controller.evaluate_pressure(reset, previous, IO_THRESHOLDS).io_pressure_pct)
        quick = controller.node_snapshot(io_metrics(501), taken_at=1000.4)
        self.assertIsNone(controller.evaluate_pressure(quick, previous, IO_THRESHOLDS).io_pressure_pct)

    def test_io_pressure_is_capped_at_100(self):
        current, previous = self.snapshots(waited=45, wall=30)  # a multi-task PSI sum can exceed wall time
        self.assertEqual(controller.evaluate_pressure(current, previous, IO_THRESHOLDS).io_pressure_pct, 100.0)

    def test_a_nodes_own_io_limits_override_only_that_node(self):
        entry = {"name": "slow-disks", "io_pressure_stop_pct": 30, "io_pressure_resume_pct": 10}
        merged = controller.thresholds_for_node(IO_THRESHOLDS, entry)
        self.assertEqual((merged["io_pressure_stop_pct"], merged["io_pressure_resume_pct"]), (30, 10))
        self.assertEqual(IO_THRESHOLDS["io_pressure_stop_pct"], 40, "the global thresholds are not modified")
        self.assertEqual(controller.thresholds_for_node(THRESHOLDS, {"name": "plain"}), THRESHOLDS)

    def test_state_keeps_the_legacy_snapshot_shape_and_round_trips_the_io_counter(self):
        state = {}
        controller.snapshot_to_state(state, controller.node_snapshot(io_metrics(700), taken_at=1234.5))
        self.assertEqual(set(state["previous_snapshot"]), set(controller.LEGACY_SNAPSHOT_FIELDS))
        self.assertEqual(state["previous_io"], {"waiting_seconds": 700, "taken_at": 1234.5})
        restored = controller.snapshot_from_state(state)
        self.assertEqual((restored.io_waiting_seconds, restored.taken_at), (700, 1234.5))
        controller.snapshot_to_state(state, controller.node_snapshot(METRICS, taken_at=1300.0))
        self.assertNotIn("previous_io", state)
        self.assertIsNone(controller.snapshot_from_state(state).io_waiting_seconds)

    def test_state_written_before_this_version_still_loads(self):
        legacy = controller.NodeSnapshot(80, 100, 30, 20).__dict__
        old = {"previous_snapshot": {k: legacy[k] for k in controller.LEGACY_SNAPSHOT_FIELDS}}
        snapshot = controller.snapshot_from_state(old)
        self.assertEqual(snapshot.cpu_total_seconds, 100)
        self.assertIsNone(snapshot.io_waiting_seconds)

    def test_config_validation_of_io_limits(self):
        config = config_v2()
        config["thresholds"].update(io_pressure_stop_pct=40, io_pressure_resume_pct=15)
        controller.validate_config(config)
        for bad in (
            {"io_pressure_stop_pct": 40},
            {"io_pressure_stop_pct": 15, "io_pressure_resume_pct": 15},
            {"io_pressure_stop_pct": 10, "io_pressure_resume_pct": 20},
            {"io_pressure_stop_pct": 101, "io_pressure_resume_pct": 20},
            {"io_pressure_stop_pct": 40, "io_pressure_resume_pct": 0},
            {"io_pressure_stop_pct": "40", "io_pressure_resume_pct": 15},
            {"io_pressure_stop_pct": True, "io_pressure_resume_pct": 15},
        ):
            broken = config_v2()
            broken["nodes"][0].update(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                controller.validate_config(broken)
            broken = config_v2()
            broken["thresholds"].update(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                controller.validate_config(broken)
        node_only = config_v2()
        node_only["nodes"][0].update(io_pressure_stop_pct=40, io_pressure_resume_pct=15)
        controller.validate_config(node_only)

    def test_slow_disk_node_is_drained_through_the_cycle_and_the_other_node_is_not(self):
        config = config_v2()
        config["nodes"][0].update(io_pressure_stop_pct=40, io_pressure_resume_pct=15)
        client = FakeClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 3, "name": "remote-agent", "no_schedule": False},
            ]
        )
        state = {}
        scrapes = {"builder-a": [(100, 1000.0), (130, 1030.0)], "builder-b": [(100, 1000.0), (101, 1030.0)]}
        changes_per_cycle = []
        for cycle in range(2):
            def fetch(url, cycle=cycle):
                node = "builder-a" if "builder-a" in url else "builder-b"
                waiting, _ = scrapes[node][cycle]
                return io_metrics(waiting, idle=80 + 30 * cycle)

            with patch.object(controller, "fetch_text", side_effect=fetch), patch.object(
                controller.time, "time", side_effect=lambda cycle=cycle: scrapes["builder-a"][cycle][1]
            ):
                controller.run_cycle_v2(config, state, client, apply=True)
            changes_per_cycle.append(list(client.changed))
        self.assertEqual(changes_per_cycle[0], [], "the first scrape has no previous one to compare")
        self.assertEqual(client.changed, [(1, True)], "only the node whose disk is stalled is drained")


LOAD_THRESHOLDS = dict(THRESHOLDS, load_per_cpu_stop=3.0, load_per_cpu_resume=2.5)


def load_metrics(load1, cpus=4, idle=80):
    lines = [f'node_cpu_seconds_total{{cpu="{c}",mode="idle"}} {idle if c == 0 else 0}' for c in range(cpus)]
    return "\n".join(lines) + f"""
node_cpu_seconds_total{{cpu="0",mode="user"}} 20
node_load1 {load1}
node_memory_MemAvailable_bytes 300
node_memory_MemTotal_bytes 1000
node_filesystem_avail_bytes{{device="/dev/vda1",fstype="ext4",mountpoint="/"}} 200
node_filesystem_size_bytes{{device="/dev/vda1",fstype="ext4",mountpoint="/"}} 1000
"""


class LiveFeatureTests(unittest.TestCase):
    """The load, per-node disk and heartbeat behaviour the installed controller has had since
    2026-10-11; pinned here when it was brought into the repository."""

    def test_normalized_load_is_load1_over_the_cpu_count(self):
        self.assertEqual(controller.node_snapshot(load_metrics(8, cpus=4)).load_per_cpu, 2.0)
        self.assertIsNone(controller.node_snapshot(METRICS).load_per_cpu)

    def test_load_thresholds_replace_the_cpu_busy_share(self):
        previous = controller.node_snapshot(load_metrics(1), taken_at=0.0)
        hot = controller.node_snapshot(load_metrics(14, idle=200), taken_at=30.0)  # 3.5 per CPU
        pressure = controller.evaluate_pressure(hot, previous, LOAD_THRESHOLDS)
        self.assertIn("cpu_load", pressure.breaches)
        self.assertNotIn("cpu", pressure.breaches)
        calm = controller.node_snapshot(load_metrics(4, idle=200), taken_at=30.0)  # 1.0 per CPU
        self.assertTrue(controller.evaluate_pressure(calm, previous, LOAD_THRESHOLDS).recovered)
        between = controller.node_snapshot(load_metrics(11, idle=200), taken_at=30.0)  # 2.75: above resume
        pressure = controller.evaluate_pressure(between, previous, LOAD_THRESHOLDS)
        self.assertEqual(pressure.breaches, ())
        self.assertFalse(pressure.recovered)

    def test_load_thresholds_fail_closed_without_a_load_metric(self):
        with self.assertRaisesRegex(ValueError, "normalized load"):
            controller.evaluate_pressure(controller.node_snapshot(METRICS), None, LOAD_THRESHOLDS)

    def test_a_nodes_disk_thresholds_override_only_the_disk_pair(self):
        entry = {"name": "n", "disk_thresholds": {"stop_pct": 6.8, "resume_pct": 7.5}}
        merged = controller.thresholds_for_node(THRESHOLDS, entry)
        self.assertEqual((merged["disk_stop_pct"], merged["disk_resume_pct"]), (6.8, 7.5))
        self.assertEqual(merged["cpu_stop_pct"], THRESHOLDS["cpu_stop_pct"])
        self.assertEqual(THRESHOLDS["disk_stop_pct"], 10)

    def test_config_validation_of_load_and_disk_thresholds(self):
        config = config_v2()
        config["thresholds"].update(load_per_cpu_stop=3.0, load_per_cpu_resume=2.5)
        config["nodes"][0]["disk_thresholds"] = {"stop_pct": 6.8, "resume_pct": 7.5}
        controller.validate_config(config)
        for mutate in (
            lambda c: c["thresholds"].pop("load_per_cpu_resume"),
            lambda c: c["thresholds"].update(load_per_cpu_resume=3.0),
            lambda c: c["nodes"][0].update(disk_thresholds={"stop_pct": 7.5, "resume_pct": 7.5}),
            lambda c: c["nodes"][0].update(disk_thresholds={"stop_pct": 6.8}),
            lambda c: c["nodes"][0].update(fail_closed_on_metrics_error="yes"),
        ):
            broken = copy.deepcopy(config)
            mutate(broken)
            with self.assertRaises(ValueError):
                controller.validate_config(broken)

    def test_agent_readiness_separates_the_api_flag_from_the_heartbeat(self):
        now = 1000.0
        fresh = controller.agent_readiness({"id": 1, "name": "a", "last_contact": 990, "capacity": 3}, now, 120)
        self.assertTrue(fresh["ready_to_poll"])
        self.assertEqual(fresh["capacity"], 3)
        stale = controller.agent_readiness({"id": 2, "name": "a", "last_contact": 700}, now, 120)
        self.assertEqual(stale["heartbeat_state"], "stale")
        self.assertFalse(stale["ready_to_poll"])
        drained = controller.agent_readiness({"id": 3, "name": "a", "last_contact": 990, "no_schedule": True}, now, 120, True)
        self.assertEqual(drained["reasons"], ["controller_drained"])
        manual = controller.agent_readiness({"id": 4, "name": "a", "last_contact": 990, "no_schedule": True}, now, 120)
        self.assertEqual(manual["reasons"], ["manual_or_external_no_schedule"])


class GradedAdmissionTests(unittest.TestCase):
    """A host with several one-slot agents opens as many as its headroom allows, one step at a
    time, instead of switching between all open and all drained."""

    GRADED = {"load_per_cpu_full": 0.5, "open_samples": 2, "close_samples": 2}

    def snapshot(self, load1, cpus=4, memory_available_pct=60.0):
        return controller.NodeSnapshot(
            0, 0, memory_available_pct, 50, load_per_cpu=load1 / cpus
        )

    def pressure(self, memory_available_pct=60.0, breaches=()):
        return controller.Pressure(None, memory_available_pct, 50, tuple(breaches), not breaches)

    def agents(self, closed=(), manual=()):
        return [
            {
                "id": i,
                "name": f"g{i}",
                "capacity": 1,
                "no_schedule": i in closed or i in manual,
            }
            for i in range(1, 5)
        ]

    def test_headroom_is_linear_between_the_full_level_and_the_stop_threshold(self):
        def headroom(load_per_cpu):
            return controller.graded_headroom(
                self.snapshot(load_per_cpu * 4), self.pressure(), LOAD_THRESHOLDS, self.GRADED
            )

        self.assertEqual(headroom(0.2), 1.0)
        self.assertAlmostEqual(headroom(1.75), 0.5)
        self.assertEqual(headroom(3.0), 0.0)
        self.assertEqual(headroom(9.0), 0.0)

    def test_scarcest_signal_decides_and_unknown_signals_do_not_limit(self):
        thresholds = dict(LOAD_THRESHOLDS, io_pressure_stop_pct=60, io_pressure_resume_pct=20)
        calm = self.snapshot(1.0)
        stalled = controller.Pressure(None, 60.0, 50, (), True, io_pressure_pct=40.0)
        self.assertAlmostEqual(controller.graded_headroom(calm, stalled, thresholds, self.GRADED), 0.5)
        unknown = controller.Pressure(None, 60.0, 50, (), True, io_pressure_pct=None)
        self.assertEqual(controller.graded_headroom(calm, unknown, thresholds, self.GRADED), 1.0)
        low_memory = controller.Pressure(None, 22.5, 50, (), False)
        self.assertAlmostEqual(controller.graded_headroom(calm, low_memory, thresholds, self.GRADED), 0.5)

    def test_closes_one_agent_after_the_sample_window_and_stops_at_its_share(self):
        agents, state, managed = self.agents(), {}, set()
        steps = []
        for _ in range(8):
            fresh = {str(a["id"]) for a in agents}
            action, agent, facts = controller.graded_step(
                state, agents, fresh, managed, 0.5, self.GRADED, False
            )
            steps.append((action, agent["id"] if agent else None))
            if action == "close":
                agent["no_schedule"] = True
                managed.add(str(agent["id"]))
        self.assertEqual([s for s in steps if s[0]], [("close", 4), ("close", 3)])
        self.assertEqual(steps[0][0], None, "one cycle over share is not enough")
        self.assertEqual(sum(not a["no_schedule"] for a in agents), 2)

    def test_never_closes_the_last_open_agent(self):
        agents, state = self.agents(closed={2, 3, 4}), {}
        for _ in range(5):
            action, agent, _facts = controller.graded_step(
                state, agents, {"1", "2", "3", "4"}, {"2", "3", "4"}, 0.0, self.GRADED, False
            )
            self.assertIsNone(action)

    def test_reopens_only_agents_the_controller_closed_and_never_a_manual_hold(self):
        agents, state = self.agents(closed={3}, manual={4}), {}
        managed = {"3"}
        seen = []
        for _ in range(6):
            action, agent, _facts = controller.graded_step(
                state, agents, {"1", "2", "3", "4"}, managed, 1.0, self.GRADED, False
            )
            if action == "open":
                agent["no_schedule"] = False
                managed.discard(str(agent["id"]))
            seen.append((action, agent["id"] if agent else None))
        self.assertEqual([s for s in seen if s[0]], [("open", 3)])
        self.assertTrue(agents[3]["no_schedule"], "the operator's hold on agent 4 survives")

    def test_a_hard_breach_blocks_reopening(self):
        agents, state = self.agents(closed={3, 4}), {}
        for _ in range(5):
            action, _agent, _facts = controller.graded_step(
                state, agents, {"1", "2", "3", "4"}, {"3", "4"}, 1.0, self.GRADED, True
            )
            self.assertIsNone(action)

    def test_stale_agents_do_not_count_as_slots(self):
        agents, state = self.agents(), {}
        _action, _agent, facts = controller.graded_step(
            state, agents, {"1", "2"}, set(), 1.0, self.GRADED, False
        )
        self.assertEqual((facts["total_slots"], facts["open_slots"]), (2, 2))

    def test_capacity_weights_the_slots_of_a_multi_slot_agent(self):
        agents = [
            {"id": 1, "name": "g1", "capacity": 3, "no_schedule": False},
            {"id": 2, "name": "g2", "capacity": 1, "no_schedule": False},
        ]
        _action, _agent, facts = controller.graded_step({}, agents, {"1", "2"}, set(), 0.5, self.GRADED, False)
        self.assertEqual((facts["total_slots"], facts["desired_slots"]), (4, 2))

    def test_cycle_closes_agents_of_a_loaded_host_and_leaves_the_other_host_alone(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["stop_samples"] = 10
        config["resume_samples"] = 20
        config["nodes"][0]["agent_names"] = ["g1", "g2", "g3", "g4"]
        config["nodes"][0]["graded"] = dict(self.GRADED)
        now = controller.time.time()
        client = FakeClient(
            [
                {"id": i, "name": f"g{i}", "capacity": 1, "no_schedule": False, "last_contact": now}
                for i in range(1, 5)
            ]
            + [{"id": 9, "name": "remote-agent", "capacity": 2, "no_schedule": False, "last_contact": now}]
        )
        state = {}
        controller.validate_config(config)
        with patch.object(
            controller,
            "fetch_text",
            side_effect=lambda url: load_metrics(8) if "builder-a" in url else load_metrics(1),
        ):
            for _ in range(2):
                controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(4, True)])
        self.assertEqual(state["nodes"]["builder-a"]["managed_agent_ids"], ["4"])
        self.assertEqual(state["nodes"]["builder-a"].get("graded_close_samples"), 0)

    def test_graded_host_reopens_one_agent_per_window_after_recovery(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["stop_samples"] = 10
        config["resume_samples"] = 1
        config["nodes"][0]["agent_names"] = ["g1", "g2", "g3", "g4"]
        config["nodes"][0]["graded"] = dict(self.GRADED)
        now = controller.time.time()
        client = FakeClient(
            [
                {"id": i, "name": f"g{i}", "capacity": 1, "no_schedule": i > 1, "last_contact": now}
                for i in range(1, 5)
            ]
            + [{"id": 9, "name": "remote-agent", "capacity": 2, "no_schedule": False, "last_contact": now}]
        )
        state = {"nodes": {"builder-a": {"managed_agent_ids": ["2", "3", "4"]}}}
        with patch.object(controller, "fetch_text", side_effect=lambda url: load_metrics(1)):
            for _ in range(2):
                controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(2, False)], "recovery reopens a single agent, not the whole host")

    def test_config_validation_of_graded_admission(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["nodes"][0]["graded"] = dict(self.GRADED)
        controller.validate_config(config)
        for bad in (
            {"load_per_cpu_full": 3.0},
            {"memory_full_pct": 15},
            {"min_open_slots": 0},
            {"open_samples": 0},
            {"surprise": 1},
        ):
            broken = copy.deepcopy(config)
            broken["nodes"][0]["graded"] = dict(self.GRADED, **bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                controller.validate_config(broken)

    def test_nodes_without_graded_behave_exactly_as_before(self):
        config = config_v2()
        client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": False}])
        state = {"nodes": {}}
        with patch.object(controller, "fetch_text", return_value=METRICS):
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [])



class HTTPClientPatchBody(unittest.TestCase):
    def test_drain_patch_keeps_name_and_custom_labels(self):
        client = controller.HTTPClient("http://woodpecker.invalid", "token")
        sent = []
        client._request = lambda path, method="GET", payload=None: sent.append((path, method, payload))
        client.set_no_schedule({"id": 7, "name": "builder-a", "no_schedule": False, "custom_labels": {"site": "x", "mem": "l"}}, True)
        client.set_no_schedule({"id": 8, "name": "builder-b", "no_schedule": True}, False)
        self.assertEqual(sent[0], ("/api/agents/7", "PATCH", {"name": "builder-a", "no_schedule": True, "custom_labels": {"site": "x", "mem": "l"}}))
        self.assertEqual(sent[1][2], {"name": "builder-b", "no_schedule": False, "custom_labels": {}})

if __name__ == "__main__":
    unittest.main()
