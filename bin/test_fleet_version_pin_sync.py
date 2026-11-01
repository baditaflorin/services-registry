import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "fleet-version-pin-sync.py"
_spec = importlib.util.spec_from_file_location("fleet_version_pin_sync", SCRIPT)
vps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vps)


class TestParse(unittest.TestCase):
    def test_version_bodies(self):
        self.assertEqual(vps.parse_version_body('{"version":"1.2.3"}'), "1.2.3")
        self.assertEqual(vps.parse_version_body('{"data":{"version":"v0.7.5"}}'), "0.7.5")
        self.assertIsNone(vps.parse_version_body("<!doctype html>"))
        self.assertIsNone(vps.parse_version_body('{"version":""}'))
        self.assertIsNone(vps.parse_version_body('["1.2.3"]'))

    def test_semver_and_repo(self):
        self.assertLess(vps.semver("1.9.16"), vps.semver("1.9.18"))
        self.assertLess(vps.semver("0.4.7"), vps.semver("0.7.5"))
        self.assertIsNone(vps.semver("latest"))
        self.assertEqual(vps.repo_name("https://github.com/baditaflorin/go_apikey_service.git"), "go_apikey_service")
        self.assertIsNone(vps.repo_name("https://github.com/plausible/community-edition"))
        self.assertIsNone(vps.repo_name("https://github.com/baditaflorin/go_x/blob/main/mesh.go"))

    def test_candidates_skip_external_sunset_and_foreign(self):
        rows = vps.candidates([
            {"id": "a", "kind": "container", "runtime": "compose", "repo_url": "https://github.com/baditaflorin/go_a"},
            {"id": "ext", "kind": "container", "runtime": "external", "repo_url": "https://github.com/baditaflorin/go_a"},
            {"id": "old", "kind": "container", "runtime": "compose", "lifecycle": {"status": "sunset"},
             "repo_url": "https://github.com/baditaflorin/go_old"},
            {"id": "pages", "kind": "static", "repo_url": "https://github.com/baditaflorin/site"},
            {"id": "up", "kind": "container", "runtime": "compose", "repo_url": "https://github.com/other/x"},
        ])
        self.assertEqual([r["id"] for r in rows], ["a"])


class TestDecide(unittest.TestCase):
    """The rule: change a pin only to a version that is both merged (repository main) and running, never lower it,
    and never raise an existing pin to a version ghcr.io lacks."""

    def d(self, **row):
        return vps.decide(row)[0]

    def test_table(self):
        self.assertEqual(self.d(pin="0.4.7", main="0.7.5", live="0.7.5", image=True), "raise")
        self.assertEqual(self.d(pin="0.4.7", main="0.7.5", live="0.7.5", image=False), "skip")  # would turn image red
        self.assertEqual(self.d(pin="0.4.7", main="0.7.5", live="0.7.5", image=None), "skip")
        self.assertEqual(self.d(pin=None, main="0.1.1", live="0.1.1", image=False), "add")      # missing is worse
        self.assertEqual(self.d(pin="0.7.5", main="0.7.5", live="0.7.5"), "ok")
        self.assertEqual(self.d(pin="1.1.5", main="1.1.6", live="1.2.5", image=True), "skip")   # composite runner
        self.assertEqual(self.d(pin="0.1.12", main="0.1.14", live="0.1.12", image=True), "ok")  # undeployed release
        self.assertEqual(self.d(pin="0.1.0", main="0.1.14", live="0.1.12", image=True), "skip")  # main ahead of live
        self.assertEqual(self.d(pin="2.7.1", main="2.5.7", live="2.5.7", image=True), "skip")   # never lowered
        self.assertEqual(self.d(pin="0.1.0", main=None, live="0.1.0"), "skip")
        self.assertEqual(self.d(pin="0.1.0", main="0.2.0", live=None), "skip")
        self.assertEqual(self.d(pin="latest", main="0.2.0", live="0.2.0", image=True), "skip")


OVERRIDES = """{
  "$rules": [],
  "a": {
    "version": "1.0.0",
    "trl": 4
  },
  "b": {
    "trl": 2
  },
  "inline": {
    "version": "0.1.0",
    "spiffe_workload": true  },
  "c": {"version": "3.0.0"}
}
"""


