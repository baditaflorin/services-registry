"""Tests for bin/fleet-runner-shim: no secret value ever reaches a command line.

The shim is exercised against a stub `ssh` that records its argv and then runs the
remote command locally, exactly as the bastion's shell would, against a fake
`fleet-runner` binary. The fake binary comes in two kinds:

  new  answers `env-check --json` and takes `--env-from-stdin` (what go_fleet_runner
       >= the release that added env_stdin.go does)
  old  knows neither: `unknown command`, exit 1 (every earlier release)

so the skew matrix (new shim + old binary, old shim + new binary) is covered, and every
test asserts where the fake credentials are NOT: argv of any ssh call, the remote command
string, the shim's own stdout/stderr. Values are obviously fake and never real tokens.
"""
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent
SHIM = BIN / "fleet-runner-shim"
LEGACY_SHIM = BIN / "testdata" / "fleet-runner-shim.legacy"

TOKEN1 = "FAKE-TOKEN-1-do-not-use"
TOKEN2 = "FAKE-TOKEN-2-do-not-use"
SECRET_VARS = ["HCLOUD_TOKEN", "HETZNER_TOKEN", "GITHUB_TOKEN", "APIKEY_SERVICE_ADMIN_TOKEN",
               "FLEET_SECRETS_ADMIN_TOKEN", "COOLIFY_API_TOKEN"]

# The ssh stub: log argv of the call, then behave like the bastion's login shell running
# `pct exec 108 -- <command>`: strip the pct prefix and hand the rest to sh -c.
STUB_SSH = r"""#!/bin/sh
n=$(( $(cat "$STUB_LOG/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_LOG/count"
: > "$STUB_LOG/ssh.$n.argv"
for a in "$@"; do printf '%s\n' "$a" >> "$STUB_LOG/ssh.$n.argv"; done
last=""
for a in "$@"; do last="$a"; done
case "$last" in
  "pct exec "*" -- "*) rest=${last#*" -- "} ;;
  *) echo "stub ssh: unexpected remote command" >&2; exit 97 ;;
esac
STUB_CALL=$n
export STUB_CALL
# The far side of the ssh hop does not inherit the laptop's environment: scrub the
# credentials and the identity the shim exports, so the fake binary sees only what
# arrives on the remote command line and on stdin.
exec env -u HCLOUD_TOKEN -u HETZNER_TOKEN -u GITHUB_TOKEN -u APIKEY_SERVICE_ADMIN_TOKEN \
  -u FLEET_SECRETS_ADMIN_TOKEN -u COOLIFY_API_TOKEN -u FLEET_CALLER_HOST -u FLEET_CALLER_USER \
  -u FLEET_CALLER_AGENT -u FLEET_CALLER_SESSION -u FLEET_BASTION -u FLEET_ENV_FORWARD_ALWAYS \
  sh -c "$rest"
"""

