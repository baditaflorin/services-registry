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


if __name__ == "__main__":
    unittest.main()