class TestSetPins(unittest.TestCase):
    def test_replace_insert_and_inline_close(self):
        out = vps.set_pins(OVERRIDES, {"a": "1.0.1", "b": "2.0.0", "inline": "0.2.0"})
        doc = json.loads(out)
        self.assertEqual((doc["a"]["version"], doc["b"]["version"], doc["inline"]["version"]), ("1.0.1", "2.0.0", "0.2.0"))
        self.assertEqual(doc["c"], {"version": "3.0.0"})
        self.assertIn('"spiffe_workload": true  },', out)  # the inline close is kept byte for byte
        self.assertEqual(len(out.splitlines()), len(OVERRIDES.splitlines()) + 1)  # only b gained a line

    def test_refuses_unknown_and_unexpected_layout(self):
        with self.assertRaisesRegex(ValueError, "expected one top-level block"):
            vps.set_pins(OVERRIDES, {"missing": "1.0.0"})
        with self.assertRaisesRegex(ValueError, "layout"):
            vps.set_pins('{\n  "c": {"trl": 1}\n}\n', {"c": "1.0.0"})


class TestVersionOnlyChange(unittest.TestCase):
    def test_detects_anything_but_target_versions(self):
        old = [{"id": "a", "version": "1"}, {"id": "b", "env": {"X": "1"}}]
        self.assertEqual(vps.version_only_change(old, [{"id": "a", "version": "2"}, {"id": "b", "env": {"X": "1"}}],
                                                 {"a": "2"}), [])
        self.assertTrue(vps.version_only_change(old, [{"id": "a", "version": "2"}, {"id": "b"}], {"a": "2"}))
        self.assertTrue(vps.version_only_change(old, [{"id": "a", "version": "2", "trl": 3}, old[1]], {"a": "2"}))
        self.assertTrue(vps.version_only_change(old, old, {"a": "2"}))  # a target that did not change


class TestChangelog(unittest.TestCase):
    def test_today_heading_reused_or_created(self):
        self.assertEqual(vps.changelog_with("## 2026-10-17\n\n- x\n", "- y\n", "2026-10-17"), "## 2026-10-17\n\n- y\n- x\n")
        self.assertEqual(vps.changelog_with("## 2026-10-16\n\n- x\n", "- y\n", "2026-10-17"),
                         "## 2026-10-17\n\n- y\n\n## 2026-10-16\n\n- x\n")


class TestStaleTargets(unittest.TestCase):
    def test_only_a_changed_row_of_a_target_is_skippable(self):
        targets = {"a": "2", "b": "3", "c": "4"}
        self.assertEqual(vps.stale_targets(["a: changed ['trl_evidence', 'version']"], targets), {"a"})
        self.assertEqual(vps.stale_targets(["a: changed ['trl_evidence', 'version']", "c: changed ['env', 'version']"], targets), {"a", "c"})
        self.assertIsNone(vps.stale_targets([], targets))
        self.assertIsNone(vps.stale_targets(["the set of services changed"], targets))
        self.assertIsNone(vps.stale_targets(["a: row did not change"], targets))
        self.assertIsNone(vps.stale_targets(["zzz: changed ['trl_evidence']"], targets))                      # not a target: something else moved
        self.assertIsNone(vps.stale_targets(["a: changed ['trl_evidence', 'version']", "the set of services changed"], targets))
        self.assertIsNone(vps.stale_targets(["a: changed ['version']"], targets))                              # version alone is the pin itself: wrong value, not stale


