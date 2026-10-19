"""Tests for woodpecker_controller_hold_report.py with a synthetic controller journal."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import woodpecker_controller_hold_report as rep  # noqa: E402

NODE = "pilot-node"
T0 = rep.parse_instant("2026-10-19T10:00:00Z")


def line(node, offset, schedulable, io, event="node_observed"):
    return json.dumps({"node": node, "event": event, "timestamp": T0 + offset,
                       "schedulable_agents": schedulable, "io_pressure_pct": io})


def journal():
    """Hour 10: 4 samples, 3 held. Hour 11: 4 samples, 1 held. Plus noise that must be ignored."""
    rows = [line(NODE, 60 * i, 0 if i < 3 else 1, 50 if i < 3 else 5) for i in range(4)]
    rows += [line(NODE, 3600 + 60 * i, 0 if i == 0 else 2, 30 if i == 0 else 1) for i in range(4)]
    rows += [
        line("other-node", 10, 0, 90),                 # another node
        line(NODE, 20, 0, 90, event="agent_readiness"),  # another event of the same node
        "not json but mentions pilot-node node_observed",  # garbage
        json.dumps({"event": "node_observed"}),         # no node
    ]
    return rows


class SamplesTests(unittest.TestCase):
    def test_only_node_observed_records_of_the_named_node_count(self):
        self.assertEqual(len(rep.samples(journal(), NODE)), 8)
        self.assertEqual(len(rep.samples(journal(), "other-node")), 1)
        self.assertEqual(rep.samples(journal(), "missing"), [])

    def test_a_node_with_zero_schedulable_agents_is_held(self):
        self.assertTrue(rep.held({"schedulable_agents": 0}))
        self.assertFalse(rep.held({"schedulable_agents": 1}))
        self.assertFalse(rep.held({}))  # no field: do not claim a hold


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.recs = rep.samples(journal(), NODE)

    def test_hourly_held_share_and_pressure(self):
        hours = rep.by_hour(self.recs)
        self.assertEqual(list(hours), ["10-19 10:00", "10-19 11:00"])
        self.assertEqual(hours["10-19 10:00"]["samples"], 4)
        self.assertEqual(hours["10-19 10:00"]["held_pct"], 75.0)
        self.assertEqual(hours["10-19 11:00"]["held_pct"], 25.0)
        self.assertEqual(hours["10-19 10:00"]["io_mean"], (50 * 3 + 5) / 4)
        self.assertEqual(hours["10-19 10:00"]["io_over_15_pct"], 75.0)

    def test_overall_share(self):
        self.assertEqual(rep.share(self.recs), (8, 50.0))
        self.assertEqual(rep.share([]), (0, 0.0))

    def test_split_reports_before_and_after(self):
        text = rep.report(self.recs, T0 + 3600)
        self.assertIn("all samples: 8, held 50 %", text)
        self.assertIn("before the split: 4 samples, held 75 %", text)
        self.assertIn("after  the split: 4 samples, held 25 %", text)


class CommandLineTests(unittest.TestCase):
    def run_main(self, argv, stdin_text):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rep.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_prints_the_report(self):
        rc, out, _ = self.run_main(["--node", NODE, "--split", "2026-10-19T11:00:00Z"], "\n".join(journal()))
        self.assertEqual(rc, 0)
        self.assertIn("held 50 %", out)
        self.assertIn("before the split: 4 samples, held 75 %", out)

    def test_exit_code_2_when_the_node_has_no_records(self):
        rc, out, err = self.run_main(["--node", "nobody"], "\n".join(journal()))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no node_observed records", err)


if __name__ == "__main__":
    unittest.main()
