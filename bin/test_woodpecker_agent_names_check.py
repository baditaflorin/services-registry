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



class Repair(unittest.TestCase):
    def agents(self, **names):
        return [{"id": int(i[1:]), "name": n, "no_schedule": i == "i2", "custom_labels": {"site": "x"}} for i, n in names.items()]

    def test_wipe_is_undone_keeping_the_callers_drain_and_labels(self):
        known = {}
        check.repair_names(self.agents(i1="a", i2="b"), known, lambda a, n: None)
        self.assertEqual(known, {"1": "a", "2": "b"})
        wiped = self.agents(i1="a", i2="")           # a caller PATCHed agent 2 without a name; it is drained (no_schedule)
        sent = []
        self.assertEqual(check.repair_names(wiped, known, lambda a, n: sent.append((a["id"], n, a["no_schedule"], a["custom_labels"]))), 1)
        self.assertEqual(sent, [(2, "b", True, {"site": "x"})])
        self.assertEqual(wiped[1]["name"], "b")

    def test_unknown_id_and_taken_name_are_left_alone(self):
        sent = []
        self.assertEqual(check.repair_names(self.agents(i1="a", i2=""), {}, lambda a, n: sent.append(n)), 0)       # never seen
        self.assertEqual(check.repair_names(self.agents(i1="b", i2=""), {"2": "b"}, lambda a, n: sent.append(n)), 0)  # name now used by another
        self.assertEqual(sent, [])

    def test_failed_patch_keeps_the_agent_unnamed_and_does_not_raise(self):
        def boom(agent, name):
            raise OSError("down")
        agents = self.agents(i1="")
        self.assertEqual(check.repair_names(agents, {"1": "a"}, boom), 0)
        self.assertEqual(agents[0]["name"], "")

    def test_collect_with_state_counts_restores_and_reports_unnamed_after_repair(self):
        with tempfile.TemporaryDirectory() as d:
            token = Path(d, "t"); token.write_text("tok\n")
            state = {"p": {"names": {"1": "a"}, "repaired_total": 4}}
            calls = []
            text = check.collect([("p", "http://x", str(token), None)],
                                 lambda url, tok: [{"id": 1, "name": "", "no_schedule": False, "custom_labels": {"a": "b"}}],
                                 state, lambda url, tok, agent, name: calls.append((url, tok, agent["id"], name)))
        self.assertEqual(calls, [("http://x", "tok", 1, "a")])
        self.assertIn('woodpecker_agent_names_repaired_total{plane="p"} 5', text)
        self.assertIn('woodpecker_agents_unnamed{plane="p"} 0', text)
        self.assertEqual(state["p"]["repaired_total"], 5)

    def test_without_state_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            token = Path(d, "t"); token.write_text("tok\n")
            calls = []
            text = check.collect([("p", "http://x", str(token), None)], lambda u, t: [{"id": 1, "name": "", "custom_labels": {"a": "b"}}],
                                 None, lambda *a: calls.append(a))
        self.assertEqual(calls, [])
        self.assertIn('woodpecker_agents_unnamed{plane="p"} 1', text)
        self.assertNotIn("repaired_total{", text)

    def test_repair_flag_needs_state(self):
        with self.assertRaises(SystemExit):
            check.main(["--plane", "p,http://x,/nonexistent", "--repair"])


if __name__ == "__main__":
    unittest.main()
