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
    def test_physical_exporter_identity_and_unique_memory_samples(self):
        good='node_uname_info{nodename="physical"} 1\nnode_memory_MemAvailable_bytes 250\nnode_memory_MemTotal_bytes 1000\n'
        self.assertEqual(controller.physical_memory_pct(good,"physical"),25)
        for bad in (good.replace('nodename="physical"','nodename="other"'),
                    good+'node_memory_MemAvailable_bytes{instance="other"} 500\n',
                    good+'node_uname_info{nodename="physical"} 1\n',
                    good.replace('250','NaN')):
            with self.assertRaises(ValueError): controller.physical_memory_pct(bad,"physical")

    def test_physical_memory_pressure_drains_even_when_guest_healthy(self):
        config = config_v2()
        config["nodes"][0]["physical_memory"] = {"metrics_url": "http://physical/metrics", "expected_nodename":"physical", "stop_pct": 20, "resume_pct": 25}
        controller.validate_config(config)
        client = FakeClient([{"id":1,"name":"builder-agent","no_schedule":False}])
        state = {"version":2,"nodes":{}}
        physical = 'node_uname_info{nodename="physical"} 1\nnode_memory_MemAvailable_bytes 190\nnode_memory_MemTotal_bytes 1000\n'
        with patch.object(controller, "fetch_text", side_effect=[RECOVERED_METRICS, physical]), patch.object(controller, "log_event"):
            self.assertTrue(controller.run_cycle(config,state,client,apply=True))
        self.assertEqual(client.changed, [(1,True)])
        self.assertEqual(state["nodes"]["builder-a"]["managed_agent_ids"], ["1"])

    def test_missing_physical_metrics_fail_closed(self):
        config = config_v2()
        config["nodes"][0]["physical_memory"] = {"metrics_url": "http://physical/metrics", "expected_nodename":"physical", "stop_pct":20, "resume_pct":25}
        client = FakeClient([{"id":1,"name":"builder-agent","no_schedule":False}])
        with patch.object(controller, "fetch_text", side_effect=[RECOVERED_METRICS,"node_memory_MemTotal_bytes 1000\n"]), patch.object(controller, "log_event"):
            self.assertTrue(controller.run_cycle(config,{"version":2,"nodes":{}},client,apply=True))
        self.assertEqual(client.changed, [(1,True)])

    def test_physical_memory_hysteresis_and_schema(self):
        p=controller.Pressure(None,80,80,(),True)
        cfg={"metrics_url":"http://physical/metrics","expected_nodename":"physical","stop_pct":20,"resume_pct":25}
        self.assertIn("physical_memory",controller.physical_memory_pressure(p,20,cfg).breaches)
        self.assertFalse(controller.physical_memory_pressure(p,22,cfg).recovered)
        self.assertTrue(controller.physical_memory_pressure(p,25,cfg).recovered)
        for raw in ({"metrics_url":"x","expected_nodename":"physical","stop_pct":30,"resume_pct":20}, {"metrics_url":"x","expected_nodename":"physical","stop_pct":float("nan"),"resume_pct":30}):
            config=config_v2(); config["nodes"][0]["physical_memory"]=raw
            with self.assertRaises(ValueError): controller.validate_config(config)

    def test_graded_physical_gate_waits_full_recovery_before_opening(self):
        config=config_v2(); config["nodes"]=config["nodes"][:1]; config["resume_samples"]=2
        config["thresholds"].update({"load_per_cpu_stop":3.0,"load_per_cpu_resume":2.5})
        node=config["nodes"][0]
        node["physical_memory"]={"metrics_url":"http://physical/metrics","expected_nodename":"physical","stop_pct":20,"resume_pct":30}
        node["graded"]={"min_open_slots":1,"open_samples":1,"close_samples":1,"memory_full_pct":60}
        controller.validate_config(config)
        client=FakeClient([{"id":1,"name":"builder-agent","no_schedule":True,"last_contact":1000,"capacity":1}])
        state={"version":2,"nodes":{"builder-a":{"managed_agent_ids":["1"],"recovery_samples":0,
                "previous_snapshot":{"cpu_idle_seconds":100,"cpu_total_seconds":101,"memory_available_pct":80,"disk_available_pct":80}}}}
        guest=('node_cpu_seconds_total{cpu="0",mode="idle"} 101\nnode_cpu_seconds_total{cpu="0",mode="user"} 1\nnode_load1 0.1\n'
               'node_memory_MemAvailable_bytes 800\nnode_memory_MemTotal_bytes 1000\n'
               'node_filesystem_avail_bytes{mountpoint="/"} 800\nnode_filesystem_size_bytes{mountpoint="/"} 1000\n')
        between='node_uname_info{nodename="physical"} 1\nnode_memory_MemAvailable_bytes 250\nnode_memory_MemTotal_bytes 1000\n'
        healthy='node_uname_info{nodename="physical"} 1\nnode_memory_MemAvailable_bytes 400\nnode_memory_MemTotal_bytes 1000\n'
        with patch.object(controller,"fetch_text",side_effect=[guest,between,guest,healthy,guest,healthy]), patch.object(controller,"log_event"), patch.object(controller.time,"time",return_value=1000):
            controller.run_cycle(config,state,client,apply=True)
            controller.run_cycle(config,state,client,apply=True)
            self.assertEqual(client.changed,[])
            controller.run_cycle(config,state,client,apply=True)
        self.assertEqual(client.changed,[(1,False)])

    def test_worker_lease_expiry_is_utc_and_fail_closed(self):
        deadline = "2026-12-17T14:59:50Z"
        before = controller.datetime(2026, 12, 17, 14, 59, 49, tzinfo=controller.timezone.utc).timestamp()
        at = controller.datetime(2026, 12, 17, 14, 59, 50, tzinfo=controller.timezone.utc).timestamp()
        self.assertEqual(controller.worker_lease_expiry(deadline, before), (False, None))
        self.assertEqual(controller.worker_lease_expiry(deadline, at), (True, deadline))
        self.assertEqual(controller.worker_lease_expiry("2026-12-17T14:59:50+00:00", before), (True, "invalid expires_at"))
        self.assertEqual(controller.worker_lease_expiry(123, before), (True, "invalid expires_at"))

    def test_expired_worker_drains_even_when_it_is_the_minimum_schedulable_node(self):
        config = config_v2()
        config["nodes"] = [config["nodes"][0]]
        config["nodes"][0]["expires_at"] = "2000-01-01T00:00:00Z"
        config["nodes"][0]["graded"] = {"min_open_slots": 1}
        client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": False}])
        state = {"nodes": {}}
        with patch.object(controller, "fetch_text", side_effect=AssertionError("expired worker must not need metrics")):
            changed = controller.run_cycle_v2(config, state, client, apply=True)
        self.assertTrue(changed)
        self.assertEqual(client.changed, [(1, True)])
        self.assertTrue(client.remote_agents[0]["no_schedule"])
        self.assertTrue(state["nodes"]["builder-a"]["lease_expired"])

    def test_malformed_worker_expiry_fails_closed_without_reopening_a_held_agent(self):
        config = config_v2()
        config["nodes"] = [config["nodes"][0]]
        config["nodes"][0]["expires_at"] = "not-a-timestamp"
        client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": True}])
        state = {"nodes": {}}
        with patch.object(controller, "fetch_text", side_effect=AssertionError("expired worker must not need metrics")):
            changed = controller.run_cycle_v2(config, state, client, apply=True)
        self.assertFalse(changed)
        self.assertEqual(client.changed, [])
        self.assertTrue(client.remote_agents[0]["no_schedule"])
        self.assertEqual(state["nodes"]["builder-a"]["lease_expiry_reason"], "invalid expires_at")

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


