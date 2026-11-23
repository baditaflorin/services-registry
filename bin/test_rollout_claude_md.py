#!/usr/bin/env python3
"""Hermetic tests for the pure parts of rollout_claude_md.py (canonical history, three-way merge). Cloning and pushing need GitHub and are not tested here.
Run: python3 -m unittest discover -s <dir containing rollout_claude_md.py> -p 'test_rollout_claude_md.py'"""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "rollout_claude_md.py")
spec = importlib.util.spec_from_file_location("rollout_claude_md", SCRIPT)
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


# --- excludes.json, and the refusal to bury a short brief -----------------------------------------------------------------------------------------------------------
# These drive the real entry points (cmd_plan, cmd_merge, cmd_weekly, the command line) against local bare repositories that stand in for GitHub. `plan` reuses a clone that
# already exists in <work>/clones, so nothing is fetched; GIT_SSH_COMMAND=false makes any attempt to clone a repository that has no such clone fail at once.

def fleet(generation, extra=""):
    """a canonical revision: every revision rewrites every body line of the one before (as the real canonical's 84 revisions did), and ends in lines that never change"""
    return "# Fleet\n\n" + "".join("## Section %d\n%s line %d\n\n" % (i, generation, i) for i in range(40)) + "## Last\nlast line\n" + extra


FLEET1 = fleet("first")
FLEET2 = fleet("second")
FLEET3 = fleet("third", "\n## Newest\nnewest line\n")
BRIEF = "# Product brief\n\nThis repository is a product, not a fleet service.\n\n## Run\nmake run\n"
TAIL = "\n## Local tail\nrepo notes\n"
SUBSET = "".join(FLEET3.splitlines(True)[:8])  # only lines the canonical has, so nothing of its own, and ten times smaller


def content_lines(text):
    return len([l for l in text.split("\n") if l.strip()])


def excludes(*names):
    return json.dumps({"_doc": "test", "_format_version": 1, "excludes": [{"repo": n, "reason": "test reason for " + n} for n in names]})


def gitc(cwd, *a):
    r = subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@example.invalid"] + list(a), capture_output=True, text=True)
    assert r.returncode == 0, "git %s: %s" % (" ".join(a), r.stderr)
    return r.stdout.strip()


class FleetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        no_network = mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "false"})
        no_network.start()
        self.addCleanup(no_network.stop)
        self.work = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work)
        self.bare, self.seed = {}, {}

    @staticmethod
    def put(d, name, text):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)

    def make_registry(self, names, excludes_text, tag=""):
        """a registry clone whose origin/main has three canonical revisions, services.json (repo_url of every name) and excludes.json (None = no such file)"""
        bare = os.path.join(self.tmp.name, "registry%s.git" % tag)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        seed = os.path.join(self.tmp.name, "registry-seed%s" % tag)
        subprocess.run(["git", "clone", "-q", bare, seed], check=True, capture_output=True)
        for text in (FLEET1, FLEET2, FLEET3):
            self.put(seed, "CLAUDE.md", text)
            gitc(seed, "add", "-A")
            gitc(seed, "commit", "-q", "-m", "rev")
        self.put(seed, "services.json", json.dumps({"services": [{"repo_url": "https://github.com/baditaflorin/" + n} for n in names]}))
        if excludes_text is not None:
            self.put(seed, "excludes.json", excludes_text)
        gitc(seed, "add", "-A")
        gitc(seed, "commit", "-q", "-m", "data")
        gitc(seed, "push", "-q", "origin", "HEAD:main")
        reg = os.path.join(self.tmp.name, "registry%s" % tag)
        subprocess.run(["git", "clone", "-q", bare, reg], check=True, capture_output=True)
        return reg

    def make_remote(self, name, text):
        """a bare repository standing in for GitHub, with a shallow clone of it in <work>/clones/<name> as `plan` leaves them"""
        bare = os.path.join(self.tmp.name, "remotes", name + ".git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        seed = os.path.join(self.tmp.name, "remotes", name + "-seed")
        subprocess.run(["git", "clone", "-q", bare, seed], check=True, capture_output=True)
        self.put(seed, "README.md", "readme\n")
        self.put(seed, "CLAUDE.md", text)
        gitc(seed, "add", "-A")
        gitc(seed, "commit", "-q", "-m", "init")
        gitc(seed, "push", "-q", "origin", "HEAD:main")
        os.makedirs(os.path.join(self.work, "clones"), exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + bare, os.path.join(self.work, "clones", name)], check=True, capture_output=True)
        self.bare[name], self.seed[name] = bare, seed

    def tip(self, name):
        return gitc(self.bare[name], "rev-parse", "main")

    def text(self, name):
        return gitc(self.bare[name], "show", "main:CLAUDE.md") + "\n"

    def branches(self, name):
        return gitc(self.bare[name], "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()

    def plan(self, reg, repos):
        with contextlib.redirect_stdout(io.StringIO()):
            R.cmd_plan(argparse.Namespace(registry=reg, out=self.work, repos=repos, repos_file=None, workers=1))
        with open(os.path.join(self.work, "plan.json")) as f:
            return {r["repo"]: r for r in json.load(f)["rows"]}

    def weekly(self, reg, extra=None):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = R.cmd_weekly(argparse.Namespace(registry=reg, work=self.work, extra=extra, workers=1, apply_workers=1))
        with open(os.path.join(self.work, "reports", "latest.json")) as f:
            return rc, json.load(f)


class ExcludesTest(FleetCase):
    def test_weekly_skips_what_excludes_json_lists_and_still_rolls_out_to_the_rest(self):
        # listed: a diverged short brief, a copy that is merely behind (an archived repository), and one that only the extra list names and that has no remote at all
        reg = self.make_registry(["go_brief", "go_old", "go_tail", "go_plain"], excludes("go_brief", "Go_Old", "go_extra_never_cloned", "go_listed_nowhere"))
        self.make_remote("go_brief", BRIEF)
        self.make_remote("go_old", FLEET1)
        self.make_remote("go_tail", FLEET2 + TAIL)  # diverged and merges cleanly: not listed, so still merged
        self.make_remote("go_plain", FLEET1)  # behind: not listed, so still refreshed
        extra = os.path.join(self.tmp.name, "extra.txt")
        with open(extra, "w") as f:
            f.write("# infra\ngo_extra_never_cloned\n")
        before = {n: self.tip(n) for n in self.bare}

        rc, rep = self.weekly(reg, extra)

        self.assertEqual(rc, 0, rep["problems"])
        self.assertEqual(rep["excluded"], ["go_brief", "go_extra_never_cloned", "go_old"])
        self.assertEqual(rep["classified_before"], {"excluded": 3, "diverged": 1, "behind": 1})
        self.assertEqual(rep["results"], {"pushed": 2})
        self.assertEqual((rep["problems"], rep["not_reachable"]), ([], []))
        for n in ("go_brief", "go_old"):  # neither merged nor pushed, whatever their copy looks like
            self.assertEqual(self.tip(n), before[n], n)
        self.assertEqual(self.text("go_brief"), BRIEF)
        self.assertFalse(os.path.exists(os.path.join(self.work, "clones", "go_extra_never_cloned")), "a listed repository is not even cloned")
        # the repositories that are not listed are served as before
        self.assertNotEqual(self.tip("go_tail"), before["go_tail"])
        self.assertIn("newest line", self.text("go_tail"))
        self.assertIn("repo notes", self.text("go_tail"))
        self.assertEqual(self.text("go_plain"), FLEET3)
        # the logs the weekly report is checked against hold no row for a listed repository
        rows = []
        for log in ("apply_log.jsonl", "merge_log.jsonl"):
            with open(os.path.join(self.work, log)) as f:
                rows += [json.loads(l) for l in f if l.strip()]
        self.assertEqual(sorted(x["repo"] for x in rows), ["go_plain", "go_tail"])

    def test_a_plan_made_by_hand_skips_a_listed_repository_without_cloning_it(self):
        reg = self.make_registry([], excludes("go_listed"))
        self.make_remote("go_plain", FLEET1)
        rows = self.plan(reg, "go_listed,go_plain")
        self.assertEqual(rows["go_listed"]["status"], "excluded")
        self.assertIn("test reason for go_listed", rows["go_listed"]["reason"])
        self.assertEqual(rows["go_plain"]["status"], "behind")
        self.assertFalse(os.path.exists(os.path.join(self.work, "clones", "go_listed")))

    def test_a_weekly_run_in_which_everything_is_excluded_still_writes_its_report(self):
        reg = self.make_registry(["go_brief"], excludes("go_brief"))
        self.make_remote("go_brief", BRIEF)  # nothing to apply: apply writes no log on a fresh work directory
        rc, rep = self.weekly(reg)
        self.assertEqual((rc, rep["excluded"], rep["results"]), (0, ["go_brief"], {}))

    def test_a_list_that_cannot_be_used_stops_the_plan_before_anything_is_cloned(self):
        for tag, text in (("missing", None), ("broken", "{oops"), ("shape", '{"excludes": ["go_a"]}')):
            with self.subTest(tag):
                reg = self.make_registry(["go_a"], text, tag=tag)
                out = os.path.join(self.tmp.name, "out-" + tag)
                with self.assertRaises(R.ExcludesUnusable):
                    R.cmd_plan(argparse.Namespace(registry=reg, out=out, repos="go_a", repos_file=None, workers=1))
                self.assertFalse(os.path.exists(os.path.join(out, "clones")))

    def test_the_command_line_stops_with_a_message_when_the_list_cannot_be_read(self):
        reg = self.make_registry(["go_a"], "{oops")
        p = subprocess.run([sys.executable, SCRIPT, "weekly", "--registry", reg, "--work", self.work], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("excludes.json", p.stderr)
        self.assertIn("stopped before anything was cloned or pushed", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.work, "plan.json")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "clones")))

    def test_the_names_are_lower_cased_and_carry_their_reason(self):
        got = R.parse_excludes(json.dumps({"excludes": [{"repo": " Go_Foo ", "reason": "r1"}, {"repo": "bar"}]}))
        self.assertEqual(got, {"go_foo": "r1", "bar": ""})
        self.assertEqual(R.parse_excludes('{"excludes": []}'), {}, "an empty list is a valid list: nothing is excluded")

    def test_anything_that_is_not_the_list_is_unusable(self):
        for bad in ("", "{oops", "[]", "{}", '{"excludes": null}', '{"excludes": {"a": 1}}', '{"excludes": ["go_a"]}', '{"excludes": [{"reason": "no repo"}]}',
                    '{"excludes": [{"repo": ""}]}', '{"excludes": [{"repo": 7}]}'):
            with self.subTest(bad=bad), self.assertRaises(R.ExcludesUnusable):
                R.parse_excludes(bad)

    def test_the_real_excludes_json_parses_and_still_lists_go_url_categorizer_api(self):
        # This file stops the weekly rollout when it cannot be parsed, so CI checks it here, before it can reach origin/main. The entry below is the repository whose short
        # CLAUDE.md the rollout merged the 141 KB canonical into on four Mondays (2026-11-02 .. 11-23): take it out only together with this assertion, on purpose.
        with open(os.path.join(HERE, "..", "excludes.json")) as f:
            names = R.parse_excludes(f.read())
        self.assertIn("go-url-categorizer-api", names)


