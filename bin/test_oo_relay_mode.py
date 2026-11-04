"""Tests for `bin/oo` relay mode, against a fake relay on localhost.

No OpenObserve and no credential is involved: the token is a random value made
for the test. The bastion variant runs the remote script locally through stub
`ssh` and `pct` commands, which checks its quoting.
"""
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

OO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oo")
BASE_US = 1762171200000000


def make_handler(token, seen):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.headers.get("Authorization") != "Bearer " + token:
                self._send(401, b"unauthorized")
                return
            seen.append(body)
            if body.get("container_name") == "boom":
                out = {"status": "unavailable", "error": "OpenObserve query unavailable; this is not an empty result"}
            else:
                rows = [
                    {
                        "timestamp_unix_micros": BASE_US + i * 1_000_000,
                        "container_name": body["container_name"],
                        "host": body.get("host", "h1"),
                        "message": "line %d %s" % (i, body.get("contains", "")),
                    }
                    for i in range(3)
                ]
                if body["order"] == "desc":
                    rows.reverse()
                out = {"status": "ok", "count": 3, "limit": body["limit"], "truncated": False, "rows": rows}
            self._send(200, json.dumps(out).encode())

        def _send(self, code, payload):
            self.send_response(code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


class RelayModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.token = secrets.token_hex(32)
        cls.token_file = os.path.join(cls.tmp, "token")
        with open(cls.token_file, "w") as f:
            f.write(cls.token + "\n")
        os.chmod(cls.token_file, 0o600)
        cls.seen = []
        cls.server = HTTPServer(("127.0.0.1", 0), make_handler(cls.token, cls.seen))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        stubs = os.path.join(cls.tmp, "stubs")
        os.mkdir(stubs)
        for name, body in {
            "ssh": '#!/usr/bin/env bash\nshift\nexec bash -c "$1"\n',
            "pct": '#!/usr/bin/env bash\nshift 2\n[[ "$1" == "--" ]] && shift\nexec "$@"\n',
        }.items():
            path = os.path.join(stubs, name)
            with open(path, "w") as f:
                f.write(body)
            os.chmod(path, 0o755)
        cls.stubs = stubs

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_oo(self, *args, **env):
        full = {k: v for k, v in os.environ.items() if not k.startswith(("OO_", "OPENOBSERVE_"))}
        full["OO_RELAY_URL"] = "http://127.0.0.1:%d" % self.port
        full["OO_RELAY_TOKEN_FILE"] = self.token_file
        full.update(env)
        full = {k: v for k, v in full.items() if v is not None}
        return subprocess.run(["bash", OO, *args], capture_output=True, text=True, env=full, timeout=60)

    def test_logs_shows_oldest_first_and_sends_the_filters(self):
        self.seen.clear()
        r = self.run_oo("logs", "domainscope-api", "--host", "0docker_docker_prod", "--grep", "[ext-traits-backfill]", "--since", "3d", "--limit", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("2025-11-03 12:00:00.000 [0docker_docker_prod] line 0 [ext-traits-backfill]", lines[0])
        sent = self.seen[-1]
        self.assertEqual(sent["container_name"], "domainscope-api")
        self.assertEqual(sent["host"], "0docker_docker_prod")
        self.assertEqual(sent["contains"], "[ext-traits-backfill]")
        self.assertEqual(sent["limit"], 3)
        self.assertEqual(sent["order"], "desc")
        self.assertEqual(set(sent), {"container_name", "host", "contains", "start", "end", "limit", "order"})

    def test_logs_absolute_window_is_sent_oldest_first(self):
        self.seen.clear()
        r = self.run_oo(
            "logs", "domainscope-api", "--host", "0docker_docker_prod",
            "--from", "2026-11-03T12:00:00Z", "--to", "2026-11-03T18:30:00Z",
            "--grep", "[ext-traits-backfill]", "--limit", "500",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        sent = self.seen[-1]
        self.assertEqual(sent["start"], "2026-11-03T12:00:00.000000Z")
        self.assertEqual(sent["end"], "2026-11-03T18:30:00.000000Z")
        self.assertEqual(sent["order"], "asc")
        self.assertEqual(sent["limit"], 500)
        # asc rows are printed as received (oldest first), not reversed
        self.assertTrue(r.stdout.splitlines()[0].startswith("2025-11-03 12:00:00.000"))
        self.assertEqual(self.run_oo("logs", "c1", "--to", "2026-11-03T18:30:00Z").returncode, 1)
        r = self.run_oo("logs", "c1", "--from", "last tuesday")
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid time", r.stderr)

    def test_grep_needs_a_container(self):
        r = self.run_oo("grep", "foo")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--container", r.stderr)

    def test_grep_with_container(self):
        r = self.run_oo("grep", "foo", "--container", "c1", "--limit", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[h1/c1]", r.stdout)

    def test_since_redeploy_and_context(self):
        r = self.run_oo("since-redeploy", "c1", "--limit", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("earliest line found at 2025-11-03T12:00:00", r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 3)
        r = self.run_oo("context", "c1", "2025-11-03T12:00:01Z", "--limit", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 3)

    def test_sql_commands_are_refused(self):
        for cmd in (["query", "SELECT 1"], ["errors"], ["containers"], ["hosts"], ["summary"], ["tail", "c1"]):
            r = self.run_oo(*cmd)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn("relay", r.stderr)

    def test_relay_failure_is_an_error_not_zero_rows(self):
        r = self.run_oo("logs", "boom")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unavailable", r.stderr)
        self.assertNotIn("no matching", r.stderr)

    def test_wrong_token_is_an_error(self):
        other = os.path.join(self.tmp, "other")
        with open(other, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(other, 0o600)
        r = self.run_oo("logs", "c1", OO_RELAY_TOKEN_FILE=other)
        self.assertEqual(r.returncode, 1)
        self.assertIn("error:", r.stderr)

    def test_token_file_must_be_private_and_well_formed(self):
        loose = os.path.join(self.tmp, "loose")
        with open(loose, "w") as f:
            f.write(self.token)
        os.chmod(loose, 0o644)
        r = self.run_oo("logs", "c1", OO_RELAY_TOKEN_FILE=loose)
        self.assertEqual(r.returncode, 1)
        self.assertIn("mode 600", r.stderr)
        bad = os.path.join(self.tmp, "bad")
        with open(bad, "w") as f:
            f.write("not-a-token\n")
        os.chmod(bad, 0o600)
        r = self.run_oo("logs", "c1", OO_RELAY_TOKEN_FILE=bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn("64-hex", r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(bad).st_mode), 0o600)

    def test_bad_inputs_never_reach_the_relay(self):
        self.seen.clear()
        for args in (["logs", "c1", "--since", "5x"], ["logs", "c1", "--limit", "abc"]):
            r = self.run_oo(*args)
            self.assertEqual(r.returncode, 1, args)
        self.assertEqual(self.seen, [])

    def test_relay_url_must_be_an_origin(self):
        r = self.run_oo("logs", "c1", OO_RELAY_URL="http://127.0.0.1:1/v1/logs")
        self.assertEqual(r.returncode, 1)
        self.assertIn("OO_RELAY_URL", r.stderr)

    def test_bastion_mode_reads_the_token_on_the_remote_side(self):
        self.seen.clear()
        path = self.stubs + os.pathsep + os.environ["PATH"]
        r = self.run_oo(
            "logs", "c1", "--limit", "3", "--grep", "it's \"q\" $(touch /tmp/oo-relay-injected)",
            PATH=path, OO_RELAY_VIA="bastion", OO_RELAY_REMOTE_TOKEN_FILE=self.token_file,
            OO_RELAY_SOCKS="", OO_RELAY_TOKEN_FILE=None,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.seen[-1]["contains"], "it's \"q\" $(touch /tmp/oo-relay-injected)")
        self.assertFalse(os.path.exists("/tmp/oo-relay-injected"))

    def test_without_relay_url_the_old_credentials_are_still_required(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OO_", "OPENOBSERVE_"))}
        r = subprocess.run(["bash", OO, "logs", "c1"], capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("OPENOBSERVE_USER", r.stderr)


if __name__ == "__main__":
    unittest.main()
