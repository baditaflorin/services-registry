#!/usr/bin/env python3
"""Hermetic tests for docdrift.py (builds tiny git repos in a temp dir; no network, no fleet repos).
Run: python3 -m unittest discover -s <dir containing docdrift.py> -p 'test_docdrift.py'"""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("docdrift", os.path.join(HERE, "docdrift.py"))
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

MAIN_GO = '''package main

import "os"

func main() {
	switch os.Args[1] {
	case "alpha":
		cmdAlpha(os.Args[2:])
	case "beta", "bravo":
		cmdBeta(os.Args[2:])
	default:
		os.Exit(2)
	}
}
'''
OTHER_GO = '''package main

// cmdAlpha does alpha things.
func cmdAlpha(args []string) {
	_ = args // keep
	println("alpha", len(args))
}

func cmdBeta(args []string) {
	println("beta")
}

const Limit = 5
'''


def sh(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode()


def commit(repo, files, msg):
    for path, text in files.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").write(text)
        sh(repo, "add", path)
    sh(repo, "commit", "-q", "-m", msg)
    return sh(repo, "rev-parse", "HEAD").strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.repo = os.path.join(self.root, "runner")
        os.makedirs(self.repo)
        sh(self.repo, "init", "-q", "-b", "main")
        self.c1 = commit(self.repo, {"main.go": MAIN_GO, "other.go": OTHER_GO, "data.json": json.dumps({"services": {"a": {"replicas": 2, "targets": ["x", "y"]}}})}, "init")
        self.git = D.Git(self.root, "main")

    def tearDown(self):
        self.tmp.cleanup()


class ResolveTests(Base):
    def test_go_symbol_ignores_comments_and_whitespace(self):
        t1, _ = D.resolve(self.git, "runner", "go:other.go#cmdAlpha")
        commit(self.repo, {"other.go": OTHER_GO.replace("// cmdAlpha does alpha things.", "// reworded comment").replace("_ = args // keep", "_ = args   // another")}, "comment-only")
        t2, _ = D.resolve(D.Git(self.root, "main"), "runner", "go:other.go#cmdAlpha")
        self.assertEqual(D.sha(t1), D.sha(t2))

    def test_go_symbol_changes_with_body(self):
        d1 = D.sha(D.resolve(self.git, "runner", "go:other.go#cmdAlpha")[0])
        commit(self.repo, {"other.go": OTHER_GO.replace('println("alpha", len(args))', 'println("alpha!", len(args))')}, "body")
        d2 = D.sha(D.resolve(D.Git(self.root, "main"), "runner", "go:other.go#cmdAlpha")[0])
        self.assertNotEqual(d1, d2)

    def test_go_const_and_missing(self):
        text, _ = D.resolve(self.git, "runner", "go:other.go#Limit")
        self.assertIn("Limit", text)
        with self.assertRaises(LookupError):
            D.resolve(self.git, "runner", "go:other.go#Nope")

    def test_verb_block_includes_handler_and_multilabel(self):
        t, f = D.resolve(self.git, "runner", "verb:alpha")
        self.assertIn("cmdAlpha", t)
        self.assertEqual(f, "other.go")
        tb, _ = D.resolve(self.git, "runner", "verb:bravo")
        self.assertIn("cmdBeta", tb)
        with self.assertRaises(LookupError):
            D.resolve(self.git, "runner", "verb:canary")

    def test_verb_digest_stable_when_other_verb_changes(self):
        d1 = D.sha(D.resolve(self.git, "runner", "verb:alpha")[0])
        commit(self.repo, {"main.go": MAIN_GO.replace('cmdBeta(os.Args[2:])', 'cmdBeta(os.Args[2:]) // changed')}, "touch beta")
        commit(self.repo, {"other.go": OTHER_GO.replace('println("beta")', 'println("beta2")')}, "beta body")
        d2 = D.sha(D.resolve(D.Git(self.root, "main"), "runner", "verb:alpha")[0])
        self.assertEqual(d1, d2)

    def test_json_pointer_regex_file(self):
        t, _ = D.resolve(self.git, "runner", "json:data.json#/services/a/replicas")
        self.assertEqual(t, "2")
        r, _ = D.resolve(self.git, "runner", "regex:other.go#const Limit = \\d+")
        self.assertIn("Limit = 5", r)
        f, _ = D.resolve(self.git, "runner", "file:data.json")
        self.assertIn("services", f)
        with self.assertRaises(LookupError):
            D.resolve(self.git, "runner", "json:data.json#/services/zzz")


class CheckTests(Base):
    def doc(self, anchors):
        p = os.path.join(self.root, "doc.md")
        open(p, "w").write("# doc\n\n" + "\n\n".join(anchors) + "\n")
        return p

    def anchor(self, sym, aid="a1", extra=""):
        t, _ = D.resolve(self.git, "runner", sym)
        return "<!-- anchor id=%s repo=runner symbol=%s verified=%s digest=%s on=2026-10-12 %s-->" % (aid, sym, self.c1[:9], D.sha(t), extra)

    def run_check(self, p, as_of="2026-10-12"):
        _, anchors, canon = D.parse_doc(p)
        g = D.Git(self.root, "main")
        return [D.check_anchor(g, a, as_of, False) for a in anchors]

    def test_fresh_then_stale_then_broken(self):
        p = self.doc([self.anchor("go:other.go#cmdAlpha"), self.anchor("verb:alpha", "a2")])
        self.assertEqual([r["status"] for r in self.run_check(p)], ["FRESH", "FRESH"])
        commit(self.repo, {"other.go": OTHER_GO.replace('println("alpha", len(args))', 'println("alpha", 1)')}, "change alpha")
        res = self.run_check(p)
        self.assertEqual([r["status"] for r in res], ["STALE", "STALE"])
        self.assertTrue(res[0]["commits"][0].endswith("change alpha"))
        commit(self.repo, {"main.go": MAIN_GO.replace('case "alpha":\n\t\tcmdAlpha(os.Args[2:])\n', '')}, "remove alpha")
        self.assertEqual(self.run_check(p)[1]["status"], "BROKEN")

    def test_unavailable_repo_is_skipped_not_broken(self):
        p = self.doc(["<!-- anchor id=s1 repo=notcloned symbol=verb:alpha verified=abc digest=sha256:0 on=2026-10-12 -->"])
        self.assertEqual(self.run_check(p)[0]["status"], "SKIPPED")
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "check", p]), 0)

    def test_expired(self):
        p = self.doc([self.anchor("go:other.go#Limit", "e1", "expires=2026-10-01 ")])
        self.assertEqual(self.run_check(p, "2026-10-12")[0]["status"], "EXPIRED")
        self.assertEqual(self.run_check(p, "2026-09-01")[0]["status"], "FRESH")

    def test_unknown_verified_sha_is_reported_as_rewritten(self):
        t, _ = D.resolve(self.git, "runner", "go:other.go#Limit")
        bad = "<!-- anchor id=u repo=runner symbol=go:other.go#Limit verified=deadbeef0 digest=sha256:0 on=2026-10-12 -->"
        p = self.doc([bad])
        r = self.run_check(p)[0]
        self.assertEqual(r["status"], "STALE")
        self.assertIn("not in runner", r["detail"])

    def test_stamp_refuses_a_shallow_clone(self):
        shallow = os.path.join(self.root, "shallowrepo")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + self.repo, shallow], check=True, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p = os.path.join(self.root, "st.md")
        open(p, "w").write("# d\n<!-- anchor id=s repo=shallowrepo symbol=verb:alpha verified=deadbeef0 digest=sha256:0 on=2026-10-01 -->\n")
        before = open(p).read()
        D.main(["--repos", self.root, "--ref", "main", "stamp", p, "--yes"])
        self.assertEqual(open(p).read(), before)  # nothing was rewritten
        self.assertTrue(D.Git(self.root, "main").incomplete("shallowrepo"))
        self.assertFalse(D.Git(self.root, "main").incomplete("runner"))

    def test_add_refuses_unresolvable_and_stamp_needs_yes(self):
        p = os.path.join(self.root, "d2.md")
        open(p, "w").write("# d\nline\n")
        ap = D.main(["--repos", self.root, "--ref", "main", "add", "--doc", p, "--line", "2", "--id", "x", "--repo", "runner", "--symbol", "verb:canary"])
        self.assertEqual(ap, 1)
        ok = D.main(["--repos", self.root, "--ref", "main", "add", "--doc", p, "--line", "2", "--id", "x", "--repo", "runner", "--symbol", "verb:alpha"])
        self.assertEqual(ok, 0)
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "stamp", p]), 2)
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "stamp", p, "--yes"]), 0)


