import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("fleet-edge-probe.py")
_spec = importlib.util.spec_from_file_location("fleet_edge_probe", SCRIPT)
fep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fep)

ARGS = types.SimpleNamespace(timeout=5.0, expiry_days=14)
FAR_FUTURE = 4102444800  # 2100-01-01


def svc(**kw):
    base = {
        "id": "demo", "kind": "container", "runtime": "compose",
        "url": "https://demo.example.test", "network_exposure": "gateway-public",
    }
    base.update(kw)
    return base


def run_probe(service, curl_result):
    """Run probe() with DNS/TLS healthy and /health returning curl_result."""
    health_url = service.get("health_url") or service["url"].rstrip("/") + "/health"
    with mock.patch.object(fep, "resolve_a", return_value={"176.9.123.221"}), \
         mock.patch.object(fep, "inspect_cert",
                           return_value=(True, ["demo.example.test"], FAR_FUTURE, "")), \
         mock.patch.object(fep, "curl",
                           return_value=(curl_result[0], health_url, curl_result[1])):
        return fep.probe(service, ARGS)


class HealthBodyTests(unittest.TestCase):
    def test_existing_healthy_bodies_still_pass(self):
        for body in ('{"status":"ok"}', '{"status":"HEALTHY"}', '{"status":"up"}',
                     '{"service":"x"}', '{"version":"1.2.3"}', "ok", "OK", "healthy"):
            self.assertTrue(fep.health_body_ok(body), body)

    def test_ok_true_is_healthy(self):
        self.assertTrue(fep.health_body_ok('{"ok":true}'))
        self.assertTrue(fep.health_body_ok('{"ok": true}'))

    def test_status_success_is_healthy_case_insensitive(self):
        self.assertTrue(fep.health_body_ok('{"status":"success","data":{}}'))
        self.assertTrue(fep.health_body_ok('{"status":"Success"}'))
        self.assertTrue(fep.health_body_ok('{"status":"SUCCESS"}'))

    def test_unhealthy_bodies_stay_down(self):
        for body in ('{"ok":false}', '{"ok":"true"}', '{"ok":1}', '{"status":"error"}',
                     '{"status":"failed"}', '{"status":"degraded"}', "",
                     "<html><title>Welcome</title></html>", "[]", '["ok"]', "null"):
            self.assertFalse(fep.health_body_ok(body), repr(body))


class ProbeVerdictTests(unittest.TestCase):
    def test_ok_true_body_is_healthy(self):
        r = run_probe(svc(), (200, '{"ok":true}'))
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["hard_fail"])

    def test_status_success_body_is_healthy(self):
        r = run_probe(svc(), (200, '{"status":"success","data":{"uptime":1}}'))
        self.assertTrue(r["ok"], r)

    def test_ok_false_is_down(self):
        r = run_probe(svc(), (200, '{"ok":false}'))
        self.assertIn("health_body", r["fails"])
        self.assertTrue(r["hard_fail"])

    def test_http_502_is_down(self):
        r = run_probe(svc(), (502, '{"ok":true}'))
        self.assertIn("health_status", r["fails"])
        self.assertTrue(r["hard_fail"])

    def test_http_410_is_still_down_when_not_sunset(self):
        r = run_probe(svc(), (410, ""))
        self.assertIn("health_status", r["fails"])
        self.assertTrue(r["hard_fail"])

    def test_curl_timeout_is_down(self):
        # drive the real curl() wrapper: subprocess timeout -> (0, url, "")
        with mock.patch.object(fep.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("curl", 5)):
            code, final, body = fep.curl("https://demo.example.test/health", 5.0, True)
        self.assertEqual((code, body), (0, ""))
        r = run_probe(svc(), (code, body))
        self.assertIn("health_status", r["fails"])
        self.assertTrue(r["hard_fail"])

    def test_connection_error_is_down(self):
        r = run_probe(svc(), (0, ""))
        self.assertIn("health_status", r["fails"])
        self.assertTrue(r["hard_fail"])

    def test_dns_failure_is_down(self):
        with mock.patch.object(fep, "resolve_a", return_value=set()):
            r = fep.probe(svc(), ARGS)
        self.assertIn("dns_nxdomain", r["fails"])
        self.assertTrue(r["hard_fail"])


