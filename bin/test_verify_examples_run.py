"""bin/verify-examples-run.sh: the script that would run on the builder is executed here against shims (a fake
fleet-runner keystore, a fake curl, a fake `flock`/`shred`) and a local HTTP service, to prove what matters about the key:
it is revoked by slug (never by value) on every exit path, it is on no command line, in no output and in no file once
the run is over, and the revocation is proven or the run exits 3."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIN = Path(__file__).resolve().parent
RUN = BIN / "verify-examples-run.sh"
VERIFY = BIN / "verify-examples.py"
BASH = shutil.which("bash")
GIT = shutil.which("git")

FLEET_RUNNER = r"""#!/bin/sh
# fake keystore: state in $FAKE_STATE. Like the real one, `key revoke <slug>` looks a SCOPE up (ours is the hostnames,
# so a slug finds nothing); `key revoke kid_...` works.
echo "fleet-runner $*" >> "$FAKE_STATE/argv.log"
[ "$1" = key ] || exit 2
case "$2" in
  issue)
    [ -e "$FAKE_STATE/mint-fails" ] && { echo "mint refused" >&2; exit 1; }
    k="FAKEKEY$(date +%s)$$"; echo "$k" > "$FAKE_STATE/key"; rm -f "$FAKE_STATE/revoked"; touch "$FAKE_STATE/minted"
    echo "issued kid_test prefix=${k%????????????}" >&2; printf '%s\n' "$k" ;;
  revoke)
    [ -e "$FAKE_STATE/revoke-fails" ] && { echo "keystore down" >&2; exit 1; }
    case "$3" in
      kid_test) touch "$FAKE_STATE/revoked"; echo "revoked kid_test" ;;
      kid_old) touch "$FAKE_STATE/leftover-revoked"; echo "revoked kid_old" ;;
      kid_*) echo "no-op: $3 was already revoked or unknown" ;;
      *) echo "no active key found for scope \"$3\"" >&2; exit 1 ;;
    esac ;;
  list)
    rows=""
    if [ -e "$FAKE_STATE/leftover" ] && [ ! -e "$FAKE_STATE/leftover-revoked" ]; then
      rows='{"user":"examples-verifier","key_id":"kid_old","created_at":"2026-10-29T00:00:00Z","revoked_at":"","expires_at":"2999-01-01T00:00:00Z"}'
    fi
    if [ -e "$FAKE_STATE/minted" ] && [ ! -e "$FAKE_STATE/revoked" ]; then
      [ -n "$rows" ] && rows="$rows,"
      rows="$rows"'{"user":"examples-verifier","key_id":"kid_test","created_at":"2026-10-30T00:00:00Z","revoked_at":"","expires_at":"2999-01-01T00:00:00Z","key":""}'
    fi
    echo "[$rows]" ;;
