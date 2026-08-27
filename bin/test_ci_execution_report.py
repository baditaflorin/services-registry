#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("ci_execution_report.py")
SPEC = importlib.util.spec_from_file_location("ci_execution_report", MODULE_PATH)
assert SPEC and SPEC.loader
report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = report
SPEC.loader.exec_module(report)


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "default_execution_host": "0mcp",
            "agent_hosts": {"remote-agent": "0docker"},
            "agent_id_hosts": {"12": "0docker"},
        }
        self.agents = {1: "local-agent", 2: "remote-agent"}

    def test_default_host(self):
        host, names = report.classify_pipeline(
            {"workflows": [{"agent_id": 1}]}, self.agents, self.config
        )
        self.assertEqual(host, "0mcp")
        self.assertEqual(names, ["local-agent"])

    def test_agent_override(self):
        host, _ = report.classify_pipeline(
            {"workflows": [{"agent_id": 2}]}, self.agents, self.config
        )
        self.assertEqual(host, "0docker")

    def test_mixed_pipeline(self):
        host, _ = report.classify_pipeline(
            {"workflows": [{"agent_id": 1}, {"agent_id": 2}]},
            self.agents,
            self.config,
        )
        self.assertEqual(host, "mixed")

    def test_unassigned_pipeline(self):
        host, names = report.classify_pipeline(
            {"workflows": []}, self.agents, self.config
        )
        self.assertEqual((host, names), ("unassigned", []))

    def test_unknown_agent(self):
        host, names = report.classify_pipeline(
            {"workflows": [{"agent_id": 99}]}, self.agents, self.config
        )
        self.assertEqual(host, "unknown")
        self.assertEqual(names, ["unknown-agent-99"])

    def test_retired_agent_id_override(self):
        host, names = report.classify_pipeline(
            {"workflows": [{"agent_id": 12}]}, self.agents, self.config
        )
        self.assertEqual(host, "0docker")
        self.assertEqual(names, ["retired-agent-12"])


class ConfigTests(unittest.TestCase):
    def test_duplicate_plane_names_fail(self):
        config = {
            "version": 1,
            "control_planes": [
                {"name": "same", "url": "https://a", "default_execution_host": "a"},
                {"name": "same", "url": "https://b", "default_execution_host": "b"},
            ],
        }
        path = Path(self.id().replace(".", "-"))
        try:
            path.write_text(json_dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                report.load_config(path)
        finally:
            path.unlink(missing_ok=True)


class PaginationTests(unittest.TestCase):
    def test_repository_pagination_stops_on_no_new_ids(self):
        client = object.__new__(report.WoodpeckerClient)
        client.name = "test"
        pages = {
            1: [{"id": 1, "active": True, "full_name": "o/a"}],
            2: [{"id": 2, "active": True, "full_name": "o/b"}],
            3: [{"id": 2, "active": True, "full_name": "o/b"}],
        }

        def fake_get(_path, query):
            return pages[query["page"]]

        client.get = fake_get
        self.assertEqual([row["id"] for row in client.repositories()], [1, 2])


def json_dumps(value):
    import json

    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