class GrowthRefusalTest(unittest.TestCase):
    def test_a_short_copy_with_lines_of_its_own_is_refused_and_the_note_says_what_to_do(self):
        note = R.growth_refusal(BRIEF, FLEET3)
        self.assertIn("excludes.json", note)
        self.assertIn("--allow-growth", note)

    def test_a_copy_near_the_canonical_is_not(self):
        self.assertIsNone(R.growth_refusal(FLEET2 + TAIL, FLEET3 + TAIL))

    def test_a_copy_that_does_not_grow_is_never_refused(self):
        self.assertIsNone(R.growth_refusal(FLEET3, FLEET3))
        self.assertIsNone(R.growth_refusal(FLEET3, FLEET2))

    def test_the_limit_is_three_times(self):
        self.assertIsNone(R.growth_refusal("x" * 100, "y" * 299))
        self.assertIsNotNone(R.growth_refusal("x" * 100, "y" * 300))

    def test_an_empty_copy_is_refused_without_dividing_by_zero(self):
        self.assertIn("0 it has", R.growth_refusal("", "abc"))


class MergeRailTest(FleetCase):
    def setUp(self):
        super(MergeRailTest, self).setUp()
        self.reg = self.make_registry(["go_brief", "go_tail", "go_subset"], excludes())  # nothing is excluded: this is about the refusal
        self.make_remote("go_brief", BRIEF)
        self.make_remote("go_tail", FLEET2 + TAIL)
        self.make_remote("go_subset", SUBSET)
        self.plan(self.reg, "go_brief,go_tail,go_subset")

    def merge(self, **kw):
        ns = argparse.Namespace(registry=self.reg, out=self.work, only=None, push=True, prefer_canonical=True, replace_safe=True, allow_growth=False, workers=1)
        vars(ns).update(kw)
        with contextlib.redirect_stdout(io.StringIO()):
            return {x["repo"]: x for x in R.cmd_merge(ns)}

    def test_a_short_brief_is_refused_and_nothing_is_pushed_while_the_others_are_merged(self):
        before = self.tip("go_brief")
        res = self.merge()
        self.assertEqual(res["go_brief"]["result"], "refused-growth")
        self.assertIn("limit 3x", res["go_brief"]["note"])
        self.assertEqual(self.tip("go_brief"), before)
        self.assertEqual(self.branches("go_brief"), ["main"])
        self.assertEqual(self.text("go_brief"), BRIEF)
        self.assertEqual(res["go_tail"]["result"], "pushed")
        self.assertIn("newest line", self.text("go_tail"))

    def test_a_copy_with_no_lines_of_its_own_is_restored_however_small_it_is(self):
        res = self.merge()
        self.assertEqual(res["go_subset"]["result"], "pushed")
        self.assertEqual(res["go_subset"]["replaced_safe"], content_lines(FLEET3) - content_lines(SUBSET))
        self.assertEqual(self.text("go_subset"), FLEET3)

    def test_a_dry_run_reports_the_refusal_too(self):
        tips = {n: self.tip(n) for n in self.bare}
        res = self.merge(push=False)
        self.assertEqual(res["go_brief"]["result"], "refused-growth")
        self.assertEqual(res["go_tail"]["result"], "resolved-not-pushed")
        self.assertEqual({n: self.tip(n) for n in self.bare}, tips)

    def test_allow_growth_merges_a_brief_by_hand(self):
        res = self.merge(only="go_brief", allow_growth=True)
        self.assertEqual(res["go_brief"]["result"], "pushed")
        self.assertIn("newest line", self.text("go_brief"))
        self.assertTrue(gitc(self.bare["go_brief"], "log", "-1", "--format=%s", "main").startswith("docs(claude): merge the services-registry canonical"))

    def test_a_push_that_loses_a_race_is_redone_on_the_newer_tip(self):
        self.put(self.seed["go_tail"], "README.md", "newer\n")  # the repository moved on after plan: the first push is rejected
        gitc(self.seed["go_tail"], "add", "-A")
        gitc(self.seed["go_tail"], "commit", "-q", "-m", "newer")
        gitc(self.seed["go_tail"], "push", "-q", "origin", "HEAD:main")
        res = self.merge(only="go_tail")
        self.assertEqual(res["go_tail"]["result"], "pushed")
        self.assertIn("newest line", self.text("go_tail"))

    def test_a_newer_tip_that_is_a_short_brief_is_refused_too_and_nothing_goes_to_the_sync_branch(self):
        self.put(self.seed["go_tail"], "CLAUDE.md", BRIEF)  # after plan, someone replaced the copy with a brief: the first push is rejected, the merge is redone on this tip
        gitc(self.seed["go_tail"], "add", "-A")
        gitc(self.seed["go_tail"], "commit", "-q", "-m", "a brief")
        gitc(self.seed["go_tail"], "push", "-q", "origin", "HEAD:main")
        newer = self.tip("go_tail")
        res = self.merge(only="go_tail")
        self.assertEqual(res["go_tail"]["result"], "refused-growth")
        self.assertEqual(self.tip("go_tail"), newer)
        self.assertEqual(self.branches("go_tail"), ["main"])

    def test_weekly_reports_the_refusal_as_a_problem_and_still_serves_the_others(self):
        before = self.tip("go_brief")
        rc, rep = self.weekly(self.reg)
        self.assertEqual(rc, 1)
        self.assertEqual([(p["repo"], p["result"]) for p in rep["problems"]], [("go_brief", "refused-growth")])
        self.assertEqual(rep["results"], {"pushed": 2, "refused-growth": 1})
        self.assertEqual(rep["excluded"], [])
        self.assertEqual(self.tip("go_brief"), before)


if __name__ == "__main__":
    unittest.main()