class GenerateAndLintTests(Base):
    def test_gen_verbs_check_and_write(self):
        p = os.path.join(self.root, "g.md")
        open(p, "w").write("x\n<!-- BEGIN GENERATED:verbs repo=runner -->\nstale hand typed list\n<!-- END GENERATED:verbs -->\ny\n")
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--check"]), 1)
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--write"]), 0)
        text = open(p).read()
        self.assertIn("`alpha`", text)
        self.assertIn("`bravo`", text)
        self.assertNotIn("stale hand typed", text)
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--check"]), 0)

    def test_gen_skips_block_whose_repo_is_missing(self):
        p = os.path.join(self.root, "g3.md")
        body = "<!-- BEGIN GENERATED:verbs repo=notcloned -->\nkeep\n<!-- END GENERATED:verbs -->\n"
        open(p, "w").write(body)
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--check"]), 0)
        self.assertEqual(open(p).read(), body)

    def test_gen_proxmox_inventory_renders_one_host_without_status_or_date(self):
        fs = os.path.join(self.root, "fleet-state")
        os.makedirs(fs)
        sh(fs, "init", "-q", "-b", "main")
        snap = {"taken_at": "2026-10-14T10:00:00Z", "hosts": {
            "0infer": {"hostname": "pve01", "pve_version": "x", "guests": [
                {"type": "qemu", "vmid": 702, "name": "infer-builder-ci", "status": "running", "cpus": 30, "maxmem_mb": 24576, "template": False},
                {"type": "qemu", "vmid": 9000, "name": "infer-debian13-template", "status": "stopped", "cpus": 2, "maxmem_mb": 2048, "template": True}]},
            "0docker": {"hostname": "h", "pve_version": "x", "guests": []}}}
        commit(fs, {"inventory/proxmox-snapshot.json": json.dumps(snap)}, "snapshot")
        p = os.path.join(self.root, "inv.md")
        open(p, "w").write("a\n<!-- BEGIN GENERATED:proxmox-inventory host=0infer -->\nhand typed\n<!-- END GENERATED:proxmox-inventory -->\nb\n")
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--write"]), 0)
        t = open(p).read()
        self.assertIn("| 702 | `infer-builder-ci` | VM | 30 | 24 |", t)
        self.assertIn("| 9000 | `infer-debian13-template` (template) | VM | 2 | 2 |", t)
        self.assertNotIn("running", t)
        self.assertNotIn("2026-10-14", t)
        self.assertNotIn("hand typed", t)
        # a later snapshot with a changed taken_at and the same guests does not change the block
        snap["taken_at"] = "2026-10-21T10:00:00Z"
        commit(fs, {"inventory/proxmox-snapshot.json": json.dumps(snap)}, "weekly")
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--check"]), 0)
        # a new RAM size does
        snap["hosts"]["0infer"]["guests"][0]["maxmem_mb"] = 32768
        commit(fs, {"inventory/proxmox-snapshot.json": json.dumps(snap)}, "bigger")
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--check"]), 1)

    def test_gen_accepts_space_after_colon(self):
        p = os.path.join(self.root, "g2.md")
        open(p, "w").write("<!-- BEGIN GENERATED: verbs repo=runner -->\n<!-- END GENERATED: verbs -->\n")
        self.assertEqual(D.main(["--repos", self.root, "--ref", "main", "gen", p, "--write"]), 0)
        self.assertIn("`alpha`", open(p).read())

    def test_lint_flags_only_real_lone_ampersand(self):
        p = os.path.join(self.root, "l.md")
        open(p, "w").write("```bash\ngit add -A & git commit -m x\ngit add -A && git commit -m y\n./server &\nmake a   # Security & Compliance\ncmd >out 2>&1\necho \"a & b\"\n```\n\nInline `git tag 1 & git push` and `a && b`.\n")
        hits = [i for i, _ in D.lint_shell(p)]
        self.assertEqual(hits, [2, 10])


