#!/usr/bin/env python3

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("woodpecker_agent_names_check.py")
SPEC = importlib.util.spec_from_file_location("woodpecker_agent_names_check", MODULE_PATH)
assert SPEC and SPEC.loader
check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)

CONFIG = {"nodes": [
    {"name": "infer", "agent_names": ["infer-a", "infer-b"]},
    {"name": "gone", "agent_names": ["old-agent"]},
]}


class AgentNamesCheck(unittest.TestCase):
    def test_clean_plane(self):
        agents = [{"id": 1, "name": "infer-a", "custom_labels": {"site": "x"}}, {"id": 2, "name": "infer-b", "custom_labels": {"site": "x"}}]
        text = "\n".join(check.plane_metrics("p", agents, {"nodes": CONFIG["nodes"][:1]}))
        self.assertIn('woodpecker_agents_unnamed{plane="p"} 0', text)
        self.assertIn('woodpecker_load_controller_missing_agents{plane="p",node="infer"} 0', text)

    def test_emptied_name_and_stale_entry_are_counted(self):
        agents = [{"id": 1, "name": "infer-a", "custom_labels": {"site": "x"}}, {"id": 2, "name": "", "custom_labels": {}}]
        text = "\n".join(check.plane_metrics("p", agents, CONFIG))
        self.assertIn('woodpecker_agents_unnamed{plane="p"} 1', text)
        self.assertIn('woodpecker_agents_without_labels{plane="p"} 1', text)
        self.assertIn('woodpecker_load_controller_missing_agents{plane="p",node="infer"} 1', text)
        self.assertIn('woodpecker_load_controller_missing_agents{plane="p",node="gone"} 1', text)

    def test_whitespace_name_is_unnamed(self):
        self.assertIn('unnamed{plane="p"} 1', "\n".join(check.plane_metrics("p", [{"id": 1, "name": "  ", "custom_labels": {"a": "b"}}], None)))

    def test_unread_plane_reports_down_and_nothing_else(self):
        self.assertEqual(check.plane_metrics("p", None, CONFIG), ['woodpecker_agent_names_check_up{plane="p"} 0'])

    def test_collect_survives_a_failing_plane_and_groups_samples(self):
        with tempfile.TemporaryDirectory() as d:
            token = Path(d, "t"); token.write_text("secret\n")
            cfg = Path(d, "c.json"); cfg.write_text(json.dumps(CONFIG))

            def fetch(url, tok):
                self.assertEqual(tok, "secret")
                if "bad" in url:
                    raise OSError("down")
                return [{"id": 1, "name": "infer-a", "custom_labels": {"a": "b"}}]

            text = check.collect([("good", "http://good", str(token), str(cfg)), ("bad", "http://bad", str(token), None)], fetch)
        self.assertIn('woodpecker_agent_names_check_up{plane="bad"} 0', text)
        self.assertIn('woodpecker_agent_names_check_up{plane="good"} 1', text)
        self.assertEqual(text.count("# TYPE woodpecker_agent_names_check_up"), 1)
        self.assertNotIn("secret", text)

    def test_parse_plane_rejects_bad_value(self):
        with self.assertRaises(Exception):
            check.parse_plane("only,two")


if __name__ == "__main__":
    unittest.main()