class TestWaitAndMergeAndResume(unittest.TestCase):
    OK = json.dumps({"statuses": [{"context": c, "state": "success"} for c in vps.CI_CONTEXTS]})

    def _run(self, answers, merged):
        """A fake `run`: gh api answers come from `answers` in order (an Exception is raised), `gh pr merge` is recorded."""
        def fake(cmd, cwd=None, check=True, **kw):
            if cmd[:2] == ["gh", "api"]:
                a = answers.pop(0)
                if isinstance(a, Exception):
                    raise a
                return mock.Mock(stdout=a, returncode=0)
            if cmd[:3] == ["gh", "pr", "merge"]:
                merged.append(cmd)
                return mock.Mock(stdout="", returncode=0)
            raise AssertionError(cmd)
        return fake

    def test_a_refused_status_call_is_retried_not_fatal(self):
        merged, sleeps = [], []
        answers = [RuntimeError("gh api ...: exit 1: gh: API rate limit exceeded"), RuntimeError("again"), self.OK]
        with mock.patch.object(vps, "run", self._run(answers, merged)), mock.patch.object(vps.time, "sleep", sleeps.append):
            vps.wait_and_merge("/w", 7, "abc", 3600)
        self.assertEqual(len(merged), 1)
        self.assertIn("--match-head-commit", merged[0])
        self.assertEqual(sleeps, [120, 120])

    def test_failed_ci_still_stops_and_a_deadline_still_ends_the_wait(self):
        bad = json.dumps({"statuses": [{"context": vps.CI_CONTEXTS[0], "state": "failure"}]})
        with mock.patch.object(vps, "run", self._run([bad], [])):
            with self.assertRaises(RuntimeError):
                vps.wait_and_merge("/w", 7, "abc", 3600)
        with mock.patch.object(vps, "run", self._run([RuntimeError("down")] * 5, [])), mock.patch.object(vps.time, "sleep", lambda s: None):
            with self.assertRaises(RuntimeError):
                vps.wait_and_merge("/w", 7, "abc", 0)

    def test_an_open_pull_request_is_resumed_and_exit_3_means_it_could_not_be_merged(self):
        args = mock.Mock(ci_timeout=60, prom="")
        pr = {"number": 9, "headRefName": "bot/version-pin-sync-x", "headRefOid": "deadbeef"}
        with mock.patch.object(vps, "wait_and_merge") as wm:
            self.assertEqual(vps.resume_open_pr("/w", pr, {}, args), 0)
            wm.assert_called_once_with("/w", 9, "deadbeef", 60)
        with mock.patch.object(vps, "wait_and_merge", side_effect=RuntimeError("CI failed on #9")):
            self.assertEqual(vps.resume_open_pr("/w", pr, {}, args), 3)


class TestFreshCheckout(unittest.TestCase):
    """2026-10-20 18:21Z the job stopped at its own guard with edited overrides.json/services.json left in its work dir; from then on `checkout -B main origin/main`
    refused to run over them and every run (18:21, 00:18, 06:18) failed there, so no pin was synced for 12 hours."""

    def test_a_work_dir_left_dirty_by_a_failed_run_is_cleaned_and_follows_origin(self):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

        def g(*args, cwd):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            tmp = Path(tmp)
            origin, seed, work = tmp / "origin.git", tmp / "seed", tmp / "work"
            g("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp)
            g("clone", "-q", str(origin), str(seed), cwd=tmp)
            (seed / "overrides.json").write_text('{"a": {"version": "1"}}\n')
            (seed / "services.json").write_text('[{"id": "a", "version": "1"}]\n')
            g("add", "-A", cwd=seed)
            g("commit", "-q", "-m", "one", cwd=seed)
            g("push", "-q", "origin", "HEAD:main", cwd=seed)
            with mock.patch.object(vps, "REPO_URL", str(origin)):
                vps.fresh_checkout(work)                                          # the first run clones
                # a run that died half-way: edited tracked files, an untracked file, even its own branch with a local commit
                g("checkout", "-q", "-b", "bot/version-pin-sync-x", cwd=work)
                (work / "overrides.json").write_text('{"a": {"version": "2"}}\n')
                (work / "services.json").write_text("changed\n")
                (work / "leftover.tmp").write_text("x\n")
                g("commit", "-q", "-am", "half-way", cwd=work)
                (work / "overrides.json").write_text("dirty again\n")
                (seed / "overrides.json").write_text('{"a": {"version": "3"}}\n')   # origin moved on meanwhile
                g("commit", "-q", "-am", "two", cwd=seed)
                g("push", "-q", "origin", "HEAD:main", cwd=seed)
                vps.fresh_checkout(work)                                          # must not raise
            self.assertEqual((work / "overrides.json").read_text(), '{"a": {"version": "3"}}\n')
            self.assertEqual((work / "services.json").read_text(), '[{"id": "a", "version": "1"}]\n')
            self.assertFalse((work / "leftover.tmp").exists())
            head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=work, capture_output=True, text=True, env=env).stdout.strip()
            self.assertEqual(head, "main")


if __name__ == "__main__":
    unittest.main()
