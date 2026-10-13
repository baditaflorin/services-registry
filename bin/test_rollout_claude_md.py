#!/usr/bin/env python3
"""Hermetic tests for the pure parts of rollout_claude_md.py (canonical history, three-way merge). Cloning and pushing need GitHub and are not tested here.
Run: python3 -m unittest discover -s <dir containing rollout_claude_md.py> -p 'test_rollout_claude_md.py'"""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("rollout_claude_md", os.path.join(HERE, "rollout_claude_md.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
V1 = "# Fleet\n\n## Auth\nold auth wording\n\n## Notes\nnote line\n\n## Deploy\nrun deploy\n\n## Tail\nlast line\n"
V2 = V1.replace("old auth wording", "new auth wording")
V3 = V2.replace("run deploy\n", "run deploy\nrun cleanup\n")


class RolloutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = os.path.join(self.tmp.name, "reg")
        os.makedirs(self.reg)
        self.git("init", "-q", "-b", "main")
        self.revs = []
        for text in (V1, V2, V3):
            with open(os.path.join(self.reg, "CLAUDE.md"), "w") as f:
                f.write(text)
            self.git("add", "-A"); self.git("commit", "-q", "-m", "rev")
            self.revs.append(self.git("rev-parse", "HEAD"))
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")  # canonical() reads origin/main

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        r = subprocess.run(["git", "-C", self.reg] + list(a), capture_output=True, text=True, env=ENV)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_canonical_counts_how_many_revisions_behind_each_blob_is(self):
        text, blob, blobs, head = R.canonical(self.reg)
        self.assertEqual(text, V3)
        self.assertEqual(blobs[blob][0], 0)
        old = self.git("rev-parse", self.revs[0] + ":CLAUDE.md")
        self.assertEqual(blobs[old][0], 2)

    def test_merge_keeps_a_local_edit_and_takes_the_canonical_change(self):
        local = V1.replace("last line", "last line --local-flag")  # edited a line the canonical did not touch
        merged, conflicts = R.merge_text(self.reg, self.revs[0], local, V3)
        self.assertEqual(conflicts, 0)
        self.assertIn("--local-flag", merged)
        self.assertIn("new auth wording", merged)
        self.assertIn("run cleanup", merged)

    def test_merge_reports_a_conflict_when_both_sides_changed_the_same_lines(self):
        local = V1.replace("old auth wording", "service specific auth wording")
        merged, conflicts = R.merge_text(self.reg, self.revs[0], local, V3)
        self.assertEqual(conflicts, 1)
        self.assertIn("<<<<<<<", merged)

    def test_prefer_canonical_keeps_both_inserts_and_lets_the_canonical_win_changes(self):
        # both sides changed the Auth line -> canonical wins and the local wording is reported; both inserted after "Tail" -> both kept
        local = V1.replace("old auth wording", "service specific auth wording") + "local tail section\n"
        theirs = V3 + "canonical tail section\n"
        text, conflicts, over = R.merge_prefer_canonical(self.reg, self.revs[0], local, theirs)
        self.assertGreaterEqual(conflicts, 1)
        self.assertIn("new auth wording", text)
        self.assertNotIn("service specific auth wording", text)
        self.assertTrue(any("service specific auth wording" in o for o in over))
        self.assertIn("local tail section", text)
        self.assertIn("canonical tail section", text)
        self.assertNotIn("<<<<<<<", text)

    def test_prefer_canonical_is_a_no_op_when_the_copy_equals_the_base_plus_nothing(self):
        text, conflicts, over = R.merge_prefer_canonical(self.reg, self.revs[0], V1, V3)
        self.assertEqual((text, conflicts, over), (V3, 0, []))

    def test_closest_rev_finds_the_revision_a_copy_was_made_from(self):
        texts = R.history(self.reg)
        rev, behind, own = R.closest_rev(texts, V2 + "an own line\n")
        self.assertEqual((rev, behind, own), (self.revs[1], 1, 1))

    def test_repo_names_come_from_services_json_and_the_extra_file(self):
        with open(os.path.join(self.reg, "services.json"), "w") as f:
            f.write(json.dumps({"services": [{"repo_url": "https://github.com/baditaflorin/go_a"}, {"repo_url": "https://github.com/baditaflorin/go_b.git"}, {"repo_url": "https://example.com/x/y"}, {}]}))
        self.git("add", "-A"); self.git("commit", "-q", "-m", "services")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        extra = os.path.join(self.tmp.name, "extra.txt")
        with open(extra, "w") as f:
            f.write("# infra\nfleet-state\n\ngo_a\n")
        self.assertEqual(R.repo_names(self.reg, extra), ["fleet-state", "go_a", "go_b"])

    def test_refresh_clone_survives_a_stale_index_lock_and_moves_to_the_remote_tip(self):
        remote = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
        out = os.path.join(self.tmp.name, "work")
        d = os.path.join(out, "clones", "svc")
        subprocess.run(["git", "clone", "-q", remote, d], check=True, env=ENV)
        for name, text in (("a", "one\n"), ("b", "two\n")):
            with open(os.path.join(d, "CLAUDE.md"), "w") as f:
                f.write(text)
            subprocess.run(["git", "-C", d, "add", "-A"], check=True)
            subprocess.run(["git", "-C", d, "commit", "-q", "-m", name], check=True, env=ENV)
        subprocess.run(["git", "-C", d, "push", "-q", "-u", "origin", "main"], check=True, env=ENV)
        subprocess.run(["git", "-C", d, "reset", "-q", "--hard", "HEAD~1"], check=True)  # the clone is behind the remote
        with open(os.path.join(d, ".git", "index.lock"), "w") as f:
            f.write("")  # what an interrupted run leaves behind
        got, err = R.refresh_clone("svc", out)
        self.assertEqual((got, err), (d, None))
        with open(os.path.join(d, "CLAUDE.md")) as f:
            self.assertEqual(f.read(), "two\n")
        self.assertFalse(os.path.exists(os.path.join(d, ".git", "index.lock")))


if __name__ == "__main__":
    unittest.main()
