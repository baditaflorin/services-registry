"""sync_extra_repos() of bin/rollout-weekly.sh: fleet-state's rollout/extra_repos.txt is the source of truth, the local file the fallback."""
import os
import re
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "rollout-weekly.sh")


def git(d, *a):
    return subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), capture_output=True, text=True, check=True).stdout.strip()


def run_sync(w):
    with open(SCRIPT) as f:
        src = f.read()
    fn = re.search(r"^sync_extra_repos\(\) \{.*?^\}\n", src, re.S | re.M).group(0)
    p = subprocess.run(["bash", "-c", 'W="$1"\n' + fn + "\nsync_extra_repos", "_", w], capture_output=True, text=True)
    return p.stdout.strip()


class SyncExtraRepos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name
        self.origin = os.path.join(r, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", self.origin], check=True)
        seed = os.path.join(r, "seed")
        subprocess.run(["git", "clone", "-q", self.origin, seed], check=True, capture_output=True)
        self.seed = seed
        self.w = os.path.join(r, "work")
        os.makedirs(self.w)
        with open(os.path.join(self.w, "extra_repos.txt"), "w") as f:
            f.write("# local\nlocal-only-repo\n")

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, text):
        os.makedirs(os.path.join(self.seed, "rollout"), exist_ok=True)
        with open(os.path.join(self.seed, "rollout", "extra_repos.txt"), "w") as f:
            f.write(text)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", "list")
        git(self.seed, "push", "-q", "origin", "HEAD:master")
        subprocess.run(["git", "clone", "-q", self.origin, os.path.join(self.w, "fleet-state")], check=True, capture_output=True)

    def local(self):
        with open(os.path.join(self.w, "extra_repos.txt")) as f:
            return f.read()

    def test_the_list_in_fleet_state_replaces_the_local_file(self):
        self.publish("# header\nrepo-a\nrepo-b\n\n# more\nrepo-c\n")
        out = run_sync(self.w)
        self.assertIn("synced from fleet-state (3 names)", out)
        self.assertEqual(self.local().count("repo-"), 3)
        self.assertNotIn("local-only-repo", self.local())

    def test_no_list_in_fleet_state_keeps_the_local_file(self):
        self.publish("# header only\n")
        self.assertIn("no list in fleet-state yet", run_sync(self.w))
        self.assertIn("local-only-repo", self.local())

    def test_an_unreachable_fleet_state_keeps_the_local_file(self):
        self.publish("repo-a\n")
        git(os.path.join(self.w, "fleet-state"), "remote", "set-url", "origin", os.path.join(self.tmp.name, "gone.git"))
        self.assertIn("fetch of fleet-state failed", run_sync(self.w))
        self.assertIn("local-only-repo", self.local())


if __name__ == "__main__":
    unittest.main()
