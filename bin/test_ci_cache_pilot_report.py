import contextlib
import io
import unittest

import ci_cache_pilot_report as report

CUTOVER = 1_000_000
DAY = 86_400


def run(repo, wf, agent, started, seconds):
    return report.Run(repo, wf, agent, "push", started, started + seconds)


class ParseInstantTests(unittest.TestCase):
    def test_iso_utc_and_unix_agree(self):
        self.assertEqual(report.parse_instant("2026-10-20T09:00:00Z"), 1792486800)
        self.assertEqual(report.parse_instant("1792486800"), 1792486800)

    def test_offset_is_honoured(self):
        self.assertEqual(
            report.parse_instant("2026-10-20T12:00:00+03:00"),
            report.parse_instant("2026-10-20T09:00:00Z"),
        )


class RunsFromDetailTests(unittest.TestCase):
    def test_keeps_only_successful_timed_attributed_workflows(self):
        detail = {
            "workflows": [
                {"name": "go", "state": "success", "agent_id": 3, "started": 100, "finished": 160},
                {"name": "go", "state": "failure", "agent_id": 3, "started": 100, "finished": 160},
                {"name": "woodpecker", "state": "success", "agent_id": 3, "started": 100, "finished": 900},
                {"name": "lint", "state": "success", "agent_id": 0, "started": 100, "finished": 160},
                {"name": "lint", "state": "success", "agent_id": 3, "started": 0, "finished": 160},
                {"name": "lint", "state": "success", "agent_id": 3, "started": 200, "finished": 100},
            ]
        }
        runs = report.runs_from_detail("o/r", "push", detail)
        self.assertEqual([(r.workflow, r.seconds) for r in runs], [("go", 60)])

    def test_extra_excluded_workflow(self):
        detail = {"workflows": [{"name": "docs", "state": "success", "agent_id": 1, "started": 1, "finished": 5}]}
        self.assertEqual(report.runs_from_detail("o/r", "push", detail, {"docs"}), [])

    def test_missing_workflows_is_empty(self):
        self.assertEqual(report.runs_from_detail("o/r", "push", {}), [])


class ResolveAgentTests(unittest.TestCase):
    AGENTS = {1: "0docker-a", 99: "0own-build-agent", 100: "pve01-fleet-agent"}

    def test_by_name_and_by_id(self):
        self.assertEqual(report.resolve_agent_ids(self.AGENTS, "0own-build-agent"), {99})
        self.assertEqual(report.resolve_agent_ids(self.AGENTS, "100"), {100})

    def test_unknown_lists_known_names(self):
        with self.assertRaises(ValueError) as caught:
            report.resolve_agent_ids(self.AGENTS, "nope")
        self.assertIn("0own-build-agent", str(caught.exception))


def make_runs(pilot_after_factor, control_after_factor, n=4, repo="o/r", wf="go"):
    """`n` runs per cell; base 100 s before, scaled after."""
    runs = []
    for i in range(n):
        runs.append(run(repo, wf, 99, CUTOVER - DAY + i * 10, 100))  # pilot before
        runs.append(run(repo, wf, 99, CUTOVER + DAY + i * 10, int(100 * pilot_after_factor)))
        runs.append(run(repo, wf, 1, CUTOVER - DAY + i * 10, 100))  # control before
        runs.append(run(repo, wf, 1, CUTOVER + DAY + i * 10, int(100 * control_after_factor)))
    return runs


class CompareTests(unittest.TestCase):
    def test_pilot_faster_than_controls_gives_effect_below_one(self):
        summary = report.compare(make_runs(0.5, 1.0), {99}, CUTOVER, 7 * DAY)
        self.assertEqual(summary["pairs_compared"], 1)
        self.assertEqual(summary["pilot_after_over_before"], 0.5)
        self.assertEqual(summary["control_after_over_before"], 1.0)
        self.assertEqual(summary["effect"], 0.5)

    def test_fleet_wide_slowdown_is_cancelled_out(self):
        # everything got 20 % slower (load), the pilot by the same amount: no effect
        summary = report.compare(make_runs(1.2, 1.2), {99}, CUTOVER, 7 * DAY)
        self.assertEqual(summary["effect"], 1.0)

    def test_too_few_runs_in_a_cell_is_not_compared(self):
        summary = report.compare(make_runs(0.5, 1.0, n=2), {99}, CUTOVER, 7 * DAY, min_runs=3)
        self.assertEqual(summary["pairs_compared"], 0)
        self.assertNotIn("effect", summary)
        self.assertIn("note", summary)

    def test_runs_outside_the_window_are_ignored(self):
        old = [run("o/r", "go", 99, CUTOVER - 30 * DAY, 1) for _ in range(5)]
        summary = report.compare(make_runs(0.5, 1.0) + old, {99}, CUTOVER, 7 * DAY)
        self.assertEqual(summary["unpaired"]["pilot_before"]["runs"], 4)

    def test_median_of_several_repositories(self):
        runs = make_runs(0.5, 1.0, repo="o/a") + make_runs(0.7, 1.0, repo="o/b") + make_runs(0.9, 1.0, repo="o/c")
        summary = report.compare(runs, {99}, CUTOVER, 7 * DAY)
        self.assertEqual(summary["pairs_compared"], 3)
        self.assertEqual(summary["effect"], 0.7)

    def test_no_pilot_runs_at_all(self):
        runs = [r for r in make_runs(0.5, 1.0) if r.agent_id != 99]
        summary = report.compare(runs, {99}, CUTOVER, 7 * DAY)
        self.assertEqual(summary["pairs_compared"], 0)
        self.assertEqual(summary["unpaired"]["pilot_after"]["runs"], 0)


class RepoNameTests(unittest.TestCase):
    def test_accepts_owner_name_and_skips_comments(self):
        self.assertEqual(
            report.valid_repo_names(["# sample", "", "baditaflorin/go_trust_decay", " o/r.v2 "]),
            ["baditaflorin/go_trust_decay", "o/r.v2"],
        )

    def test_rejects_anything_that_could_alter_the_url_path(self):
        for bad in ("../admin", "o/r/extra", "o/../r", "o/r?x=1", "o/r#f", "o r/x", "/x", "o/"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                report.valid_repo_names([bad])


class CliTests(unittest.TestCase):
    def test_help_exits_cleanly(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            report.parse_args(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_required_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            report.parse_args(["--plane", "ci.0exec.com"])


if __name__ == "__main__":
    unittest.main()