class LostResponseClient(FakeClient):
    """A pause PATCH whose response never arrives: urlopen's read times out.

    With applied=True the server had already set no_schedule (the 2026-10-21
    case: the gateway logged the controller's PATCH /api/agents/101 as 499 and
    the agent stayed paused); with applied=False the request never landed.
    """

    def __init__(self, agents, applied):
        super().__init__(agents)
        self.applied = applied

    def set_no_schedule(self, agent, value):
        if value and not self.applied:
            raise TimeoutError("The read operation timed out")
        super().set_no_schedule(agent, value)
        if value:
            raise TimeoutError("The read operation timed out")


def agent_by_id(client, agent_id):
    return next(agent for agent in client.remote_agents if agent["id"] == agent_id)


class LostPauseResponseTests(unittest.TestCase):
    def test_state_sync_failure_prevents_the_pause_and_keeps_previous_state(self):
        import json
        import tempfile

        client = FakeClient([{"id": 7, "name": "builder-agent", "no_schedule": False}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            path.write_text('{"previous": true}\n', encoding="utf-8")
            state = {}
            with patch.object(controller.os, "fsync", side_effect=OSError("fixture sync failure")):
                with self.assertRaises(OSError):
                    controller.drain_recorded_first(
                        client, client.remote_agents[0], state,
                        lambda: controller.save_state(path, state, durable=True),
                    )
            self.assertEqual(json.loads(path.read_text()), {"previous": True})
            self.assertEqual(list(Path(directory).iterdir()), [path])
        self.assertEqual(client.changed, [])
        self.assertFalse(client.remote_agents[0]["no_schedule"])

    def test_pause_state_syncs_file_and_directory_before_sending_request(self):
        import json
        import tempfile

        client = FakeClient([{"id": 7, "name": "builder-agent", "no_schedule": False}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            state = {}
            events = []
            real_sync = controller.os.fsync
            real_patch = client.set_no_schedule

            def sync(fd):
                real_sync(fd)
                events.append("sync")

            def pause(agent, value):
                self.assertEqual(events, ["sync", "sync"])
                self.assertEqual(json.loads(path.read_text())["managed_agent_ids"], ["7"])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                events.append("pause")
                real_patch(agent, value)

            client.set_no_schedule = pause
            with patch.object(controller.os, "fsync", side_effect=sync):
                controller.drain_recorded_first(
                    client, client.remote_agents[0], state,
                    lambda: controller.save_state(path, state),
                )
            self.assertEqual(events, ["sync", "sync", "pause"])

    def quarantine_builder_b(self, applied):
        config = config_v2()
        config["nodes"][1]["fail_closed_on_metrics_error"] = True
        client = LostResponseClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 2, "name": "remote-agent", "no_schedule": False},
            ],
            applied,
        )
        state = {
            "nodes": {
                "builder-a": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "builder-b": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }
        persisted = []

        def fetch(url):
            if "builder-b" in url:
                raise TimeoutError("timed out")
            return METRICS

        with patch.object(controller, "fetch_text", side_effect=fetch):
            with self.assertRaises(TimeoutError):
                controller.run_cycle_v2(
                    config, state, client, apply=True, persist=lambda: persisted.append(copy.deepcopy(state))
                )
        # The record reached the state file before the request went out.
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["nodes"]["builder-b"]["managed_agent_ids"], ["2"])
        self.assertTrue(persisted[0]["nodes"]["builder-b"]["full_drain_pending_recovery"])
        return config, client, persisted[0]

    def test_v2_pause_applied_but_unanswered_is_restored_by_the_controller(self):
        config, client, state = self.quarantine_builder_b(applied=True)
        self.assertTrue(agent_by_id(client, 2)["no_schedule"])

        # The process restarts from the file written before the request; the
        # node recovers (two samples: the quarantine dropped the CPU baseline).
        for metrics in (RECOVERED_METRICS, RECOVERED_METRICS_NEXT):
            with patch.object(controller, "fetch_text", return_value=metrics):
                controller.run_cycle_v2(config, state, client, apply=True)

        self.assertEqual(client.changed, [(2, True), (2, False)])
        self.assertFalse(agent_by_id(client, 2)["no_schedule"])
        self.assertEqual(state["nodes"]["builder-b"]["managed_agent_ids"], [])

    def test_v2_pause_that_never_landed_is_forgotten_and_not_mistaken_for_a_hold(self):
        config, client, state = self.quarantine_builder_b(applied=False)
        self.assertFalse(agent_by_id(client, 2)["no_schedule"])

        with patch.object(controller, "fetch_text", return_value=RECOVERED_METRICS):
            controller.run_cycle_v2(config, state, client, apply=True)

        self.assertEqual(client.changed, [])
        self.assertEqual(state["nodes"]["builder-b"]["managed_agent_ids"], [])
        self.assertFalse(state["nodes"]["builder-b"]["managed_no_schedule"])

    def test_v2_ambiguous_pause_restart_keeps_graded_recovery_barrier(self):
        config, client, state = self.quarantine_builder_b(applied=True)
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["resume_samples"] = 3
        config["nodes"][1]["graded"] = {"load_per_cpu_full": 0.5, "open_samples": 1, "close_samples": 1}
        for agent in client.remote_agents:
            agent.update(capacity=1, last_contact=controller.time.time())
        with patch.object(controller, "fetch_text", return_value=load_metrics(0.1)):
            for _ in range(2):
                controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [(2, True)])
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(2, True), (2, False)])

    def test_v1_pause_applied_but_unanswered_is_restored_by_the_controller(self):
        config = config_v1()
        client = LostResponseClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 3, "name": "remote-agent", "no_schedule": False},
            ],
            applied=True,
        )
        state = {
            "agents": {
                "builder-agent": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
                "remote-agent": {"previous_snapshot": controller.NodeSnapshot(80, 100, 30, 20).__dict__},
            }
        }
        persisted = []

        def unreadable_backing(url):
            return BACKING_MISMATCHED if "pve-host" in url else METRICS

        with patch.object(controller, "fetch_text", side_effect=unreadable_backing):
            with self.assertRaises(TimeoutError):
                controller.run_cycle_v1(
                    config, state, client, apply=True, persist=lambda: persisted.append(copy.deepcopy(state))
                )
        state = persisted[-1]
        self.assertEqual(state["agents"]["builder-agent"]["managed_agent_ids"], ["1"])

        samples = iter(
            [
                (RECOVERED_METRICS, BACKING_HEALTHY),
                (RECOVERED_METRICS_NEXT, BACKING_HEALTHY),
                (RECOVERED_METRICS_NEXT2, BACKING_HEALTHY),
            ]
        )
        for _ in range(3):
            guest, backing = next(samples)
            with patch.object(
                controller, "fetch_text", side_effect=lambda url: backing if "pve-host" in url else guest
            ):
                controller.run_cycle_v1(config, state, client, apply=True)

        self.assertEqual(client.changed, [(1, True), (1, False)])
        self.assertFalse(agent_by_id(client, 1)["no_schedule"])

    def test_main_writes_the_record_to_the_state_file_although_the_cycle_failed(self):
        import json
        import tempfile

        config = config_v2()
        config["nodes"][1]["fail_closed_on_metrics_error"] = True
        client = LostResponseClient(
            [
                {"id": 1, "name": "builder-agent", "no_schedule": False},
                {"id": 2, "name": "remote-agent", "no_schedule": False},
            ],
            applied=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp, "config.json")
            config_path.write_text(json.dumps(config), encoding="utf-8")
            token_path = Path(tmp, "token")
            token_path.write_text("test-token\n", encoding="utf-8")
            state_path = Path(tmp, "state.json")

            def fetch(url):
                if "builder-b" in url:
                    raise TimeoutError("timed out")
                return METRICS

            with patch.object(controller, "HTTPClient", return_value=client), patch.object(
                controller, "fetch_text", side_effect=fetch
            ):
                code = controller.main(
                    [
                        "--config", str(config_path),
                        "--state", str(state_path),
                        "--token-file", str(token_path),
                        "--apply",
                        "--once",
                    ]
                )

            self.assertEqual(code, 1)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["nodes"]["builder-b"]["managed_agent_ids"], ["2"])
        self.assertTrue(saved["nodes"]["builder-b"]["managed_no_schedule"])


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

    def test_a_nodes_load_limits_override_only_that_node(self):
        original = dict(LOAD_THRESHOLDS)
        local = controller.thresholds_for_node(LOAD_THRESHOLDS, {"load_thresholds": {"stop": 0.8, "resume": 0.6}})
        self.assertEqual((local["load_per_cpu_stop"], local["load_per_cpu_resume"]), (0.8, 0.6))
        self.assertEqual(LOAD_THRESHOLDS, original)
        current = controller.node_snapshot(load_metrics(4, idle=200), taken_at=30.0)  # 1.0/core
        previous = controller.node_snapshot(load_metrics(1, idle=100), taken_at=0.0)
        self.assertIn("cpu_load", controller.evaluate_pressure(current, previous, local).breaches)
        self.assertNotIn("cpu_load", controller.evaluate_pressure(current, previous, LOAD_THRESHOLDS).breaches)

    def test_node_load_limits_validate_and_constrain_graded_range(self):
        config = config_v2()
        config["thresholds"].update(load_per_cpu_stop=3.0, load_per_cpu_resume=2.5)
        config["nodes"][0].update(load_thresholds={"stop": 0.8, "resume": 0.6}, graded={"load_per_cpu_full": 0.2})
        controller.validate_config(config)
        for value in ({"stop": 0.8}, {"stop": 0.8, "resume": 0.8}, {"stop": True, "resume": 0.2},
                      {"stop": float("nan"), "resume": 0.2}, {"stop": 0.8, "resume": -1}):
            broken = copy.deepcopy(config)
            broken["nodes"][0]["load_thresholds"] = value
            with self.assertRaises(ValueError):
                controller.validate_config(broken)
        broken = copy.deepcopy(config)
        broken["nodes"][0]["graded"]["load_per_cpu_full"] = 0.8
        with self.assertRaises(ValueError):
            controller.validate_config(broken)

    def test_missing_local_load_telemetry_quarantines_node(self):
        config = config_v2()
        config["nodes"][0]["load_thresholds"] = {"stop": 0.8, "resume": 0.6}
        client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": False}])
        state = {"version": 2, "nodes": {}}
        with patch.object(controller, "fetch_text", return_value=RECOVERED_METRICS), patch.object(controller, "log_event"):
            controller.run_cycle(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1, True)])
        self.assertEqual(state["nodes"]["builder-a"]["managed_agent_ids"], ["1"])

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