# The fake remote fleet-runner. It records how it was called and what it received, and
# emulates the behaviour of the real binary that the shim depends on.
FAKE_BIN = r"""#!/bin/sh
n=$STUB_CALL
{ for a in "$@"; do printf '%s\n' "$a"; done; } > "$STUB_LOG/bin.$n.argv"
if [ "$STUB_BIN_KIND" = old ]; then
  case "$1" in
    env-check|--env-from-stdin) echo "unknown command: $1" ; exit 1 ;;
  esac
fi
env | grep -E '^(HCLOUD_TOKEN|HETZNER_TOKEN|GITHUB_TOKEN|APIKEY_SERVICE_ADMIN_TOKEN|FLEET_SECRETS_ADMIN_TOKEN|COOLIFY_API_TOKEN|FLEET_ENV_OVERRIDE|FLEET_BASTION|FLEET_CALLER_HOST)=' | sort > "$STUB_LOG/bin.$n.env"
if [ "$1" = env-check ]; then
  printf '{\n  "schema_version": 1,\n  "verb": "env-check",\n  "env_from_stdin": true,\n  "names": [\n'
  first=1
  for v in HCLOUD_TOKEN HETZNER_TOKEN GITHUB_TOKEN APIKEY_SERVICE_ADMIN_TOKEN FLEET_SECRETS_ADMIN_TOKEN COOLIFY_API_TOKEN; do
    case " $STUB_BUILDER_HAS " in *" $v "*) r=bootstrap-file ;; *) r=none ;; esac
    [ $first = 1 ] || printf ',\n'
    first=0
    printf '    {\n      "name": "%s",\n      "secret": true,\n      "resolved": "%s"\n    }' "$v" "$r"
  done
  printf '\n  ]\n}\n'
  exit 0
fi
if [ "$1" = --env-from-stdin ]; then
  shift
  : > "$STUB_LOG/bin.$n.block"
  while IFS= read -r line; do
    [ -n "$line" ] || break
    printf '%s\n' "$line" >> "$STUB_LOG/bin.$n.block"
    # the real binary sets these in its own environment (builder's value wins: not modelled)
    export "$line"
  done
fi
env | grep -E '^(HCLOUD_TOKEN|HETZNER_TOKEN|GITHUB_TOKEN|APIKEY_SERVICE_ADMIN_TOKEN|FLEET_SECRETS_ADMIN_TOKEN|COOLIFY_API_TOKEN|FLEET_ENV_OVERRIDE|FLEET_BASTION|FLEET_CALLER_HOST)=' | sort > "$STUB_LOG/bin.$n.env"
# like the real verbs, only read stdin when told to (a plain `deploy` never does)
if [ "$STUB_READ_STDIN" = 1 ]; then cat > "$STUB_LOG/bin.$n.stdin"; fi
exit "${STUB_EXIT:-0}"
"""


def bashes():
    found = []
    for cand in ("/bin/bash", shutil.which("bash")):
        if cand and os.path.exists(cand) and os.path.realpath(cand) not in [os.path.realpath(f) for f in found]:
            found.append(cand)
    return found


class ShimCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shim-test-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.log = self.root / "log"
        self.log.mkdir()
        self.tmpdir = self.root / "tmpdir"
        self.tmpdir.mkdir()
        ssh = self.stubs / "ssh"
        ssh.write_text(STUB_SSH)
        ssh.chmod(0o755)
        fake = self.stubs / "fleet-runner-remote"
        fake.write_text(FAKE_BIN)
        fake.chmod(0o755)
        self.fake = fake

    def env(self, **extra):
        base = {k: v for k, v in os.environ.items()
                if k not in SECRET_VARS and not k.startswith("FLEET_")}
        base.update({
            "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "STUB_LOG": str(self.log),
            "STUB_BIN_KIND": "new",
            "STUB_BUILDER_HAS": "",
            "STUB_READ_STDIN": "0",
            "FLEET_REMOTE_BIN": str(self.fake),
            "FLEET_CALLER_HOST": "laptop-test",
            "TMPDIR": str(self.tmpdir),
        })
        base.update(extra)
        return base

    def run_shim(self, args=("deploy", "go_demo"), stdin="", shim=SHIM, bash=None, timeout=30, **env):
        cmd = ([bash, str(shim)] if bash else [str(shim)]) + list(args)
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=self.env(**env))
        return p

    # recorded artefacts -------------------------------------------------------------------
    def calls(self):
        return int((self.log / "count").read_text()) if (self.log / "count").exists() else 0

    def ssh_argv(self, n):
        return (self.log / f"ssh.{n}.argv").read_text()

    def bin_file(self, n, kind):
        f = self.log / f"bin.{n}.{kind}"
        return f.read_text() if f.exists() else None

    def all_argv_text(self):
        return "".join(self.ssh_argv(n) + (self.bin_file(n, "argv") or "") for n in range(1, self.calls() + 1))

    def assert_no_secret_anywhere_on_a_command_line(self, p, *values):
        argv = self.all_argv_text()
        for v in values:
            self.assertNotIn(v, argv, "secret value reached an ssh/remote command line")
            self.assertNotIn(v, p.stdout, "secret value printed on stdout")
            self.assertNotIn(v, p.stderr, "secret value printed on stderr")
        self.assertEqual(list(self.tmpdir.iterdir()), [], "temp dir not cleaned up")


