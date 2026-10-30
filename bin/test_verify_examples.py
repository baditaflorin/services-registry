import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "verify-examples.py"
_spec = importlib.util.spec_from_file_location("verify_examples", SCRIPT)
ve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ve)


class TestClassify(unittest.TestCase):
    def test_states(self):
        c = ve.classify
        self.assertEqual(c(None, ""), "unreachable")
        self.assertEqual(c(200, "{}"), "ok")
        self.assertEqual(c(302, ""), "ok")
        self.assertEqual(c(401, ""), "gated")
        self.assertEqual(c(403, ""), "gated")
        self.assertEqual(c(404, ""), "not-found")
        self.assertEqual(c(405, ""), "method")
        self.assertEqual(c(500, ""), "error")
        self.assertEqual(c(429, ""), "error")

    def test_400_tells_a_missing_input_from_a_refused_one(self):
        body = '{"status":"error","error":{"code":400,"message":"missing required query parameter \'q\'"}}'
        self.assertEqual(ve.classify(400, body), "missing-param")
        self.assertEqual(ve.classify(422, "url is required"), "missing-param")
        self.assertEqual(ve.classify(400, '{"error":"blocked target"}'), "rejected")
        self.assertEqual(ve.classify(400, ""), "rejected")


SVC = {"id": "x", "kind": "container", "runtime": "compose", "host_port": 8123, "url": "https://x.0crawl.com",
       "example_path": "/?q=cats"}


class TestProbe(unittest.TestCase):
    def test_lan_first_host_that_answers_wins_and_key_is_sent(self):
        calls = []

        def fake(url, timeout, key):
            calls.append((url, key))
            return (None, "", "refused") if "10.0.0.1" in url else (200, "{}", "")

        with mock.patch.object(ve, "fetch", fake):
            sid, r = ve.probe(SVC, 5, "k", ("10.0.0.1", "10.0.0.2"))
        self.assertEqual(sid, "x")
        self.assertEqual([c[0] for c in calls], ["http://10.0.0.1:8123/?q=cats", "http://10.0.0.2:8123/?q=cats"])
        self.assertEqual((r["state"], r["http"], r["via"]), ("ok", 200, "lan"))

    def test_no_route_and_no_key_is_unprobed_and_makes_no_call(self):
        svc = dict(SVC, runtime="coolify", host_port=None)
        with mock.patch.object(ve, "fetch", side_effect=AssertionError("must not call")):
            _, r = ve.probe(svc, 5, None, ("10.0.0.1",))
        self.assertEqual(r["state"], "unprobed")

    def test_edge_used_only_with_a_key(self):
        svc = dict(SVC, runtime="coolify", host_port=None)
        with mock.patch.object(ve, "fetch", return_value=(404, "", "")) as f:
            _, r = ve.probe(svc, 5, "k", ())
        f.assert_called_once_with("https://x.0crawl.com/?q=cats", 5, "k")
        self.assertEqual((r["state"], r["via"]), ("not-found", "edge"))

    def test_example_without_a_path_is_unprobed(self):
        _, r = ve.probe(dict(SVC, example_path=""), 5, "k", ("h",))
        self.assertEqual(r["state"], "unprobed")


class TestOutput(unittest.TestCase):
    def test_render_is_sorted_and_never_holds_the_key(self):
        res = {"b": {"path": "/", "http": 200, "state": "ok", "via": "lan", "ms": 1, "error": ""},
               "a": {"path": "/", "http": 401, "state": "gated", "via": "lan", "ms": 1, "error": ""}}
        out = json.loads(ve.render(res, now="2026-10-30T00:00:00Z"))
        self.assertEqual(list(out["services"]), ["a", "b"])
        self.assertEqual(ve.tally(res), {"gated": 1, "ok": 1})

    def test_redirects_are_not_followed_so_the_key_stays_on_the_host(self):
        self.assertIsNone(ve._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil/"))

    def test_eligible_skips_lan_internal_served_by_and_sunset(self):
        self.assertTrue(ve.eligible(SVC))
        self.assertFalse(ve.eligible(dict(SVC, network_exposure="lan-internal")))
        self.assertFalse(ve.eligible(dict(SVC, served_by="other")))
        self.assertFalse(ve.eligible(dict(SVC, lifecycle={"status": "sunset"})))


if __name__ == "__main__":
    unittest.main()
