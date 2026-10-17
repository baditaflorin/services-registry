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

    def test_a_symlinked_cache_directory_is_never_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        victim = outside / "precious"
        put(victim, 1100, age_days=90)
        shutil.rmtree(self.root / "go-build")
        os.symlink(outside, self.root / "go-build")
        result = bash(SCRIPTS / "prune.sh", "--root", str(self.root), "--go-build-max-mb", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refusing symlinked", result.stdout)
        self.assertTrue(victim.exists(), "a file behind a symlinked cache directory must never be deleted")

    def test_symlinks_inside_the_cache_are_not_followed_either(self):
        outside = self.tmp / "outside2"
        put(outside / "precious", 1100, age_days=90)
        planted = self.root / "go-mod" / "github.com" / "x"
        planted.mkdir(parents=True)
        os.symlink(outside, planted / "evil@v1.0.0")
        put(self.root / "go-mod" / "github.com" / "x" / "big@v1.0.0" / "f.go", 3000, age_days=60)
        result = bash(SCRIPTS / "prune.sh", "--root", str(self.root), "--go-mod-max-mb", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((outside / "precious").exists(), "must not reach through a planted symlink")

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
        self.assertIn("HOLD the agent", result.stdout)

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

    def compose_dir(self, service="woodpecker-agent", filename="compose.yml"):
        d = self.tmp / "agent"
        d.mkdir(exist_ok=True)
        (d / filename).write_text(
            f"services:\n  {service}:\n    image: example/agent:1\n    restart: always\n"
        )
        return d

    def host_args(self, d, **extra):
        args = ["--root", str(self.tmp / "cache"), "--min-free-gb", "0", "--compose-dir", str(d),
                "--compose-file", "compose.yml", "--service", "woodpecker-agent", "--project", "own-agent"]
        for key, value in extra.items():
            args += [f"--{key}", value]
        return args

    def test_host_aware_dry_run_uses_the_hosts_real_names_and_writes_nothing(self):
        d = self.compose_dir()
        result = bash(SCRIPTS / "install-host.sh", *self.host_args(d), env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((d / "woodpecker-agent-ci-cache.override.yml").exists())
        self.assertIn("-p own-agent -f compose.yml -f woodpecker-agent-ci-cache.override.yml config", result.stdout)
        self.assertIn(f"apply-agent.sh --compose-dir {d} --compose-file compose.yml --service woodpecker-agent --project own-agent", result.stdout)
        self.assertIn("label wp_uuid", result.stdout)
        self.assertNotIn("com.woodpecker-ci.step", result.stdout)
        self.assertNotIn("woodpecker-cross-host-agent", result.stdout)

    def test_apply_writes_an_override_for_the_hosts_service_with_tight_permissions(self):
        d = self.compose_dir()
        result = bash(SCRIPTS / "install-host.sh", *self.host_args(d), "--apply", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = d / "woodpecker-agent-ci-cache.override.yml"
        text = target.read_text()
        self.assertIn("\n  woodpecker-agent:\n", text)
        self.assertNotIn("woodpecker-cross-host-agent:", text)
        self.assertIn("WOODPECKER_BACKEND_DOCKER_VOLUMES", text)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        again = bash(SCRIPTS / "install-host.sh", *self.host_args(d), "--apply", env=self.env)
        self.assertEqual(again.returncode, 0, "re-applying the same override is idempotent")

    def test_refuses_to_overwrite_a_different_existing_override(self):
        d = self.compose_dir()
        (d / "woodpecker-agent-ci-cache.override.yml").write_text("services: {}\n")
        result = bash(SCRIPTS / "install-host.sh", *self.host_args(d), "--apply", env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual((d / "woodpecker-agent-ci-cache.override.yml").read_text(), "services: {}\n")

    def test_a_service_that_is_not_in_the_compose_file_is_refused(self):
        d = self.compose_dir(service="something-else")
        result = bash(SCRIPTS / "install-host.sh", *self.host_args(d), "--apply", env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("bogus service", result.stderr)
        self.assertFalse((d / "woodpecker-agent-ci-cache.override.yml").exists())

    def test_service_and_project_names_are_validated(self):
        d = self.compose_dir()
        for flag, value in (("service", "a;rm -rf /"), ("project", "x y"), ("compose-file", "../x.yml")):
            with self.subTest(flag=flag):
                args = self.host_args(d)
                args += [f"--{flag}", value]
                result = bash(SCRIPTS / "install-host.sh", *args, env=self.env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

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



STUB_DOCKER = r"""#!/bin/sh
# Stub docker for apply-agent.sh tests. State lives in files under $STUB_DIR.
echo "$*" >> "$STUB_DIR/calls.log"
pop() { # pop the first line of a sequence file; echo the default when it is empty
  f="$STUB_DIR/$1"
  if [ -s "$f" ]; then head -n1 "$f"; tail -n +2 "$f" > "$f.new"; mv "$f.new" "$f"; else echo "$2"; fi
}
case "$1" in
  ps)
    n="$(pop ps-seq 0)"; i=0
    while [ "$i" -lt "$n" ]; do echo "step$i"; i=$((i+1)); done ;;
  inspect) pop health-seq "${STUB_HEALTH:-healthy}" ;;
  compose)
    case " $* " in
      *" config --services "*) printf '%s\n' "${STUB_SERVICES:-woodpecker-agent}" ;;
      *" config "*)
        echo "services:"; echo "  woodpecker-agent:"; echo "    environment:"
        [ "${STUB_HAS_VOLUMES:-1}" = "1" ] && echo "      WOODPECKER_BACKEND_DOCKER_VOLUMES: /x:/root/.npm" ;;
      *" ps -q "*) echo cid123 ;;
      *) : ;;
    esac ;;
esac
exit 0
"""


@unittest.skipUnless(BASH, "bash is required")
class ApplyAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ci-cache-apply-"))
        self.stub_dir = self.tmp / "stub"
        self.stub_dir.mkdir()
        docker = self.stub_dir / "docker"
        docker.write_text(STUB_DOCKER)
        docker.chmod(0o755)
        self.agent = self.tmp / "agent"
        self.agent.mkdir()
        (self.agent / "compose.yml").write_text("services:\n  woodpecker-agent:\n    image: x\n")
        (self.agent / "woodpecker-agent-ci-cache.override.yml").write_text("services: {}\n")
        self.env = {
            "PATH": f"{self.stub_dir}:{os.environ['PATH']}",
            "STUB_DIR": str(self.stub_dir),
            "CI_CACHE_POLL_SECONDS": "0",
            "CI_CACHE_HEALTH_WAIT_SECONDS": "0",
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_apply(self, *extra, env=None):
        args = ["--compose-dir", str(self.agent), "--compose-file", "compose.yml",
                "--service", "woodpecker-agent", "--project", "own-agent", "--stable", "0", *extra]
        return bash(SCRIPTS / "apply-agent.sh", *args, env={**self.env, **(env or {})})

    def calls(self):
        path = self.stub_dir / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def ups(self):
        return [c for c in self.calls() if " up -d " in c]

    def test_dry_run_changes_nothing(self):
        result = self.run_apply("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ups(), [])
        self.assertIn("would wait", result.stdout)

    def test_waits_for_the_host_to_be_idle_then_recreates_only_the_agent_with_the_override(self):
        (self.stub_dir / "ps-seq").write_text("3\n2\n1\n0\n")
        result = self.run_apply()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ups = self.ups()
        self.assertEqual(len(ups), 1, ups)
        self.assertIn("-p own-agent", ups[0])
        self.assertIn("-f woodpecker-agent-ci-cache.override.yml", ups[0])
        self.assertTrue(ups[0].endswith("up -d woodpecker-agent"), ups[0])
        ps_calls = [c for c in self.calls() if c.startswith("ps ")]
        self.assertGreaterEqual(len(ps_calls), 4, "it must keep polling while steps are running")
        self.assertTrue((self.agent / ".ci-cache-cutover").exists())

    def test_idle_detection_uses_the_label_every_step_container_carries(self):
        # regression: an earlier draft filtered on a label step containers do not have, so a busy
        # host looked idle and a running pipeline was killed
        self.run_apply()
        ps_calls = [c for c in self.calls() if c.startswith("ps ")]
        self.assertTrue(ps_calls)
        for call in ps_calls:
            self.assertIn("label=wp_uuid", call)
            self.assertNotIn("com.woodpecker-ci.step", call)

    def test_never_idle_means_nothing_is_changed(self):
        (self.stub_dir / "ps-seq").write_text("1\n" * 5000)
        result = self.run_apply("--max-wait", "0")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.ups(), [])
        self.assertIn("nothing was changed", result.stderr)

    def test_refuses_when_compose_would_manage_a_second_service(self):
        result = self.run_apply(env={"STUB_SERVICES": "woodpecker-agent\nbogus"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.ups(), [])

    def test_refuses_when_the_override_does_not_set_the_volumes(self):
        result = self.run_apply(env={"STUB_HAS_VOLUMES": "0"})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.ups(), [])

    def test_an_unhealthy_agent_is_rolled_back_automatically(self):
        result = self.run_apply(env={"STUB_HEALTH": "unhealthy"})
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        ups = self.ups()
        self.assertEqual(len(ups), 2, ups)
        self.assertIn("-f woodpecker-agent-ci-cache.override.yml", ups[0])
        self.assertNotIn("override", ups[1], "the automatic rollback must use the base compose file alone")

    def test_rollback_mode_uses_the_base_compose_file_and_records_the_time(self):
        result = self.run_apply("--rollback")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ups = self.ups()
        self.assertEqual(len(ups), 1)
        self.assertNotIn("override", ups[0])
        self.assertTrue((self.agent / ".ci-cache-rollback").exists())

    def test_arguments_are_validated(self):
        for flag, value in (("service", "a b"), ("project", "x;y"), ("compose-file", "../c.yml")):
            with self.subTest(flag=flag):
                args = ["--compose-dir", str(self.agent), "--compose-file", "compose.yml",
                        "--service", "woodpecker-agent", "--project", "own-agent", f"--{flag}", value]
                result = bash(SCRIPTS / "apply-agent.sh", *args, env=self.env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.ups(), [])

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