class PrometheusPhysicalSourceTests(unittest.TestCase):
    def source(self):
        return {"url": "http://100.123.48.115:9090/api/v1/query", "proxy_url": "http://127.0.0.1:1056",
                "job": "infer_node_exporter_via_tailscale", "host": "infer-hypervisor",
                "instance": "100.85.112.66:9100", "fleet": "0infer", "role": "hypervisor",
                "max_age_seconds": 60, "max_skew_seconds": 5}

    def response(self, available=8):
        source = self.source()
        values = {"up": 1, "identity": 1, "available": available, "total": 100}
        rows = []
        for name in controller.PROM_METRICS:
            for suffix, value in (("", values[name]), ("_timestamp", 990)):
                labels = {k: source[k] for k in controller.PROM_LABELS}
                labels["probe_kind"] = name + suffix
                if name == "identity":
                    labels["nodename"] = "pve01"
                rows.append({"metric": labels, "value": [1000, str(value)]})
        return {"status": "success", "data": {"resultType": "vector", "result": rows}}

    def row(self, data, kind):
        return next(r for r in data["data"]["result"] if r["metric"]["probe_kind"] == kind)

    def test_valid_source_and_rejects_insecure_or_invalid_configuration(self):
        source = self.source()
        controller.validate_prometheus_source(source)
        for key, value in (("url", "http://evil.example:9090/api/v1/query"),
                           ("url", "http://100.123.48.115:9090/other"),
                           ("proxy_url", "http://evil.example:1056"),
                           ("proxy_url", "http://user:pass@127.0.0.1:1056"),
                           ("job", 'bad"label'), ("max_age_seconds", 301), ("max_skew_seconds", True)):
            bad = dict(source, **{key: value})
            with self.assertRaises(ValueError):
                controller.validate_prometheus_source(bad)

    def test_parser_fails_closed_on_missing_duplicate_wrong_identity_and_stale_data(self):
        def duplicate(x):
            x["data"]["result"][-1] = copy.deepcopy(x["data"]["result"][0])
        for mutate in (
            lambda x: x["data"]["result"].pop(), duplicate,
            lambda x: self.row(x, "up")["value"].__setitem__(1, "0"),
            lambda x: self.row(x, "identity")["metric"].__setitem__("nodename", "other"),
            lambda x: self.row(x, "total")["metric"].__setitem__("instance", "other"),
            lambda x: self.row(x, "available")["value"].__setitem__(1, "NaN"),
            lambda x: self.row(x, "total_timestamp")["value"].__setitem__(1, "900"),
            lambda x: self.row(x, "total_timestamp")["value"].__setitem__(1, "1006"),
            lambda x: self.row(x, "total_timestamp")["value"].__setitem__(1, "989"),
            lambda x: x.__setitem__("warnings", ["partial data"]),
        ):
            response = self.response()
            mutate(response)
            with self.assertRaises(ValueError):
                controller.parse_prometheus_physical(response, self.source(), "pve01", 1000, 1000)
        self.assertEqual(controller.parse_prometheus_physical(self.response(), self.source(), "pve01", 1000, 1000), 8)
        with self.assertRaises(ValueError):
            controller.parse_prometheus_physical(self.response(), self.source(), "pve01", 1000, 1006)

    def test_transport_deadline_oversize_and_errors_fail_closed(self):
        import subprocess
        for effect in (subprocess.TimeoutExpired("probe", 4), OSError("unreachable"),
                       subprocess.CalledProcessError(1, "probe")):
            with patch.object(controller.subprocess, "run", side_effect=effect), self.assertRaises(ValueError):
                controller.prometheus_physical_pct(self.source(), "pve01")
        for body in (b"x" * (controller.PROM_MAX_BYTES + 1), b"not json", b'{"status":"success","status":"error"}', b'{"x":NaN}'):
            result = subprocess.CompletedProcess([], 0, stdout=body)
            with patch.object(controller.subprocess, "run", return_value=result), self.assertRaises(ValueError):
                controller.prometheus_physical_pct(self.source(), "pve01")

    def test_transport_refuses_http_redirects(self):
        import http.server
        import subprocess
        import threading
        paths = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/other")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%s/start" % server.server_port
            result = subprocess.run([sys.executable, "-c", controller.PROM_FETCH_CHILD, url],
                                    env={"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"},
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(paths, ["/start"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_full_cycle_drains_on_low_physical_memory_and_on_transport_failure(self):
        import json
        import subprocess
        for body, error in ((json.dumps(self.response()).encode(), None), (None, OSError("unreachable"))):
            config = config_v2()
            config["nodes"] = [config["nodes"][0]]
            config["nodes"][0]["physical_memory"] = {"prometheus": self.source(), "expected_nodename": "pve01", "stop_pct": 10, "resume_pct": 15}
            controller.validate_config(config)
            client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": False}])
            state = {"nodes": {}}
            result = subprocess.CompletedProcess([], 0, stdout=body)
            with patch.object(controller, "fetch_text", return_value=RECOVERED_METRICS), patch.object(controller.time, "time", return_value=1000), patch.object(controller, "log_event"), patch.object(controller.subprocess, "run", side_effect=error, return_value=result) as run:
                controller.run_cycle_v2(config, state, client, apply=True)
            self.assertEqual(client.changed, [(1, True)])
            self.assertEqual(run.call_args.kwargs["timeout"], 4)
            self.assertEqual(run.call_args.kwargs["env"]["http_proxy"], "http://127.0.0.1:1056")
            self.assertEqual(run.call_args.kwargs["env"]["no_proxy"], "")

    def test_hold_skips_source_and_release_requires_full_healthy_window(self):
        import json
        import subprocess
        config = config_v2()
        config["resume_samples"] = 3
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        node = config["nodes"][0]
        node["graded"] = {"min_open_slots": 1, "open_samples": 1, "close_samples": 1, "load_per_cpu_full": 0.5}
        node["admission_hold"] = True
        node["physical_memory"] = {"prometheus": self.source(), "expected_nodename": "pve01", "stop_pct": 10, "resume_pct": 15}
        controller.validate_config(config)
        client = FakeClient([
            {"id": 1, "name": "builder-agent", "no_schedule": True, "capacity": 1, "last_contact": 1000},
            {"id": 2, "name": "remote-agent", "no_schedule": False, "capacity": 1, "last_contact": 1000},
        ])
        state = {"nodes": {"builder-a": {"managed_agent_ids": ["1"], "recovery_samples": 99}}}
        with patch.object(controller, "fetch_text", return_value=load_metrics(0.1)), patch.object(controller.time, "time", return_value=1000), patch.object(controller, "log_event"), patch.object(controller.subprocess, "run", side_effect=AssertionError("hold must not probe")):
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [])
        self.assertEqual(state["nodes"]["builder-a"]["recovery_samples"], 0)
        del node["admission_hold"]
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(self.response(available=30)).encode())
        with patch.object(controller, "fetch_text", return_value=load_metrics(0.1)), patch.object(controller.time, "time", return_value=1000), patch.object(controller, "log_event"), patch.object(controller.subprocess, "run", return_value=result):
            for _ in range(2):
                controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [])
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1, False)])


