"""Tests for ci_cache_pilot_fleet_runner_report.py. No network, no fleet-runner: a fake runner
serves synthetic pipelines whose answer is known in advance."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_cache_pilot_fleet_runner_report as fleet  # noqa: E402

REPO = "owner/repo"
CUTOVER = 1_000_000
PILOT, CONTROL = 99, 5


def workflow(name, agent, start, seconds, state="success", steps=()):
    return {
        "name": name,
        "agent_id": agent,
        "state": state,
        "started": start,
        "finished": start + seconds,
        "children": [
            {"name": n, "started": start, "finished": start + s, "state": "success"} for n, s in steps
        ],
    }


def pipeline(number, *workflows):
    return {"number": number, "event": "push", "workflows": list(workflows)}


def scenario():
    """go: pilot 100 s -> 50 s, controls 100 s -> 100 s (so the effect is 0.5)."""
    out, n = [], 1
    for after, base in ((False, CUTOVER - 3600), (True, CUTOVER + 3600)):
        for i in range(3):
            start = base + i * 60
            pilot_s = 50 if after else 100
            out.append(pipeline(n, workflow("go", PILOT, start, pilot_s, steps=[("git", 30), ("test", pilot_s - 30)]),
                                workflow("woodpecker", PILOT, start, 999)))
            out.append(pipeline(n + 1, workflow("go", CONTROL, start, 100, steps=[("git", 10), ("test", 90)])))
            n += 2
    return out


class FetchTests(unittest.TestCase):
    def test_unreadable_numbers_are_counted_and_the_rest_kept_in_order(self):
        def runner(plane, repo, number):
            self.assertEqual((plane, repo), ("0exec", REPO))
            return None if number == 12 else {"workflows": [], "marker": number}

        got, failed = fleet.fetch("0exec", REPO, range(10, 15), workers=3, runner=runner)
        self.assertEqual(failed, 1)
        self.assertEqual([p["number"] for p in got], [10, 11, 13, 14])
        self.assertEqual([p["marker"] for p in got], [10, 11, 13, 14])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.summary = fleet.summarize(scenario(), REPO, {PILOT}, CUTOVER, window_days=1, min_runs=3)

    def test_effect_is_the_pilot_ratio_over_the_control_ratio(self):
        self.assertEqual(self.summary["pairs_compared"], 1)
        self.assertEqual(self.summary["pilot_after_over_before"], 0.5)
        self.assertEqual(self.summary["control_after_over_before"], 1.0)
        self.assertEqual(self.summary["effect"], 0.5)

    def test_gate_workflow_is_excluded_and_only_successes_count(self):
        # 12 pipelines x one "go" workflow each; the 6 gate workflows are not counted.
        self.assertEqual(self.summary["workflow_runs"], 12)
        failed = scenario() + [pipeline(99, workflow("go", PILOT, CUTOVER + 10, 5, state="failure"))]
        again = fleet.summarize(failed, REPO, {PILOT}, CUTOVER, window_days=1, min_runs=3)
        self.assertEqual(again["workflow_runs"], 12)

    def test_step_medians_use_only_workflows_after_the_cutover(self):
        steps = self.summary["steps_after_cutover"]
        self.assertEqual(steps["git"]["pilot"], (3, 30))
        self.assertEqual(steps["git"]["others"], (3, 10))
        self.assertEqual(steps["test"]["pilot"], (3, 20))
        self.assertEqual(steps["test"]["others"], (3, 90))

    def test_too_few_runs_in_a_cell_yields_a_note_not_a_number(self):
        few = fleet.summarize(scenario()[:4], REPO, {PILOT}, CUTOVER, window_days=1, min_runs=3)
        self.assertNotIn("effect", few)
        self.assertIn("note", few)


class CommandLineTests(unittest.TestCase):
    BASE = ["--repo", REPO, "--first", "1", "--last", "12", "--pilot-agent-id", "99",
            "--cutover", "1970-01-12T13:46:40Z"]

    def parse_fails(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fleet.parse_args(argv)

    def test_rejects_a_repo_that_is_not_owner_slash_name(self):
        self.parse_fails(["--repo", "no-slash", *self.BASE[2:]])
        self.parse_fails(["--repo", "a/b; rm -rf /", *self.BASE[2:]])

    def test_rejects_an_empty_or_huge_range(self):
        self.parse_fails([*self.BASE[:2], "--first", "9", "--last", "3", *self.BASE[6:]])
        self.parse_fails([*self.BASE[:2], "--first", "1", "--last", "9000", *self.BASE[6:]])

    def test_main_prints_json_with_the_effect(self):
        with mock.patch.object(fleet, "fetch", return_value=(scenario(), 2)) as fetch:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                argv = [*self.BASE, "--window-days", "1", "--json"]
                self.assertEqual(fleet.main(argv), 0)
        fetch.assert_called_once()
        data = json.loads(out.getvalue())
        self.assertEqual((data["fetched"], data["unreadable"]), (12, 2))
        self.assertEqual(data["effect"], 0.5)


if __name__ == "__main__":
    unittest.main()