class ShimNonTTY(ShimCase):
    def each_bash(self, fn):
        for b in bashes():
            with self.subTest(bash=b):
                # fresh log per bash
                shutil.rmtree(self.log)
                self.log.mkdir()
                fn(b)

    def test_no_secret_set_is_one_plain_call(self):
        # bash 3.2 regression too: empty arrays under set -u
        def go(b):
            p = self.run_shim(bash=b, FLEET_BASTION="root@bastion.test")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.calls(), 1)
            argv = self.ssh_argv(1)
            self.assertNotIn("-t\n", argv)
            self.assertNotIn("env-check", argv)
            self.assertNotIn("--env-from-stdin", argv)
            self.assertIn("FLEET_CALLER_HOST=laptop-test", argv)
            self.assertIn("deploy", self.bin_file(1, "argv"))
        self.each_bash(go)

    def test_secret_builder_lacks_goes_on_stdin_only(self):
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN=TOKEN1, GITHUB_TOKEN=TOKEN2, STUB_BUILDER_HAS="GITHUB_TOKEN")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.calls(), 2)  # probe, then the call
            self.assertIn("env-check --json", self.ssh_argv(1))
            self.assertIn("--env-from-stdin", self.ssh_argv(2))
            self.assertEqual(self.bin_file(2, "block"), f"HCLOUD_TOKEN={TOKEN1}\n")
            # the value reached the binary through stdin, became environment, and nowhere on argv
            self.assertIn(f"HCLOUD_TOKEN=", self.bin_file(2, "env") or "")
            self.assertNotIn("GITHUB_TOKEN", self.bin_file(2, "env") or "", "builder already has it: must not be sent")
            self.assert_no_secret_anywhere_on_a_command_line(p, TOKEN1, TOKEN2)
            # the binary was invoked with the marker first, then the verb, as the Go side expects
            self.assertEqual(self.bin_file(2, "argv").split("\n")[:2], ["--env-from-stdin", "deploy"])
        self.each_bash(go)

    def test_builder_has_everything_sends_nothing(self):
        def go(b):
            p = self.run_shim(bash=b, GITHUB_TOKEN=TOKEN1, STUB_BUILDER_HAS="GITHUB_TOKEN")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.calls(), 2)
            self.assertNotIn("--env-from-stdin", self.ssh_argv(2))
            self.assertIsNone(self.bin_file(2, "block"))
            self.assert_no_secret_anywhere_on_a_command_line(p, TOKEN1)
        self.each_bash(go)

    def test_forward_always_sends_and_asks_to_override(self):
        def go(b):
            p = self.run_shim(bash=b, GITHUB_TOKEN=TOKEN1, STUB_BUILDER_HAS="GITHUB_TOKEN", FLEET_ENV_FORWARD_ALWAYS="1")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.bin_file(2, "block"), f"GITHUB_TOKEN={TOKEN1}\nFLEET_ENV_OVERRIDE=GITHUB_TOKEN\n")
            self.assert_no_secret_anywhere_on_a_command_line(p, TOKEN1)
        self.each_bash(go)

    def test_callers_stdin_follows_the_block(self):
        def go(b):
            p = self.run_shim(bash=b, stdin="line one\nline two\n", HCLOUD_TOKEN=TOKEN1, STUB_READ_STDIN="1")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.bin_file(2, "stdin"), "line one\nline two\n")
            self.assertNotIn(TOKEN1, self.bin_file(2, "stdin"))
        self.each_bash(go)

    def test_exit_status_and_cleanup(self):
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN=TOKEN1, STUB_EXIT="7")
            self.assertEqual(p.returncode, 7)
            self.assertEqual(list(self.tmpdir.iterdir()), [])
        self.each_bash(go)

    def test_unclosed_caller_stdin_does_not_hang_the_shim(self):
        def go(b):
            cmd = [b, str(SHIM), "deploy", "x"]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=self.env(HCLOUD_TOKEN=TOKEN1))
            try:
                # p.stdin stays open and silent: a plain `... | ssh` would wait for it forever
                deadline = time.time() + 20
                while p.poll() is None and time.time() < deadline:
                    time.sleep(0.05)
                self.assertIsNotNone(p.poll(), "shim waited for a caller stdin that never closes")
                self.assertEqual(p.returncode, 0)
            finally:
                if p.poll() is None:
                    p.kill()
                p.stdin.close()
                p.stdout.close()
                p.stderr.close()
                p.wait()
            self.assertEqual(list(self.tmpdir.iterdir()), [])
        self.each_bash(go)

    def test_value_with_newline_is_not_sent(self):
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN="FAKE-A\nFAKE-B")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("control character", p.stderr)
            self.assertNotIn("--env-from-stdin", self.ssh_argv(self.calls()))
            self.assert_no_secret_anywhere_on_a_command_line(p, "FAKE-A", "FAKE-B")
        self.each_bash(go)

    def test_name_the_builder_does_not_list_is_not_sent(self):
        # a builder whose allow-list lacks a name would reject the whole block
        fake = self.stubs / "fleet-runner-remote"
        fake.write_text(FAKE_BIN.replace("HCLOUD_TOKEN HETZNER_TOKEN GITHUB_TOKEN", "HETZNER_TOKEN GITHUB_TOKEN", 1))
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN=TOKEN1)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertNotIn("--env-from-stdin", self.ssh_argv(self.calls()))
        self.each_bash(go)

    # Where the fake credential may appear, file by file: the recorded record of every ssh
    # call (argv), of the remote command (argv), and of what the remote binary read from the
    # ssh stdin (the block) and then held in its environment (the binary applies the block
    # with os.Setenv; the environment is not a command line).
    def files_containing(self, value):
        return sorted(f.name for f in self.log.iterdir() if value in f.read_text())

    def test_fake_token_only_ever_in_the_recorded_stdin_new_path(self):
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN=TOKEN1)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.files_containing(TOKEN1), ["bin.2.block", "bin.2.env"])
            self.assertEqual([f for f in self.files_containing(TOKEN1) if f.endswith(".argv")], [])
            self.assertNotIn(TOKEN1, p.stdout + p.stderr)
        self.each_bash(go)

    def test_fake_token_nowhere_in_the_legacy_binary_fallback(self):
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN=TOKEN1, STUB_BIN_KIND="old")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.files_containing(TOKEN1), [], "no argv, stdin or environment record may hold it")
            self.assertNotIn(TOKEN1, p.stdout + p.stderr)
        self.each_bash(go)

    def test_secret_names_match_the_runner_allow_list(self):
        # go_fleet_runner env_stdin.go envBlockSecretNames: the shim must send exactly these and no others.
        text = SHIM.read_text()
        line = [l for l in text.splitlines() if l.startswith("SECRET_NAMES=")][0]
        self.assertEqual(line.split('"')[1].split(), SECRET_VARS)

    # skew ---------------------------------------------------------------------------------
    def test_skew_new_shim_old_binary_sends_no_secret(self):
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN=TOKEN1, STUB_BIN_KIND="old")
            self.assertEqual(p.returncode, 0, p.stderr)  # the verb itself still runs
            self.assertEqual(self.calls(), 2)
            self.assertIn("NOT sending HCLOUD_TOKEN", p.stderr)
            self.assertEqual(p.stderr.count("\n"), 1, "one warning line")
            self.assertNotIn("--env-from-stdin", self.ssh_argv(2))
            self.assertEqual(self.bin_file(2, "argv").split("\n")[0], "deploy")
            self.assert_no_secret_anywhere_on_a_command_line(p, TOKEN1)
        self.each_bash(go)

    def test_skew_new_shim_old_binary_no_secret_set_is_silent_and_unchanged(self):
        def go(b):
            p = self.run_shim(bash=b, STUB_BIN_KIND="old")
            self.assertEqual((p.returncode, p.stderr), (0, ""))
            self.assertEqual(self.calls(), 1)
        self.each_bash(go)

    def test_escape_hatch_puts_secret_on_argv_loudly(self):
        def go(b):
            p = self.run_shim(bash=b, HCLOUD_TOKEN=TOKEN1, FLEET_ALLOW_ARGV_SECRETS="1", STUB_BIN_KIND="old")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.calls(), 1, "no probe in the escape hatch")
            self.assertIn(f"HCLOUD_TOKEN={TOKEN1}", self.ssh_argv(1))
            self.assertIn("FLEET_ALLOW_ARGV_SECRETS=1", p.stderr)
            self.assertIn("visible in ps", p.stderr)
            self.assertNotIn(TOKEN1, p.stderr)
        self.each_bash(go)

    def test_skew_old_shim_new_binary_still_works(self):
        # The frozen pre-change shim puts tokens in `env VAR=value`; a new binary must accept it
        # (that form is exactly what the Go side's legacy test pins).
        p = self.run_shim(shim=LEGACY_SHIM, HCLOUD_TOKEN=TOKEN1, STUB_BIN_KIND="new")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls(), 1)
        self.assertIn("HCLOUD_TOKEN=", self.bin_file(1, "env"))
        self.assertIn(f"HCLOUD_TOKEN={TOKEN1}", self.ssh_argv(1), "baseline: the legacy shim leaks on argv (the bug this fixes)")