class CopyTests(Base):
    def make_canon(self):
        canon = os.path.join(self.root, "registry")
        os.makedirs(canon)
        sh(canon, "init", "-q", "-b", "main")
        v = []
        for i in range(3):
            commit(canon, {"CLAUDE.md": "# canon v%d\nline\n" % i, "services.json": json.dumps([{"repo_url": "https://github.com/o/a"}])}, "v%d" % i)
            v.append(sh(canon, "rev-parse", "HEAD").strip())
        return canon, v

    def test_copy_status_exact_match_gives_revisions_behind(self):
        canon, v = self.make_canon()
        g = D.Git(self.root, "main")
        revs = D.canonical_revisions(g, "registry", "CLAUDE.md")
        self.assertEqual(len(revs), 3)
        self.assertEqual(D.copy_status(revs, D.blob_sha("# canon v2\nline\n"))[:2], ("current", 0))
        self.assertEqual(D.copy_status(revs, D.blob_sha("# canon v0\nline\n"))[:2], ("behind", 2))
        self.assertEqual(D.copy_status(revs, D.blob_sha("# something else\n"))[0], "diverged")

    def test_sync_keeps_local_tail_only(self):
        canon = os.path.join(self.root, "canon.md")
        open(canon, "w").write("# canon v9\nline\n")
        tgt = os.path.join(self.root, "CLAUDE.md")
        open(tgt, "w").write("# old canon\nold\n" + D.LOCAL_MARK_TEXT + "\n## repo notes\nkeep me\n")
        self.assertEqual(D.main(["--repos", self.root, "sync", canon, tgt]), 1)          # differs: prints diff
        self.assertEqual(D.main(["--repos", self.root, "sync", canon, tgt, "--write"]), 0)
        out = open(tgt).read()
        self.assertTrue(out.startswith("# canon v9\nline\n"))
        self.assertIn("keep me", out)
        self.assertNotIn("old canon", out)
        self.assertEqual(D.main(["--repos", self.root, "sync", canon, tgt]), 0)          # idempotent
        body, tail = D.split_local_tail(out)
        self.assertEqual(body, "# canon v9\nline\n")

    def test_sync_without_tail_is_byte_identical_to_canonical(self):
        canon = os.path.join(self.root, "canon.md")
        open(canon, "w").write("# canon v9\nline\n")
        tgt = os.path.join(self.root, "T.md")
        open(tgt, "w").write("totally different\n")
        D.main(["--repos", self.root, "sync", canon, tgt, "--write"])
        self.assertEqual(open(tgt).read(), open(canon).read())

    def test_canonical_ref_stamp(self):
        canon, v = self.make_canon()
        p = os.path.join(self.root, "copy.md")
        open(p, "w").write("<!-- canonical-ref repo=registry path=CLAUDE.md sha=%s level=warn -->\n" % v[0][:9])
        _, _, c = D.parse_doc(p)
        r = D.check_canon(D.Git(self.root, "main"), c[0], "2026-10-12")
        self.assertEqual(r["status"], "STALE")
        self.assertIn("moved 2 commit", r["detail"])


if __name__ == "__main__":
    unittest.main()