SUNSET = {"status": "sunset", "sunset_at": "2026-09-27", "reason": "retired"}


class LifecycleSkipTests(unittest.TestCase):
    def test_sunset_is_skipped_without_probing(self):
        with mock.patch.object(fep, "probe", side_effect=AssertionError("must not probe")):
            r = fep.probe_or_skip(svc(id="old", lifecycle=SUNSET), ARGS)
        self.assertTrue(r["skipped"])
        self.assertEqual(r["lifecycle"], "sunset")
        self.assertFalse(r["hard_fail"])
        self.assertEqual(r["fails"], [])

    def test_active_null_and_missing_lifecycle_are_probed(self):
        for lc in ({"status": "active"}, None, "garbage", {}):
            with mock.patch.object(fep, "probe", return_value={"probed": True}) as p:
                r = fep.probe_or_skip(svc(lifecycle=lc), ARGS)
            self.assertEqual(r, {"probed": True}, lc)
            p.assert_called_once()
        with mock.patch.object(fep, "probe", return_value={"probed": True}):
            self.assertEqual(fep.probe_or_skip(svc(), ARGS), {"probed": True})

    def test_sunset_service_returning_410_is_not_down_and_stays_visible_in_prom(self):
        results = [
            fep.probe_or_skip(svc(id="old", lifecycle=SUNSET), ARGS),
            run_probe(svc(id="live"), (502, "")),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.prom")
            fep.write_prom(path, results, "vtest")
            text = Path(path).read_text()
        self.assertIn('fleet_edge_probe_hard_fail_total{vantage="vtest"} 1', text)
        self.assertIn('fleet_edge_probe_skipped_total{vantage="vtest"} 1', text)
        self.assertIn('fleet_edge_probe_service_skipped{vantage="vtest",slug="old"', text)
        self.assertIn('lifecycle="sunset"} 1', text)
        self.assertIn('fleet_edge_probe_service_hard_fail{vantage="vtest",slug="old",host="demo.example.test",runtime="compose"} 0', text)
        self.assertIn('fleet_edge_probe_service_hard_fail{vantage="vtest",slug="live",host="demo.example.test",runtime="compose"} 1', text)
        self.assertNotIn('fleet_edge_probe_service_ok{vantage="vtest",slug="old"', text)


class MainEndToEndTests(unittest.TestCase):
    def test_main_reports_skipped_and_exit_code(self):
        reg = [svc(id="old", lifecycle=SUNSET), svc(id="live", url="https://live.example.test")]
        with tempfile.TemporaryDirectory() as d:
            services = os.path.join(d, "services.json")
            Path(services).write_text(json.dumps(reg))

            def fake_probe(s, args):
                return fep._result(s["id"], "live.example.test", "compose", [], {})

            argv = ["fleet-edge-probe", "--services", services, "--json"]
            with mock.patch.object(fep, "probe", side_effect=fake_probe) as p, \
                 mock.patch.object(fep.sys, "argv", argv), \
                 mock.patch("builtins.print") as pr:
                rc = fep.main()
            self.assertEqual(rc, 0)
            self.assertEqual([c.args[0]["id"] for c in p.call_args_list], ["live"])
            report = json.loads(pr.call_args_list[-1].args[0])
            self.assertEqual(report["skipped"], 1)
            self.assertEqual(report["probed"], 1)
            self.assertEqual(report["hard_fail"], 0)

    def test_main_still_exits_1_on_real_failure_next_to_sunset(self):
        reg = [svc(id="old", lifecycle=SUNSET), svc(id="bad", url="https://bad.example.test")]
        with tempfile.TemporaryDirectory() as d:
            services = os.path.join(d, "services.json")
            Path(services).write_text(json.dumps(reg))
            argv = ["fleet-edge-probe", "--services", services, "--json"]
            bad = fep._result("bad", "bad.example.test", "compose", ["health_status"], {})
            with mock.patch.object(fep, "probe", return_value=bad), \
                 mock.patch.object(fep.sys, "argv", argv), \
                 mock.patch("builtins.print"):
                rc = fep.main()
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
