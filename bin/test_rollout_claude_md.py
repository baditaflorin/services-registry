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

    def test_replace_safe_only_for_a_copy_with_no_lines_of_its_own(self):
        lacking = V3.replace("run cleanup\n", "")  # canonical minus one line, nothing added: own == 0 against V3
        texts = R.history(self.reg)
        rev, behind, own = R.closest_rev(texts, lacking)
        self.assertEqual(own, 0)
        text, restored = R.replace_safe(lacking, V3, own)
        self.assertEqual(text, V3)
        self.assertEqual([l.strip() for l in restored], ["run cleanup"])
        self.assertIsNone(R.replace_safe(V3, V3, 0), "an identical copy needs nothing")
        self.assertIsNone(R.replace_safe(V3 + "an own line\n", V3, 1), "a copy with its own line is a real merge, never replaced")

    def test_a_three_way_merge_cannot_restore_the_missing_line_but_replace_safe_does(self):
        lacking = V3.replace("run cleanup\n", "")
        text, conflicts, over = R.merge_prefer_canonical(self.reg, self.revs[2], lacking, V3)
        self.assertEqual(text, lacking, "the merge keeps the missing line as a local deletion: the reason this mode exists")

    def test_cmd_merge_with_replace_safe_pushes_the_canonical_to_a_copy_that_lacked_a_line(self):
        out = os.path.join(self.tmp.name, "work"); os.makedirs(os.path.join(out, "clones"))
        bare = os.path.join(self.tmp.name, "origin.git")
        def g(cwd, *a):
            r = subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, text=True, env=ENV); assert r.returncode == 0, r.stderr; return r.stdout.strip()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        clone = os.path.join(out, "clones", "go_lacking")
        subprocess.run(["git", "clone", "-q", bare, clone], check=True, capture_output=True)
        with open(os.path.join(clone, "CLAUDE.md"), "w") as f:
            f.write(V3.replace("run cleanup\n", ""))
        g(clone, "add", "-A"); g(clone, "commit", "-q", "-m", "x"); g(clone, "push", "-q", "origin", "HEAD:main")
        with open(os.path.join(out, "CLAUDE.md.canonical"), "w") as f:
            f.write(V3)
        with open(os.path.join(out, "plan.json"), "w") as f:
            json.dump({"canonical_head": "abc123def", "rows": [{"repo": "go_lacking", "status": "diverged"}]}, f)
        ns = type("A", (), dict(registry=self.reg, out=out, only=None, push=True, prefer_canonical=True, replace_safe=True, workers=1))()
        res = R.cmd_merge(ns)
        self.assertEqual(res[0]["result"], "pushed")
        self.assertEqual(res[0]["replaced_safe"], 1)
        g(clone, "fetch", "-q", "origin")
        self.assertEqual(g(clone, "show", "origin/main:CLAUDE.md") + "\n", V3)
        self.assertIn("restore the 1 line(s)", g(clone, "log", "-1", "--format=%B", "origin/main"))

    def test_repo_names_come_from_services_json_and_the_extra_file(self):
        with open(os.path.join(self.reg, "services.json"), "w") as f:
            f.write(json.dumps({"services": [{"repo_url": "https://github.com/baditaflorin/go_a"}, {"repo_url": "https://github.com/baditaflorin/go_b.git"}, {"repo_url": "https://example.com/x/y"}, {}]}))
        self.git("add", "-A"); self.git("commit", "-q", "-m", "services")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        extra = os.path.join(self.tmp.name, "extra.txt")
        with open(extra, "w") as f:
            f.write("# infra\nfleet-state\n\ngo_a\n")
        self.assertEqual(R.repo_names(self.reg, extra), ["fleet-state", "go_a", "go_b"])

    def test_refresh_clone_works_on_the_remote_default_branch_whatever_the_cache_has_checked_out(self):
        r = tempfile.mkdtemp()
        origin = os.path.join(r, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        seed = os.path.join(r, "seed")
        subprocess.run(["git", "clone", "-q", origin, seed], check=True, capture_output=True)
        g = lambda d, *a: subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), capture_output=True, text=True, check=True).stdout.strip()
        with open(os.path.join(seed, "CLAUDE.md"), "w") as f:
            f.write("one\n")
        g(seed, "add", "CLAUDE.md")
        g(seed, "commit", "-q", "-m", "one")
        g(seed, "push", "-q", "origin", "HEAD:main")
        cache = os.path.join(r, "clones", "repo")
        os.makedirs(os.path.dirname(cache))
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + origin, cache], check=True, capture_output=True)
        g(cache, "checkout", "-q", "-b", "leftover-branch")  # what an earlier session left checked out in the cache
        with open(os.path.join(seed, "CLAUDE.md"), "w") as f:
            f.write("two\n")
        g(seed, "commit", "-qam", "two")
        g(seed, "push", "-q", "origin", "HEAD:main")
        d, err = R.refresh_clone("repo", r)
        self.assertIsNone(err)
        self.assertEqual(g(d, "rev-parse", "--abbrev-ref", "HEAD"), "main")  # the push of HEAD would otherwise create a stray remote branch
        with open(os.path.join(d, "CLAUDE.md")) as f:
            self.assertEqual(f.read(), "two\n")

    def test_drop_skip_ci_only_touches_the_commit_that_goes_to_the_sync_branch(self):
        d = tempfile.mkdtemp()
        sh = lambda *a: subprocess.run(["git", "-C", d] + list(a), capture_output=True, text=True, check=True).stdout.strip()
        sh("init", "-q", "-b", "main")
        open(os.path.join(d, "CLAUDE.md"), "w").write("x\n")
        sh("add", "CLAUDE.md")
        subprocess.run(["git", "-C", d] + R.ID + ["commit", "-q", "-m", "docs(claude): refresh CLAUDE.md from the canonical abc [skip ci]", "-m", "body line one\n\nCo-Authored-By: someone"], check=True, capture_output=True)
        R.drop_skip_ci(d)
        msg = sh("log", "-1", "--format=%B")
        self.assertNotIn("[skip ci]", msg)
        self.assertTrue(msg.startswith("docs(claude): refresh CLAUDE.md from the canonical abc"))
        self.assertIn("body line one", msg)
        self.assertIn("Co-Authored-By: someone", msg)
        before = sh("rev-parse", "HEAD")
        R.drop_skip_ci(d)  # nothing to drop: the commit is left alone
        self.assertEqual(before, sh("rev-parse", "HEAD"))

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
