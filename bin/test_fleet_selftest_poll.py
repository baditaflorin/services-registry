import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name("fleet-selftest-poll.py")
_spec = importlib.util.spec_from_file_location("fleet_selftest_poll", SCRIPT)
fsp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fsp)

GOOD = json.dumps({"ok": True, "checks": [{"name": "dns", "pass": True}, {"name": "db", "pass": True}]})
BAD = json.dumps({"ok": False, "checks": [{"name": "dns", "pass": True}, {"name": "db", "pass": False}]})


class H(BaseHTTPRequestHandler):
    routes = {
        "/good/selftest": (200, GOOD),
        "/bad/selftest": (503, BAD),
        "/okfalse/selftest": (200, json.dumps({"ok": False})),
        "/plain/selftest": (200, "fine"),
        "/gone/selftest": (404, "nope"),
        "/gated/selftest": (401, "auth"),
        "/boom/selftest": (500, "<html>oops</html>"),
        "/selftest": (200, GOOD),
    }

    def do_GET(self):  # noqa: N802
        code, body = self.routes.get(self.path, (404, ""))
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


class SelftestPollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), H)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def probe(self, name):
        # selftest_url forces the path to /selftest, so route by host path via health_url prefix:
        # call fetch/classify directly for the local server (https-only eligibility is tested separately).
        status, body, err = fsp.fetch("%s/%s/selftest" % (self.base, name), 3)
        return fsp.classify(status, body)

    def test_states(self):
        self.assertEqual(self.probe("good"), ("pass", [("dns", True), ("db", True)]))
        self.assertEqual(self.probe("bad"), ("fail", [("dns", True), ("db", False)]))
        self.assertEqual(self.probe("okfalse"), ("fail", [("_overall", False)]))
        self.assertEqual(self.probe("plain"), ("pass", [("_overall", True)]))
        self.assertEqual(self.probe("gone"), ("skip", []))
        self.assertEqual(self.probe("gated"), ("gated", []))
        self.assertEqual(self.probe("boom"), ("fail", [("_overall", False)]))

    def test_probe_direct_tries_hosts_in_order(self):
        svc = {"id": "d", "kind": "container", "runtime": "compose", "host_port": self.srv.server_port,
               "url": "https://d.invalid"}
        r = fsp.probe(svc, 2, None, ("127.0.0.2", "127.0.0.1"))  # 127.0.0.2: nothing listens there on the test port
        self.assertEqual((r["via"], r["state"]), ("lan", "pass"))
        self.assertTrue(r["url"].startswith("http://127.0.0.1:"))

    def test_probe_direct_nobody_answers_is_unreachable_not_gated(self):
        svc = {"id": "d", "kind": "container", "runtime": "compose", "host_port": 1, "url": "https://d.invalid"}
        r = fsp.probe(svc, 2, None, ("127.0.0.1",))
        self.assertEqual((r["via"], r["state"]), ("lan", "unreachable"))

    def test_probe_edge_for_non_compose(self):
        svc = {"id": "c", "kind": "container", "runtime": "coolify", "host_port": 1, "url": "https://c.invalid"}
        r = fsp.probe(svc, 2, None, ("127.0.0.1",))
        self.assertEqual(r["via"], "edge")
        self.assertEqual(r["url"], "https://c.invalid/selftest")

    def test_unreachable(self):
        status, body, err = fsp.fetch("http://127.0.0.1:1/selftest", 2)
        self.assertIsNone(status)
        self.assertEqual(fsp.classify(status, body), ("unreachable", []))
        self.assertTrue(err)

    def test_eligible(self):
        ok = {"id": "a", "kind": "container", "url": "https://a.example.test", "runtime": "compose"}
        self.assertTrue(fsp.eligible(ok))
        self.assertFalse(fsp.eligible(dict(ok, kind="static")))
        self.assertFalse(fsp.eligible(dict(ok, url="http://a.example.test")))
        self.assertFalse(fsp.eligible(dict(ok, lifecycle={"status": "sunset"})))
        self.assertFalse(fsp.eligible(dict(ok, runtime="external")))
        self.assertFalse(fsp.eligible(dict(ok, network_exposure="lan-internal")))

    def test_selftest_url_strips_path_query_and_credentials(self):
        s = {"url": "https://u:p@a.example.test:8443/x?api_key=SECRET", "health_url": "https://u:p@a.example.test:8443/health?token=SECRET"}
        self.assertEqual(fsp.selftest_url(s), "https://a.example.test:8443/selftest")
        self.assertEqual(fsp.selftest_url({"url": "https://a.example.test"}), "https://a.example.test/selftest")

    def test_consecutive_failures(self):
        rs = [{"id": "x", "state": "fail"}, {"id": "y", "state": "pass"}, {"id": "z", "state": "unreachable"}, {"id": "w", "state": "gated"}]
        f1 = fsp.update_failures({}, rs)
        self.assertEqual(f1, {"x": 1, "y": 0, "z": 1, "w": 0})
        f2 = fsp.update_failures(f1, rs)
        self.assertEqual(f2, {"x": 2, "y": 0, "z": 2, "w": 0})
        rs[0]["state"] = "pass"
        self.assertEqual(fsp.update_failures(f2, rs)["x"], 0)

    def test_render_prom(self):
        rs = [
            {"id": "good", "state": "pass", "checks": [("dns", True)]},
            {"id": 'we"ird', "state": "fail", "checks": [("db", False)]},
            {"id": "gone", "state": "skip", "checks": []},
        ]
        txt = fsp.render_prom(rs, {"good": 0, 'we"ird': 3, "gone": 0}, 1700000000, 4.2)
        self.assertIn('fleet_selftest_check_pass{service="good",check="dns"} 1', txt)
        self.assertIn('fleet_selftest_check_pass{service="we\\"ird",check="db"} 0', txt)
        self.assertIn('fleet_selftest_service_state{service="gone",state="skip"} 1', txt)
        self.assertIn('fleet_selftest_consecutive_failures{service="we\\"ird"} 3', txt)
        self.assertIn('fleet_selftest_services{state="fail"} 1', txt)
        self.assertIn("fleet_selftest_poll_targets 3", txt)
        self.assertIn("fleet_selftest_poll_last_run_timestamp_seconds 1700000000", txt)
        self.assertNotIn("service=\"gone\",check", txt)

    def test_main_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            reg = os.path.join(d, "services.json")
            json.dump([{"id": "one", "kind": "container", "url": "https://nonexistent.invalid", "runtime": "compose"},
                       {"id": "two", "kind": "static", "url": "https://x.example.test"}], open(reg, "w"))
            prom, state = os.path.join(d, "o.prom"), os.path.join(d, "s.json")
            rc = fsp.main(["--services", reg, "--prom", prom, "--state", state, "--timeout", "2"])
            self.assertEqual(rc, 0)
            txt = open(prom).read()
            self.assertIn('fleet_selftest_service_state{service="one",state="unreachable"} 1', txt)
            self.assertNotIn('service="two"', txt)
            self.assertEqual(json.load(open(state))["failures"], {"one": 1})
            fsp.main(["--services", reg, "--prom", prom, "--state", state, "--timeout", "2"])
            self.assertEqual(json.load(open(state))["failures"], {"one": 2})
            self.assertEqual(oct(os.stat(prom).st_mode)[-3:], "644")

    def test_bad_registry_is_exit_2(self):
        self.assertEqual(fsp.main(["--services", "/nonexistent/services.json"]), 2)


if __name__ == "__main__":
    unittest.main()