class GradedAdmissionTests(unittest.TestCase):
    """A host with several one-slot agents opens as many as its headroom allows, one step at a
    time, instead of switching between all open and all drained."""

    GRADED = {"load_per_cpu_full": 0.5, "open_samples": 2, "close_samples": 2}

    def test_admission_hold_preserves_manual_hold_and_recovery_window(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["resume_samples"] = 3
        node = config["nodes"][0]
        node.update(agent_names=["g1", "g2", "manual"], graded=dict(self.GRADED, open_samples=1), admission_hold=True)
        now = controller.time.time()
        client = FakeClient([
            {"id": 1, "name": "g1", "capacity": 1, "no_schedule": True, "last_contact": now},
            {"id": 2, "name": "g2", "capacity": 1, "no_schedule": True, "last_contact": now},
            {"id": 3, "name": "manual", "capacity": 1, "no_schedule": True, "last_contact": now},
            {"id": 9, "name": "remote-agent", "capacity": 1, "no_schedule": False, "last_contact": now},
        ])
        state = {"nodes": {"builder-a": {"managed_agent_ids": ["1", "2"], "recovery_samples": 99}}}
        with patch.object(controller, "fetch_text", return_value=load_metrics(0.1)):
            for _ in range(4):
                controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [])
                self.assertEqual(state["nodes"]["builder-a"]["recovery_samples"], 0)
            del node["admission_hold"]
            for _ in range(2):
                controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [])
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1, False)])
        self.assertTrue(agent_by_id(client, 3)["no_schedule"])

    def test_admission_hold_records_open_agent_before_drain_and_dry_run_is_read_only(self):
        config = config_v2()
        node = config["nodes"][0]
        node["admission_hold"] = True
        client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": False}])
        state = {"nodes": {}}
        with patch.object(controller, "fetch_text", return_value=RECOVERED_METRICS):
            controller.run_cycle_v2(config, state, client, apply=False)
        self.assertEqual(client.changed, [])
        persisted = []
        with patch.object(controller, "fetch_text", return_value=RECOVERED_METRICS):
            controller.run_cycle_v2(config, state, client, apply=True, persist=lambda: persisted.append(copy.deepcopy(state)))
        self.assertEqual(client.changed, [(1, True)])
        saved = persisted[0]["nodes"]["builder-a"]
        self.assertEqual(saved["managed_agent_ids"], ["1"])
        self.assertTrue(saved["full_drain_pending_recovery"])
        self.assertEqual(saved["recovery_samples"], 0)
        node["admission_hold"] = "yes"
        with self.assertRaisesRegex(ValueError, "admission_hold must be a boolean"):
            controller.validate_config(config)

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

    def test_minimum_schedulable_node_still_closes_excess_graded_slots(self):
        config = config_v2()
        config["nodes"] = [config["nodes"][0]]
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["stop_samples"], config["resume_samples"] = 1, 2
        config["nodes"][0]["agent_names"] = ["g1", "g2", "g3", "g4"]
        config["nodes"][0]["load_thresholds"] = {"stop": 0.8, "resume": 0.6}
        config["nodes"][0]["graded"] = {"load_per_cpu_full": 0.2, "close_samples": 1, "min_open_slots": 1}
        now = controller.time.time()
        client = FakeClient([{"id": i, "name": f"g{i}", "capacity": 1, "no_schedule": False, "last_contact": now} for i in range(1, 5)])
        state = {}
        controller.validate_config(config)
        with patch.object(controller, "fetch_text", return_value=load_metrics(8)):
            for _ in range(5):
                controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(4, True), (3, True), (2, True)])
        self.assertFalse(agent_by_id(client, 1)["no_schedule"])

    def test_graded_pause_lost_response_reconciles_after_restart_and_preserves_manual_hold(self):
        for applied in (True, False):
            with self.subTest(applied=applied):
                config = config_v2()
                config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
                config["stop_samples"], config["resume_samples"] = 10, 20
                config["nodes"][0]["agent_names"] = ["g1", "g2", "g3", "g4"]
                config["nodes"][0]["graded"] = dict(self.GRADED)
                now = controller.time.time()
                client = LostResponseClient(
                    [{"id": i, "name": f"g{i}", "capacity": 1,
                      "no_schedule": i == 3, "last_contact": now} for i in range(1, 5)]
                    + [{"id": 9, "name": "remote-agent", "capacity": 2,
                        "no_schedule": False, "last_contact": now}], applied,
                )
                state, persisted = {}, []
                with patch.object(controller, "fetch_text", side_effect=lambda url: load_metrics(8) if "builder-a" in url else load_metrics(1)):
                    controller.run_cycle_v2(config, state, client, apply=True)
                    with self.assertRaises(TimeoutError):
                        controller.run_cycle_v2(config, state, client, apply=True,
                                                persist=lambda: persisted.append(copy.deepcopy(state)))
                self.assertEqual(len(persisted), 1)
                self.assertEqual(persisted[0]["nodes"]["builder-a"]["managed_agent_ids"], ["4"])
                state = persisted[0]
                with patch.object(controller, "fetch_text", return_value=load_metrics(1)):
                    for _ in range(2):
                        controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [(4, True), (4, False)] if applied else [])
                self.assertFalse(agent_by_id(client, 4)["no_schedule"])
                self.assertTrue(agent_by_id(client, 3)["no_schedule"])
                self.assertEqual(state["nodes"]["builder-a"]["managed_agent_ids"], [])

    def test_graded_host_reopens_one_agent_per_window_after_recovery(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["stop_samples"] = 10
        config["resume_samples"] = 20
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

    def test_fully_drained_graded_node_waits_full_recovery_window(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["resume_samples"] = 3
        config["nodes"][0]["graded"] = dict(self.GRADED, open_samples=1)
        now = controller.time.time()
        client = FakeClient([
            {"id": 1, "name": "builder-agent", "capacity": 1, "no_schedule": True, "last_contact": now},
            {"id": 9, "name": "remote-agent", "capacity": 1, "no_schedule": False, "last_contact": now},
        ])
        state = {"nodes": {"builder-a": {"managed_agent_ids": ["1"]}}}
        with patch.object(controller, "fetch_text", side_effect=lambda url: load_metrics(0.1)):
            for _ in range(2):
                controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [], "full drain must survive every incomplete recovery window")
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1, False)])

    def test_full_drain_barrier_survives_restart_and_manual_open(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["resume_samples"] = 3
        config["nodes"][0]["agent_names"] = ["g1", "g2"]
        config["nodes"][0]["graded"] = dict(self.GRADED, open_samples=1)
        now = controller.time.time()
        client = FakeClient([
            {"id": 1, "name": "g1", "capacity": 1, "no_schedule": False, "last_contact": now},
            {"id": 2, "name": "g2", "capacity": 1, "no_schedule": True, "last_contact": now},
            {"id": 9, "name": "remote-agent", "capacity": 1, "no_schedule": False, "last_contact": now},
        ])
        state = {"nodes": {"builder-a": {"managed_agent_ids": ["1", "2"], "full_drain_pending_recovery": True}}}
        with patch.object(controller, "fetch_text", side_effect=lambda url: load_metrics(0.1)):
            for _ in range(2):
                controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [])
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(2, False)])

    def test_full_drain_recovery_window_restarts_after_new_pressure(self):
        config = config_v2()
        config["thresholds"] = copy.deepcopy(LOAD_THRESHOLDS)
        config["resume_samples"] = 3
        config["nodes"][0]["graded"] = dict(self.GRADED, open_samples=1)
        now = controller.time.time()
        client = FakeClient([
            {"id": 1, "name": "builder-agent", "capacity": 1, "no_schedule": True, "last_contact": now},
            {"id": 9, "name": "remote-agent", "capacity": 1, "no_schedule": False, "last_contact": now},
        ])
        state = {"nodes": {"builder-a": {"managed_agent_ids": ["1"]}}}
        with patch.object(controller, "fetch_text") as fetch:
            for load in (0.1, 0.1, 16, 0.1, 0.1):
                fetch.return_value = load_metrics(load)
                controller.run_cycle_v2(config, state, client, apply=True)
                self.assertEqual(client.changed, [])
            fetch.return_value = load_metrics(0.1)
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1, False)])

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



