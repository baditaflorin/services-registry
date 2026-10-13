#!/usr/bin/env python3
"""Hermetic tests for the pure parts of rollout_claude_md.py (canonical history, three-way merge). Cloning and pushing need GitHub and are not tested here.
Run: python3 -m unittest discover -s <dir containing rollout_claude_md.py> -p 'test_rollout_claude_md.py'"""
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