class ShimTTY(ShimCase):
    def run_on_pty(self, **env):
        master, slave = pty.openpty()
        p = subprocess.Popen([str(SHIM), "deploy", "go_demo"], stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                             text=True, env=self.env(**env), close_fds=True)
        os.close(slave)
        out = b""
        deadline = time.time() + 30
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            elif p.poll() is not None:
                break
        p.wait(timeout=10)
        err = p.stderr.read()
        p.stderr.close()
        os.close(master)
        return p.returncode, out.decode("utf-8", "replace"), err

    def test_interactive_sends_nothing_secret_and_says_so(self):
        rc, out, err = self.run_on_pty(HCLOUD_TOKEN=TOKEN1, GITHUB_TOKEN=TOKEN2, STUB_BUILDER_HAS="GITHUB_TOKEN")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls(), 2)  # probe (no tty), then the interactive call
        self.assertNotIn("-t\n", self.ssh_argv(1))
        self.assertIn("-t\n", self.ssh_argv(2))
        self.assertNotIn("--env-from-stdin", self.ssh_argv(2))
        self.assertIsNone(self.bin_file(2, "block"))
        self.assertIn("HCLOUD_TOKEN set locally but missing on the builder", err)
        self.assertEqual(err.count("\n"), 1)
        for v in (TOKEN1, TOKEN2):
            self.assertNotIn(v, self.all_argv_text())
            self.assertNotIn(v, out)
            self.assertNotIn(v, err)

    def test_interactive_with_nothing_missing_is_silent(self):
        rc, out, err = self.run_on_pty(GITHUB_TOKEN=TOKEN1, STUB_BUILDER_HAS="GITHUB_TOKEN")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("-t\n", self.ssh_argv(self.calls()))

    def test_interactive_without_secrets_is_one_tty_call(self):
        rc, out, err = self.run_on_pty()
        self.assertEqual((rc, err, self.calls()), (0, "", 1))
        self.assertIn("-t\n", self.ssh_argv(1))


if __name__ == "__main__":
    unittest.main()