esac
"""

CURL = r"""#!/bin/sh
# fake curl for the proof: -H @file carries the key; 401 once the fake keystore revoked it
echo "curl $*" >> "$FAKE_STATE/argv.log"
hdr=""; while [ $# -gt 0 ]; do [ "$1" = -H ] && hdr="$2"; shift; done
[ -n "$hdr" ] && [ -s "${hdr#@}" ] || { printf 000; exit 0; }
if [ -e "$FAKE_STATE/revoked" ] && [ ! -e "$FAKE_STATE/still-accepted" ]; then printf 401; else printf 200; fi
"""

FLOCK = "#!/bin/sh\n[ -e \"$FAKE_STATE/locked\" ] && exit 1\nexit 0\n"
SHRED = "#!/bin/sh\necho \"shred $*\" >> \"$FAKE_STATE/argv.log\"\nfor a in \"$@\"; do [ \"$a\" = -u ] || rm -f \"$a\"; done\n"


class Service(BaseHTTPRequestHandler):
    key_file = None
    slow = threading.Event()
    hit = threading.Event()
    release = threading.Event()

    def do_GET(self):
        Service.hit.set()
        if Service.slow.is_set():
            Service.release.wait(30)
        ok = self.headers.get("X-API-Key", "") == Path(Service.key_file).read_text().strip()
        body = b'{"ok":true}' if ok else b'{"error":"denied"}'
        try:
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):                     # the check was killed on purpose
            pass

    def log_message(self, *a):
        pass


@unittest.skipUnless(BASH and GIT, "bash and git are needed")
class TestRunScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ve-run-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.shims = self.tmp / "shims"
        self.work = self.tmp / "work"
        for d in (self.state, self.shims, self.work):
            d.mkdir()
        for name, body in (("fleet-runner", FLEET_RUNNER), ("curl", CURL), ("flock", FLOCK), ("shred", SHRED)):
            (self.shims / name).write_text(body)
            (self.shims / name).chmod(0o755)
        (self.tmp / "env").write_text("export SOME_ADMIN_TOKEN=not-a-real-token\n")
        Service.key_file = str(self.state / "key")
        Service.slow.clear()
        Service.hit.clear()
        Service.release.clear()
        self.addCleanup(Service.release.set)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        # a registry clone with an origin/main that holds the registry and the checker
        self.repo = self.tmp / "repo"
        (self.repo / "bin").mkdir(parents=True)
        shutil.copy(VERIFY, self.repo / "bin" / "verify-examples.py")
        svc = [{"id": "svc-a", "kind": "container", "runtime": "compose", "host_port": self.port,
                "url": "https://svc-a.example.test", "example_path": "/?q=1"},
               {"id": "svc-b", "kind": "container", "runtime": "compose", "host_port": self.port,
                "url": "https://svc-b.example.test", "example_path": "/?q=2"}]
        (self.repo / "services.json").write_text(json.dumps(svc))
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                        check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git("add", "-A")
        git("commit", "-q", "-m", "x")
        git("update-ref", "refs/remotes/origin/main", "HEAD")

    def remote_script(self, *args):
        r = subprocess.run([BASH, str(RUN), "--print-remote", *args], capture_output=True, text=True, check=True)
        return r.stdout

    def run_remote(self, *args, timeout=60, signal_when_hit=None):
        env = dict(os.environ, PATH=f"{self.shims}:{os.environ['PATH']}", FAKE_STATE=str(self.state),
                   VERIFY_EXAMPLES_REPO=str(self.repo), VERIFY_EXAMPLES_ENVFILE=str(self.tmp / "env"),
                   VERIFY_EXAMPLES_WORKBASE=str(self.work), VERIFY_EXAMPLES_LOCKFILE=str(self.tmp / "lock"), VERIFY_EXAMPLES_PAUSE="0.1")
        script = self.remote_script("--direct-hosts", "127.0.0.1", *args)
        (self.tmp / "remote.sh").write_text(script)
        with open(self.tmp / "remote.sh") as stdin:                         # stdin is the script, as with `ssh ... bash -s`
            p = subprocess.Popen([BASH, "-s"], stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=env, start_new_session=True)
        if signal_when_hit:
            self.assertTrue(Service.hit.wait(30), "the check never reached the service")
            os.kill(p.pid, signal_when_hit)
        out, err = p.communicate(timeout=timeout)
        self.out, self.err, self.rc = out, err, p.returncode
        return p.returncode

    def key(self):
        f = self.state / "key"
        return f.read_text().strip() if f.exists() else ""

    def argv_log(self):
        f = self.state / "argv.log"
        return f.read_text() if f.exists() else ""

    def assert_clean(self):
        """No key in stdout, stderr, any command line, any file left behind."""
        key = self.key()
        self.assertTrue(key.startswith("FAKEKEY"), "the shim should have issued a key")
        for what, text in (("stdout", self.out), ("stderr", self.err), ("argv", self.argv_log())):
            self.assertNotIn(key, text, what)
        self.assertEqual(list(self.work.iterdir()), [], "the work directory (key, header file) must be gone")

    def test_happy_path_revokes_by_slug_proves_401_and_leaves_nothing(self):
        self.assertEqual(self.run_remote(), 0, self.err)
        self.assert_clean()
        doc = json.loads(self.out.split("===VERIFY-EXAMPLES-JSON-BEGIN===\n")[1].split("===VERIFY-EXAMPLES-JSON-END===")[0])
        self.assertEqual({k: v["state"] for k, v in doc["services"].items()}, {"svc-a": "ok", "svc-b": "ok"})
        log = self.argv_log()
        self.assertIn("fleet-runner key revoke kid_test", log)                # by key id: a slug would find no scope
        self.assertNotIn("key revoke examples-verifier", log)
        self.assertIn("--ttl 1h --use-limit 54", log)                       # 2 services * 2 + 50
        scope = log.split("--scope ")[1].split(" ")[0]
        self.assertEqual(scope, "svc-a.example.test,svc-b.example.test")
        self.assertIn("--user examples-verifier", log)
        self.assertIn("refusal is ok", self.err)
        self.assertTrue((self.state / "revoked").exists())

    def test_the_key_is_never_a_command_line_argument_of_any_process(self):
        self.run_remote()
        for line in self.argv_log().splitlines():
            self.assertNotIn("FAKEKEY", line)
            self.assertNotIn("X-API-Key", line)

    def test_a_failing_run_is_still_revoked_and_cleaned(self):
        src = VERIFY.read_text().replace('if __name__ == "__main__":', 'def main(argv=None):\n    return 7\n\n\nif __name__ == "__main__":')
        (self.repo / "bin" / "verify-examples.py").write_text(src)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "break"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        self.assertEqual(self.run_remote(), 7)
        self.assert_clean()
        self.assertTrue((self.state / "revoked").exists())
        self.assertNotIn("===VERIFY-EXAMPLES-JSON-BEGIN===", self.out)

    def test_a_term_signal_mid_run_revokes_at_once(self):
        Service.slow.set()
        rc = self.run_remote(signal_when_hit=signal.SIGTERM, timeout=40)
        self.assertEqual(rc, 143)
        self.assert_clean()
        self.assertTrue((self.state / "revoked").exists(), self.err)

    def test_a_revoke_that_fails_exits_3_and_says_so(self):
        (self.state / "revoke-fails").touch()
        self.assertEqual(self.run_remote(), 3)
        self.assertIn("NOT SHOWN REVOKED", self.err)
        self.assertEqual(list(self.work.iterdir()), [])                     # files are shredded all the same

    def test_a_key_the_gateway_still_accepts_is_not_a_proof(self):
        (self.state / "still-accepted").touch()
        self.assertEqual(self.run_remote(), 3)
        self.assertIn("unproven", self.err)

    def test_a_failed_mint_is_revoked_by_slug_and_runs_nothing(self):
        (self.state / "mint-fails").touch()
        rc = self.run_remote()
        self.assertEqual(rc, 1)
        self.assertIn("key list", self.argv_log())                          # minted-or-not, the keystore is asked for active keys
        self.assertNotIn("===VERIFY-EXAMPLES-JSON-BEGIN===", self.out)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_a_leftover_active_key_is_revoked_before_minting(self):
        (self.state / "leftover").touch()
        self.assertEqual(self.run_remote(), 0, self.err)
        log = self.argv_log().splitlines()
        first_revoke = next(i for i, l in enumerate(log) if "key revoke kid_old" in l)
        mint = next(i for i, l in enumerate(log) if "key issue" in l)
        self.assertLess(first_revoke, mint)
        self.assertIn("leftover", self.err)

    def test_dry_mints_and_runs_nothing(self):
        self.assertEqual(self.run_remote("--dry"), 0, self.err)
        self.assertNotIn("key issue", self.argv_log())
        self.assertNotIn("key revoke", self.argv_log())
        self.assertIn("DRY RUN", self.err)
        self.assertIn("use limit 54", self.err)
        self.assertEqual(self.out, "")

    def test_only_narrows_scope_and_limit_and_an_unknown_id_stops_before_the_mint(self):
        self.assertEqual(self.run_remote("--only", "svc-a"), 0, self.err)
        log = self.argv_log()
        self.assertEqual(log.split("--scope ")[1].split(" ")[0], "svc-a.example.test")
        self.assertIn("--use-limit 52", log)
        (self.state / "argv.log").unlink()
        self.assertEqual(self.run_remote("--only", "svc-zzz"), 1)
        self.assertNotIn("key issue", self.argv_log())

    def test_a_second_run_stops_at_the_lock(self):
        (self.state / "locked").touch()
        self.assertEqual(self.run_remote(), 1)
        self.assertIn("holds the lock", self.err)
        self.assertNotIn("key issue", self.argv_log())

    def test_the_local_side_rejects_what_would_become_shell_text(self):
        for args in (["--only", "a;rm -rf /"], ["--ref", "x y"], ["--direct-hosts", "1.2.3.4,5.6.7.8,9.9.9.9"], ["--bogus"]):
            r = subprocess.run([BASH, str(RUN), "--dry", *args], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
