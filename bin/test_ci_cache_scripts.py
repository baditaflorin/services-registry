"""Tests for scripts/ci-cache/*.sh and the agent compose override.

They run the real scripts against throw-away directories; nothing touches Docker
(a stub `docker` binary stands in where a script only checks for it).
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts" / "ci-cache"
OVERRIDE = REPO / "templates" / "woodpecker-agent-ci-cache.override.yml"
DAY = 86400

BASH = shutil.which("bash")


def bash(script, *args, env=None, cwd=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        [BASH, str(script), *args], capture_output=True, text=True, env=full_env, cwd=cwd, timeout=60
    )


def put(path, kb, age_days=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 1024 * kb)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


@unittest.skipUnless(BASH, "bash is required")
class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ci-cache-test-"))
        self.root = self.tmp / "woodpecker-ci"
        for name in ("go-build", "go-mod", "npm"):
            (self.root / name).mkdir(parents=True)

    def tearDown(self):
        for path in self.tmp.rglob("*"):
            try:
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_under_the_cap_nothing_is_deleted(self):
        put(self.root / "go-build" / "aa" / "old", 100, age_days=60)
        result = bash(SCRIPTS / "prune.sh", "--root", str(self.root), "--go-build-max-mb", "50")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "go-build" / "aa" / "old").exists())

    def test_over_the_cap_deletes_oldest_first_and_stops_when_under(self):
        put(self.root / "go-build" / "aa" / "old", 1100, age_days=40)
        put(self.root / "go-build" / "bb" / "mid", 1100, age_days=10)
        put(self.root / "go-build" / "cc" / "new", 1100, age_days=0)
        result = bash(SCRIPTS / "prune.sh", "--root", str(self.root), "--go-build-max-mb", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "go-build" / "aa" / "old").exists())
        self.assertFalse((self.root / "go-build" / "bb" / "mid").exists())
        self.assertTrue((self.root / "go-build" / "cc" / "new").exists(), "the newest entry must survive")

    def test_dry_run_deletes_nothing(self):
        put(self.root / "npm" / "a", 1100, age_days=40)
        put(self.root / "npm" / "b", 1100, age_days=40)
        result = bash(SCRIPTS / "prune.sh", "--root", str(self.root), "--npm-max-mb", "1", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stdout)
        self.assertTrue((self.root / "npm" / "a").exists() and (self.root / "npm" / "b").exists())

    def test_modcache_prunes_old_read_only_modules_but_keeps_recent_ones(self):
        old_mod = self.root / "go-mod" / "github.com" / "x" / "y@v1.0.0"
        new_mod = self.root / "go-mod" / "github.com" / "x" / "z@v2.0.0"
        put(old_mod / "big.go", 1500, age_days=60)
        put(new_mod / "big.go", 10, age_days=1)
        for target in (old_mod / "big.go", old_mod):  # Go leaves the module cache read-only
            target.chmod(0o444 if target.is_file() else 0o555)
        os.utime(old_mod, (time.time() - 60 * DAY,) * 2)
        result = bash(SCRIPTS / "prune.sh", "--root", str(self.root), "--go-mod-max-mb", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(old_mod.exists(), "old read-only module directory should be removed")
        self.assertTrue(new_mod.exists(), "a recently written module must be kept")

    def test_modcache_never_goes_below_the_minimum_age(self):
        recent = self.root / "go-mod" / "github.com" / "x" / "y@v1.0.0"
        put(recent / "big.go", 1500, age_days=2)
        result = bash(
            SCRIPTS / "prune.sh", "--root", str(self.root), "--go-mod-max-mb", "1", "--mod-min-age-days", "14"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(recent.exists(), "in-use-looking modules are never pruned even over the cap")

    def test_refuses_dangerous_or_missing_roots(self):
        for bad in ("/", "/tmp", "/var/cache", "relative/path", str(self.tmp / "missing")):
            with self.subTest(root=bad):
                result = bash(SCRIPTS / "prune.sh", "--root", bad)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_rejects_non_numeric_limits(self):
        result = bash(SCRIPTS / "prune.sh", "--root", str(self.root), "--npm-max-mb", "lots")
        self.assertEqual(result.returncode, 2)


@unittest.skipUnless(BASH, "bash is required")
class InstallHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ci-cache-install-"))
        stub_dir = self.tmp / "bin"
        stub_dir.mkdir()
        docker = stub_dir / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n")
        docker.chmod(0o755)
        self.env = {"PATH": f"{stub_dir}:{os.environ['PATH']}"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_creates_nothing_and_prints_the_next_steps(self):
        root = self.tmp / "cache"
        result = bash(SCRIPTS / "install-host.sh", "--root", str(root), "--min-free-gb", "0", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(root.exists())
        self.assertIn("woodpecker-agent-ci-cache.override.yml", result.stdout)
        self.assertIn("docker inspect", result.stdout)

    def test_apply_creates_the_three_directories(self):
        root = self.tmp / "cache"
        result = bash(SCRIPTS / "install-host.sh", "--root", str(root), "--min-free-gb", "0", "--apply", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("go-build", "go-mod", "npm"):
            self.assertTrue((root / name).is_dir(), name)

    def test_refuses_when_there_is_not_enough_free_disk(self):
        root = self.tmp / "cache"
        result = bash(
            SCRIPTS / "install-host.sh", "--root", str(root), "--min-free-gb", "99999999", "--apply", env=self.env
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not enough free disk", result.stderr)
        self.assertFalse(root.exists())

    def test_refuses_without_docker(self):
        empty = self.tmp / "nodocker"
        empty.mkdir()
        for tool in ("bash", "dirname", "df", "awk", "install", "mkdir", "sed", "cat"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, empty / tool)
        result = subprocess.run(
            [BASH, str(SCRIPTS / "install-host.sh"), "--root", str(self.tmp / "c")],
            capture_output=True,
            text=True,
            env={"PATH": str(empty)},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("docker is not installed", result.stderr)


@unittest.skipUnless(BASH, "bash is required")
class BenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ci-cache-bench-"))
        self.src = self.tmp / "svc"
        self.src.mkdir()
        (self.src / "go.mod").write_text("module example.com/svc\n\ngo 1.25\n")
        self.other = self.tmp / "other"
        self.other.mkdir()
        (self.other / "go.mod").write_text("module example.com/other\n\ngo 1.25\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_prints_the_four_docker_runs_without_running_docker(self):
        result = bash(
            SCRIPTS / "bench.sh", "--src", str(self.src), "--other-src", str(self.other), "--dry-run",
            env={"TMPDIR": str(self.tmp)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [line for line in result.stdout.splitlines() if "docker run" in line]
        self.assertEqual(len(runs), 4, result.stdout)
        self.assertIn("/root/.cache/go-build", runs[0])
        self.assertIn("/go/pkg/mod", runs[0])
        self.assertIn("golang:1.25", runs[0])
        for label in ("cold", "warm", "prime-other", "warm-other"):
            self.assertIn(f"[dry-run] {label}:", result.stdout)

    def test_exit_status_is_not_masked_by_the_cleanup_trap(self):
        result = bash(SCRIPTS / "bench.sh", "--src", str(self.tmp / "does-not-exist"), "--dry-run")
        self.assertEqual(result.returncode, 2)

    def test_requires_a_go_module(self):
        (self.src / "go.mod").unlink()
        result = bash(SCRIPTS / "bench.sh", "--src", str(self.src), "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no go.mod", result.stderr)

    def test_without_docker_it_refuses_unless_dry_run(self):
        empty = self.tmp / "nodocker"
        empty.mkdir()
        for tool in ("bash", "mktemp", "rm", "dirname", "sed", "mkdir"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, empty / tool)
        result = subprocess.run(
            [BASH, str(SCRIPTS / "bench.sh"), "--src", str(self.src)],
            capture_output=True,
            text=True,
            env={"PATH": str(empty)},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("docker is required", result.stderr)


class OverrideFileTests(unittest.TestCase):
    def setUp(self):
        self.text = OVERRIDE.read_text()

    def test_mounts_exactly_the_three_default_cache_paths(self):
        value = re.search(r"WOODPECKER_BACKEND_DOCKER_VOLUMES: >-\n\s+(\S+)", self.text).group(1)
        pairs = value.split(",")
        self.assertEqual(len(pairs), 3, pairs)
        destinations = sorted(pair.rsplit(":", 1)[1] for pair in pairs)  # the default value itself contains a colon
        self.assertEqual(destinations, ["/go/pkg/mod", "/root/.cache/go-build", "/root/.npm"])
        for pair in pairs:
            self.assertTrue(pair.startswith("${WOODPECKER_CI_CACHE_ROOT:-/var/cache/woodpecker-ci}/"), pair)

    def test_targets_the_agent_service_of_the_base_template(self):
        base = (REPO / "templates" / "woodpecker-cross-host-agent.compose.yml").read_text()
        service = re.search(r"^  (woodpecker-[\w-]+):", self.text, re.M).group(1)
        self.assertIn(f"  {service}:", base)

    def test_base_template_is_left_untouched_so_other_hosts_do_not_change(self):
        base = (REPO / "templates" / "woodpecker-cross-host-agent.compose.yml").read_text()
        self.assertNotIn("WOODPECKER_BACKEND_DOCKER_VOLUMES", base)

    def test_no_secret_looking_values(self):
        self.assertNotRegex(self.text, r"(?i)(token|secret|password)\s*[:=]\s*\S{8,}")


if __name__ == "__main__":
    unittest.main()