class IndependentCpuBusyTests(unittest.TestCase):
    def metrics(self, idle, user, system=10, load=0.1):
        return (f'node_cpu_seconds_total{{cpu="0",mode="idle"}} {idle}\n'
                f'node_cpu_seconds_total{{cpu="0",mode="user"}} {user}\n'
                f'node_cpu_seconds_total{{cpu="0",mode="system"}} {system}\n'
                f'node_load1 {load}\nnode_memory_MemAvailable_bytes 800\nnode_memory_MemTotal_bytes 1000\n'
                'node_filesystem_avail_bytes{mountpoint="/"} 800\nnode_filesystem_size_bytes{mountpoint="/"} 1000\n')

    def config(self):
        config = config_v2()
        config["nodes"] = config["nodes"][:1]
        config["nodes"][0]["cpu_busy"] = {"stop_pct": 80, "resume_pct": 60, "max_sample_age_seconds": 90}
        config["nodes"][0]["load_thresholds"] = {"stop": 1.5, "resume": 1.1}
        config["stop_samples"] = 2
        config["resume_samples"] = 2
        controller.validate_config(config)
        return config

    def test_cpu_counter_integrity_and_expiry(self):
        good = self.metrics(80, 10)
        first = controller.cpu_guard_sample(good, 1000)
        second = controller.cpu_guard_sample(self.metrics(85, 35), 1030)
        self.assertAlmostEqual(controller.cpu_guard_pct(second, first, 90), 100 * 25 / 30)
        for malformed in (good + good.splitlines()[0] + '\n', good.replace(' 80\n', ' NaN\n'),
                          good.replace('mode="system"', 'mode="bad"'), good.replace(' 80\n', ' rubbish\n')):
            with self.assertRaises(ValueError): controller.cpu_guard_sample(malformed, 1000)
        for old in (None, first | {"taken_at": 900}, first | {"taken_at": 1040},
                    controller.cpu_guard_sample(self.metrics(100, 10), 1000),
                    first | {"counters": {"different": {"idle": 1}}}):
            with self.assertRaises(ValueError): controller.cpu_guard_pct(second, old, 90)
        with self.assertRaises(ValueError): controller.cpu_guard_pct(first | {"taken_at": 1030}, first, 90)

    def test_low_load_high_cpu_requires_both_recoveries_and_warmup_fails_closed(self):
        config = self.config()
        client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": False}])
        state = {"nodes": {}}
        samples = [(1000, self.metrics(80,10)), (1030, self.metrics(85,35)),
                   (1060, self.metrics(90,60)), (1090, self.metrics(115,65,load=1.3)),
                   (1120, self.metrics(140,70)), (1150, self.metrics(165,75))]
        with patch.object(controller, "fetch_text") as fetch, patch.object(controller.time, "time") as clock, patch.object(controller, "log_event"):
            for now, metrics in samples:
                fetch.return_value = metrics
                clock.return_value = now
                controller.run_cycle_v2(config, state, client, apply=True)
                if now < 1150:
                    self.assertEqual(client.changed, [(1, True)])
        self.assertEqual(client.changed, [(1, True), (1, False)])

    def test_high_cpu_drains_open_agent_after_hysteresis_even_at_node_floor(self):
        config = self.config()
        client = FakeClient([{"id": 1, "name": "builder-agent", "no_schedule": False}])
        state = {"nodes": {"builder-a": {"cpu_guard_baseline": controller.cpu_guard_sample(self.metrics(80, 10), 1000)}}}
        with patch.object(controller, "fetch_text") as fetch, patch.object(controller.time, "time") as clock, patch.object(controller, "log_event"):
            for now, metrics in [(1030, self.metrics(85,35)), (1060, self.metrics(90,60))]:
                fetch.return_value = metrics
                clock.return_value = now
                controller.run_cycle_v2(config, state, client, apply=True)
            self.assertEqual(client.changed, [(1, True)])

    def test_stale_or_bad_configuration_refuses(self):
        for change in ({"stop_pct": 50, "resume_pct": 60, "max_sample_age_seconds":90},
                       {"stop_pct": float('nan'), "resume_pct": 60, "max_sample_age_seconds":90},
                       {"stop_pct": 80, "resume_pct": 60, "max_sample_age_seconds":1}):
            config = self.config(); config["nodes"][0]["cpu_busy"] = change
            with self.assertRaises(ValueError): controller.validate_config(config)
        config = self.config(); client = FakeClient([{"id":1,"name":"builder-agent","no_schedule":False}])
        state = {"nodes":{"builder-a":{"cpu_guard_baseline":controller.cpu_guard_sample(self.metrics(80,10), 1000)}}}
        with patch.object(controller, "fetch_text", return_value=self.metrics(85,35)), patch.object(controller.time, "time", return_value=1100), patch.object(controller, "log_event"):
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertEqual(client.changed, [(1,True)])

    def test_graded_waits_for_cpu_and_complete_recovery_window(self):
        config = self.config()
        config["nodes"][0]["graded"] = {"min_open_slots": 1, "open_samples": 1, "close_samples": 1, "memory_full_pct": 60}
        client = FakeClient([{"id":1,"name":"builder-agent","no_schedule":True,"last_contact":1000,"capacity":1}])
        state = {"nodes":{"builder-a":{"managed_agent_ids":["1"], "cpu_guard_baseline":controller.cpu_guard_sample(self.metrics(80,10), 1000)}}}
        samples = [(1030, self.metrics(89,31)), (1060, self.metrics(114,36)), (1090, self.metrics(139,41))]
        with patch.object(controller, "fetch_text") as fetch, patch.object(controller.time, "time") as clock, patch.object(controller, "log_event"):
            for now, metrics in samples:
                fetch.return_value = metrics; clock.return_value = now
                client.remote_agents[0]["last_contact"] = now
                controller.run_cycle_v2(config, state, client, apply=True)
                if now < 1090: self.assertEqual(client.changed, [])
        self.assertEqual(client.changed, [(1,False)])

    def test_sample_expiring_while_other_nodes_are_scraped_quarantines(self):
        config = self.config(); config["nodes"].append(config_v2()["nodes"][1])
        state = {"nodes":{"builder-a":{"cpu_guard_baseline":controller.cpu_guard_sample(self.metrics(80,10), 1000)}}}
        client = FakeClient([{"id":1,"name":"builder-agent","no_schedule":False},
                             {"id":2,"name":"remote-agent","no_schedule":False}])
        now = [1030]
        def fetch(url):
            if "builder-b" in url: now[0] = 1130
            return self.metrics(85,35)
        with patch.object(controller, "fetch_text", side_effect=fetch), patch.object(controller.time, "time", side_effect=lambda: now[0]), patch.object(controller, "log_event"):
            controller.run_cycle_v2(config, state, client, apply=True)
        self.assertIn((1,True), client.changed)

    def test_expiry_between_restore_patches_recloses_first_agent(self):
        config = self.config(); config["resume_samples"] = 1
        config["nodes"][0]["agent_names"] = ["builder-agent", "builder-agent-2"]
        state = {"nodes":{"builder-a":{"managed_agent_ids":["1","2"],
                 "cpu_guard_baseline":controller.cpu_guard_sample(self.metrics(80,10),1000)}}}
        client = FakeClient([{"id":1,"name":"builder-agent","no_schedule":True},
                             {"id":2,"name":"builder-agent-2","no_schedule":True}])
        now = [1030]; set_schedule = client.set_no_schedule
        def delayed(agent, value):
            set_schedule(agent,value)
            if not value: now[0] = 1130
        client.set_no_schedule = delayed
        with patch.object(controller,"fetch_text",return_value=self.metrics(105,15)), patch.object(controller.time,"time",side_effect=lambda:now[0]), patch.object(controller,"log_event"):
            controller.run_cycle_v2(config,state,client,apply=True)
        self.assertEqual(client.changed, [(1,False),(1,True)])
        self.assertEqual(set(state["nodes"]["builder-a"]["managed_agent_ids"]), {"1","2"})
        self.assertTrue(state["nodes"]["builder-a"]["full_drain_pending_recovery"])

    def test_expiry_before_graded_open_keeps_closed(self):
        config = self.config(); config["resume_samples"] = 1
        config["nodes"][0]["graded"] = {"min_open_slots":1,"open_samples":1,"close_samples":1,"memory_full_pct":60}
        state = {"nodes":{"builder-a":{"managed_agent_ids":["1"],
                 "cpu_guard_baseline":controller.cpu_guard_sample(self.metrics(80,10),1000)}}}
        client = FakeClient([{"id":1,"name":"builder-agent","no_schedule":True,"last_contact":1030,"capacity":1}])
        now = [1030]; graded_step = controller.graded_step
        def delayed(*args,**kwargs):
            result = graded_step(*args,**kwargs); now[0] = 1130; return result
        with patch.object(controller,"fetch_text",return_value=self.metrics(105,15)), patch.object(controller.time,"time",side_effect=lambda:now[0]), patch.object(controller,"graded_step",side_effect=delayed), patch.object(controller,"log_event"):
            controller.run_cycle_v2(config,state,client,apply=True)
        self.assertEqual(client.changed, [])
        self.assertTrue(state["nodes"]["builder-a"]["full_drain_pending_recovery"])


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
