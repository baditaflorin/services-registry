#!/usr/bin/env python3
"""Hermetic tests for evidence_ledger.py (tiny git repos in a temp dir; no network, no fleet repos).
Run: python3 -m unittest discover -s <dir containing evidence_ledger.py> -p 'test_evidence_ledger.py'"""
import argparse
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("evidence_ledger", os.path.join(HERE, "evidence_ledger.py"))
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
V1 = "package main\n\n// timeout is the request timeout.\nconst timeout = 30\n\nfunc main() {}\n"


def git(d, *a):
    r = subprocess.run(["git", "-C", d] + list(a), capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repos = self.tmp.name
        self.d = os.path.join(self.repos, "svc")
        os.makedirs(self.d)
        git(self.d, "init", "-q", "-b", "main")
        self.write("main.go", V1)
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", "v1")
        git(self.d, "tag", "v1.0.0")
        self.sha = git(self.d, "rev-parse", "HEAD")
        claims = os.path.join(self.repos, "claims.jsonl")
        with open(claims, "w") as f:
            f.write(json.dumps(dict(chunk="c1", n=1, repo="doc-repo", path="CLAUDE.md", line=12, text="the timeout is 30 s", final_verdict="TRUE", severity=None,
                                    evidence=[dict(ref="svc/main.go:3-4@%s" % self.sha), dict(ref="other/x.go:1@%s" % self.sha), dict(ref="not a ref")])) + "\n")
        self.ledger = os.path.join(self.repos, "ledger.jsonl")
        buf = io.StringIO()
        with redirect_stdout(buf):
            E.cmd_build(argparse.Namespace(claims=claims, repos=self.repos, out=self.ledger, ref="HEAD", as_of="2026-10-12"))
        E._cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.d, name), "w") as f:
            f.write(text)

    def check(self):
        E._cache.clear()
        out = os.path.join(self.repos, "check.json")
        buf = io.StringIO()
        with redirect_stdout(buf):
            E.cmd_check(argparse.Namespace(ledger=self.ledger, repos=self.repos, ref="auto", json=out, md=os.path.join(self.repos, "check.md"), show_diff=0))
        with open(out) as f:
            return json.load(f)

    def test_build_stamps_commit_tag_and_snippet(self):
        with open(self.ledger) as f:
            rows = [json.loads(l) for l in f]
        self.assertEqual(len(rows), 1)  # the repo that is not cloned and the non-ref evidence are skipped, not invented
        r = rows[0]
        self.assertEqual((r["repo"], r["path"], r["lines"], r["stamp_tag"]), ("svc", "main.go", [3, 4], "v1.0.0"))
        self.assertEqual(r["stamp_sha"], self.sha[:12])
        self.assertIn("const timeout = 30", "\n".join(r["snippet"]))

    def test_fresh_when_nothing_changed(self):
        res = self.check()
        self.assertEqual(res["summary"]["statuses"], {"FRESH": 1})

    def test_moved_when_lines_shift(self):
        self.write("main.go", "// new header\n// another\n" + V1)
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", "header")
        row = self.check()["rows"][0]
        self.assertEqual(row["status"], "MOVED")
        self.assertEqual(row["now_at_line"], 5)

    def test_changed_reports_commits_and_release_tags_since_with_a_diff(self):
        self.write("main.go", V1.replace("= 30", "= 5"))
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", "tighten")
        git(self.d, "tag", "v1.1.0")
        self.write("README.md", "x\n")
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", "docs")
        res = self.check()
        row = res["rows"][0]
        self.assertEqual(row["status"], "CHANGED")
        self.assertEqual((row["commits_since"], row["tags_since"], row["now_tag"]), (2, 1, "v1.1.0"))
        self.assertIn("-const timeout = 30", row["diff"])
        self.assertIn("+const timeout = 5", row["diff"])
        repo = res["summary"]["repos"][0]
        self.assertEqual((repo["stamp_tag"], repo["now_tag"], repo["commits_since"], repo["tags_since"]), ("v1.0.0", "v1.1.0", 2, 1))
        with open(os.path.join(self.repos, "check.md")) as f:
            self.assertIn("v1.0.0", f.read())

    def test_gone_when_the_file_is_deleted(self):
        git(self.d, "rm", "-q", "main.go"); git(self.d, "commit", "-q", "-m", "rm")
        self.assertEqual(self.check()["rows"][0]["status"], "GONE")

    def test_no_repo_when_the_repository_is_not_cloned(self):
        os.rename(self.d, self.d + ".away")
        self.assertEqual(self.check()["rows"][0]["status"], "NO-REPO")

    def test_whitespace_only_edits_do_not_count_as_changes(self):
        self.write("main.go", V1.replace("const timeout = 30", "const   timeout  =  30"))
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", "fmt")
        self.assertEqual(self.check()["rows"][0]["status"], "FRESH")

    def test_a_credential_like_snippet_is_not_stored_and_its_diff_is_withheld(self):
        self.write("conf.yml", "db:\n  user: app\n  password: Zx81hQ7vLm2PqR9sTu4Wk6\n")
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", "conf")
        sha = git(self.d, "rev-parse", "HEAD")
        claims = os.path.join(self.repos, "claims2.jsonl")
        with open(claims, "w") as f:
            f.write(json.dumps(dict(chunk="c2", n=1, repo="doc-repo", path="CLAUDE.md", line=3, text="the db has a login", final_verdict="TRUE", severity=None,
                                    evidence=[dict(ref="svc/conf.yml:2-3@%s" % sha)])) + "\n")
        ledger = os.path.join(self.repos, "ledger2.jsonl")
        with redirect_stdout(io.StringIO()):
            E.cmd_build(argparse.Namespace(claims=claims, repos=self.repos, out=ledger, ref="HEAD", as_of="2026-10-12"))
        with open(ledger) as f:
            raw = f.read()
        self.assertNotIn("Zx81hQ7vLm2PqR9sTu4Wk6", raw)
        self.assertTrue(json.loads(raw.splitlines()[0])["redacted"])
        self.write("conf.yml", "db:\n  user: app\n  password: Qq19WmN4vB8xRt2LpZ7Ds5\n")
        git(self.d, "add", "-A"); git(self.d, "commit", "-q", "-m", "rotate")
        E._cache.clear()
        out = os.path.join(self.repos, "check2.json")
        with redirect_stdout(io.StringIO()):
            E.cmd_check(argparse.Namespace(ledger=ledger, repos=self.repos, ref="auto", json=out, md=None, show_diff=0))
        with open(out) as f:
            text = f.read()
        self.assertIn('"status": "CHANGED"', text)  # still detected, by digest
        self.assertNotIn("Qq19WmN4vB8xRt2LpZ7Ds5", text)
        self.assertNotIn("Zx81hQ7vLm2PqR9sTu4Wk6", text)


if __name__ == "__main__":
    unittest.main()
