"""regen_generated_blocks.py: drift in a GENERATED block is proposed on its own branch, never merged; nothing to do when up to date."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("regen", os.path.join(HERE, "regen_generated_blocks.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

MAIN_GO = '''package main

import "os"

func main() {
	switch os.Args[1] {
	case "alpha":
		cmdAlpha(os.Args[2:])
	case "beta", "bravo":
		cmdBeta(os.Args[2:])
%s	default:
		os.Exit(2)
	}
}
'''
CLAUDE = """# Brief

intro line

<!-- BEGIN GENERATED:verbs repo=go_fleet_runner -->

_2 verbs, generated from the dispatch switch in `go_fleet_runner/main.go`; do not edit by hand._

`alpha`, `beta`

<!-- END GENERATED:verbs -->

tail line
"""


def git(d, *a):
    return subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), capture_output=True, text=True, check=True).stdout


class Regen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name
        self.origins = os.path.join(r, "origins")
        os.makedirs(self.origins)
        self.reg_url = os.path.join(self.origins, "services-registry.git")
        self.run_url = os.path.join(self.origins, "go_fleet_runner.git")
        for url in (self.reg_url, self.run_url):
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", url], check=True)
        self.runner = self.seed(self.run_url, {"main.go": MAIN_GO % ""})
        with open(os.path.join(HERE, "docdrift.py")) as f:
            tool = f.read()
        self.registry = self.seed(self.reg_url, {"CLAUDE.md": CLAUDE, "bin/docdrift.py": tool})
        self.work = os.path.join(r, "work")
        os.makedirs(self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, url, files):
        d = os.path.join(self.tmp.name, os.path.basename(url)[:-4] + "-seed")
        subprocess.run(["git", "clone", "-q", url, d], check=True, capture_output=True)
        self.write(d, files)
        return d

    def write(self, d, files):
        for path, text in files.items():
            full = os.path.join(d, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "update")
        git(d, "push", "-q", "origin", "HEAD:main")

    def regen(self, *extra):
        return R.main(["--work", self.work, "--registry-url", self.reg_url, "--base-url", self.origins + "/", "--no-pr"] + list(extra))

    def branch_tip(self):
        out = subprocess.run(["git", "ls-remote", "--heads", self.reg_url, "refs/heads/" + R.BRANCH], capture_output=True, text=True).stdout
        return out.split()[0] if out.strip() else None

    def test_a_new_verb_is_proposed_on_its_own_branch_and_nothing_else_changes(self):
        self.write(self.runner, {"main.go": MAIN_GO % '\tcase "gamma":\n\t\tcmdGamma(os.Args[2:])\n'})
        self.assertEqual(self.regen(), 0)
        tip = self.branch_tip()
        self.assertIsNotNone(tip, "the proposal branch was not pushed")
        new = subprocess.run(["git", "--git-dir", self.reg_url, "show", R.BRANCH + ":CLAUDE.md"], capture_output=True, text=True).stdout
        self.assertIn("_4 verbs", new)
        self.assertIn("`gamma`", new)
        before = CLAUDE.split("<!-- BEGIN GENERATED")[0], CLAUDE.split("<!-- END GENERATED:verbs -->")[1]
        self.assertEqual((new.split("<!-- BEGIN GENERATED")[0], new.split("<!-- END GENERATED:verbs -->")[1]), before)  # intro and tail untouched
        main_tip = subprocess.run(["git", "--git-dir", self.reg_url, "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(main_tip, tip, "main must not move: the job never merges")

    def test_a_second_run_refreshes_the_same_branch_instead_of_adding_another(self):
        self.write(self.runner, {"main.go": MAIN_GO % '\tcase "gamma":\n\t\tcmdGamma(os.Args[2:])\n'})
        self.regen()
        first = self.branch_tip()
        self.write(self.runner, {"main.go": MAIN_GO % '\tcase "gamma":\n\t\tcmdGamma(os.Args[2:])\n\tcase "delta":\n\t\tcmdDelta(os.Args[2:])\n'})
        self.regen()
        self.assertNotEqual(self.branch_tip(), first)
        new = subprocess.run(["git", "--git-dir", self.reg_url, "show", R.BRANCH + ":CLAUDE.md"], capture_output=True, text=True).stdout
        self.assertIn("_5 verbs", new)
        heads = subprocess.run(["git", "ls-remote", "--heads", self.reg_url], capture_output=True, text=True).stdout.count("refs/heads/")
        self.assertEqual(heads, 2)  # main and the one proposal branch

    def test_dry_run_pushes_nothing(self):
        self.write(self.runner, {"main.go": MAIN_GO % '\tcase "gamma":\n\t\tcmdGamma(os.Args[2:])\n'})
        self.assertEqual(self.regen("--dry-run"), 0)
        self.assertIsNone(self.branch_tip())

    def test_nothing_to_do_when_the_blocks_are_up_to_date(self):
        self.write(self.runner, {"main.go": MAIN_GO % '\tcase "gamma":\n\t\tcmdGamma(os.Args[2:])\n'})
        self.regen()
        merged = subprocess.run(["git", "--git-dir", self.reg_url, "show", R.BRANCH + ":CLAUDE.md"], capture_output=True, text=True).stdout
        self.write(self.registry, {"CLAUDE.md": merged})  # a person merged the proposal
        subprocess.run(["git", "--git-dir", self.reg_url, "branch", "-D", R.BRANCH], check=True, capture_output=True)
        self.assertEqual(self.regen(), 0)
        self.assertIsNone(self.branch_tip(), "no drift: no branch")


if __name__ == "__main__":
    unittest.main()
