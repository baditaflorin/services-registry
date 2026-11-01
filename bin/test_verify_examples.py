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
        self.assertEqual(c(404, "404 page not found\n"), "not-found")
        self.assertEqual(c(404, ""), "not-found")
        self.assertEqual(c(404, '{"status":"no_data","error":{"code":404}}'), "no-data")
        self.assertEqual(c(404, "[1]"), "not-found")
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


class TestDetail(unittest.TestCase):
    def test_detail_names_the_reason(self):
        self.assertEqual(ve.detail('{"status":"error","error":{"code":400,"message":"missing required query parameter \'q\'"}}'),
                         "missing required query parameter 'q'")
        self.assertEqual(ve.detail('{"error":"blocked"}'), "blocked")
        self.assertEqual(ve.detail("404 page not found\n"), "404 page not found")
        self.assertEqual(len(ve.detail("x" * 500)), 120)
        self.assertEqual(ve.detail(""), "")

    def test_only_a_failed_call_carries_a_detail(self):
        with mock.patch.object(ve, "fetch", return_value=(200, "{}", "")):
            self.assertNotIn("detail", ve.probe(SVC, 5, "k", ("h",))[1])
        with mock.patch.object(ve, "fetch", return_value=(404, '{"status":"no_data"}', "")):
            r = ve.probe(SVC, 5, "k", ("h",))[1]
        self.assertEqual((r["state"], r["detail"]), ("no-data", "no_data"))


class TestProbe(unittest.TestCase):
    def answers(self, table):
        """fetch() fake: {host: (status, body, err)}; records every call as (url, key)."""
        calls = []

        def fake(url, timeout, key):
            calls.append((url, key))
            host = url.split("//")[1].split(":")[0]
            return table[host]

        return fake, calls

    def test_one_answering_host_is_the_old_row_and_every_host_is_called_with_the_key(self):
        fake, calls = self.answers({"10.0.0.1": (None, "", "URLError: refused"), "10.0.0.2": (200, "{}", "")})
        with mock.patch.object(ve, "fetch", fake):
            sid, r = ve.probe(SVC, 5, "k", ("10.0.0.1", "10.0.0.2"))
        self.assertEqual(sid, "x")
        self.assertEqual(calls, [("http://10.0.0.1:8123/?q=cats", "k"), ("http://10.0.0.2:8123/?q=cats", "k")])
        self.assertEqual((r["state"], r["http"], r["via"], r["error"]), ("ok", 200, "lan", ""))
        self.assertNotIn("replicas", r)
        self.assertNotIn("diverged", r)

    def test_a_host_that_answers_first_no_longer_hides_the_second(self):
        fake, calls = self.answers({"a": (200, "{}", ""), "b": (200, "{}", "")})
        with mock.patch.object(ve, "fetch", fake):
            _, r = ve.probe(SVC, 5, "k", ("a", "b"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(r["replicas"], [{"host": "a", "http": 200, "state": "ok"}, {"host": "b", "http": 200, "state": "ok"}])
        self.assertEqual(r["state"], "ok")
        self.assertNotIn("diverged", r)

    def test_replicas_that_differ_diverge_and_the_state_is_the_worst(self):
        cases = [
            # (first replica, second replica, expected state, expected http, who is the worst)
            ((200, "{}"), (500, '{"error":"db down"}'), "error", 500, "b"),
            ((404, "404 page not found\n"), (200, "{}"), "not-found", 404, "a"),           # a stale replica without the route
            ((200, "{}"), (404, '{"status":"no_data"}'), "no-data", 404, "b"),
            ((401, ""), (200, "{}"), "gated", 401, "a"),                                    # not measured outranks ok
            ((400, "bad"), (404, "404 page not found"), "not-found", 404, "b"),
            ((503, ""), (500, ""), "error", 503, "a"),                                      # same state: the first host
        ]
        for first, second, state, http, who in cases:
            fake, _ = self.answers({"a": first + ("",), "b": second + ("",)})
            with mock.patch.object(ve, "fetch", fake):
                _, r = ve.probe(SVC, 5, "k", ("a", "b"))
            self.assertEqual((r["state"], r["http"]), (state, http), (first, second))
            self.assertEqual([c["host"] for c in r["replicas"]], ["a", "b"])
            self.assertEqual(r.get("diverged"), True if ve.classify(*first) != ve.classify(*second) else None, (first, second))
            self.assertEqual(max(r["replicas"], key=lambda c: ve.severity(c["state"]))["state"], state)
            self.assertEqual(r["replicas"][0 if who == "a" else 1]["state"], state, (first, second))

    def test_the_detail_and_error_are_the_worst_replicas(self):
        fake, _ = self.answers({"a": (200, "{}", ""), "b": (500, '{"error":"db down"}', "")})
        with mock.patch.object(ve, "fetch", fake):
            _, r = ve.probe(SVC, 5, "k", ("a", "b"))
        self.assertEqual((r["state"], r["detail"], r["diverged"]), ("error", "db down", True))

    def test_a_host_that_does_not_answer_is_not_a_replica_but_three_hosts_are_all_called(self):
        fake, calls = self.answers({"a": (200, "{}", ""), "b": (None, "", "refused"), "c": (500, "", "")})
        with mock.patch.object(ve, "fetch", fake):
            _, r = ve.probe(SVC, 5, "k", ("a", "b", "c"))
        self.assertEqual(len(calls), 3)
        self.assertEqual([c["host"] for c in r["replicas"]], ["a", "c"])
        self.assertEqual((r["state"], r["diverged"]), ("error", True))

    def test_nobody_answering_is_unreachable_with_the_last_reason_and_no_replicas(self):
        fake, _ = self.answers({"a": (None, "", "first"), "b": (None, "", "last")})
        with mock.patch.object(ve, "fetch", fake):
            _, r = ve.probe(SVC, 5, "k", ("a", "b"))
        self.assertEqual((r["state"], r["http"], r["error"]), ("unreachable", None, "last"))
        self.assertNotIn("replicas", r)
        self.assertNotIn("detail", r)

    def test_the_key_goes_only_to_the_listed_hosts_on_the_services_own_port_and_never_to_a_redirect_target(self):
        fake, calls = self.answers({"10.0.0.1": (200, "{}", ""), "10.0.0.2": (200, "{}", "")})
        with mock.patch.object(ve, "fetch", fake):
            ve.probe(dict(SVC, url="https://x.0crawl.com"), 5, "secret-key", ("10.0.0.1", "10.0.0.2"))
        self.assertEqual({u.split("//")[1].split("/")[0] for u, _ in calls}, {"10.0.0.1:8123", "10.0.0.2:8123"})
        self.assertTrue(all(k == "secret-key" for _, k in calls))
        # a real redirect from a replica is refused, so the key cannot follow it to another host
        import io
        import urllib.error
        err = urllib.error.HTTPError("http://10.0.0.1:8123/", 302, "Found", {}, io.BytesIO(b""))
        with mock.patch.object(ve._opener, "open", side_effect=err) as o:
            status, _, _ = ve.fetch("http://10.0.0.1:8123/", 5, "secret-key")
        self.assertEqual(status, 302)
        self.assertEqual(o.call_count, 1)

    def test_state_order_knows_every_state_classify_can_return(self):
        seen = {ve.classify(s, b) for s, b in [(None, ""), (200, ""), (401, ""), (404, "x"), (404, "{}"), (405, ""), (400, "missing"),
                                                (400, "no"), (500, "")]}
        self.assertEqual(seen - set(ve.WORST_FIRST), set())
        self.assertEqual(ve.WORST_FIRST[0], "ok")
        self.assertEqual(ve.severity("something-new"), len(ve.WORST_FIRST))

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

    def test_replicas_and_diverged_are_written_and_summarised(self):
        res = {"a": {"path": "/", "http": 500, "state": "error", "via": "lan", "ms": 1, "error": "", "detail": "x",
                     "replicas": [{"host": "h1", "http": 200, "state": "ok"}, {"host": "h2", "http": 500, "state": "error"}],
                     "diverged": True},
               "b": {"path": "/", "http": 200, "state": "ok", "via": "lan", "ms": 1, "error": "",
                     "replicas": [{"host": "h1", "http": 200, "state": "ok"}, {"host": "h2", "http": 200, "state": "ok"}]},
               "c": {"path": "/", "http": 200, "state": "ok", "via": "lan", "ms": 1, "error": ""}}
        out = json.loads(ve.render(res, now="2026-10-30T00:00:00Z"))
        self.assertTrue(out["services"]["a"]["diverged"])
        self.assertEqual(len(out["services"]["b"]["replicas"]), 2)
        self.assertEqual(ve.tally(res), {"error": 1, "ok": 2})
        self.assertEqual(ve.replica_summary(res), (2, ["a"]))

    def test_redirects_are_not_followed_so_the_key_stays_on_the_host(self):
        self.assertIsNone(ve._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil/"))

    def test_eligible_skips_lan_internal_served_by_and_sunset(self):
        self.assertTrue(ve.eligible(SVC))
        self.assertFalse(ve.eligible(dict(SVC, network_exposure="lan-internal")))
        self.assertFalse(ve.eligible(dict(SVC, served_by="other")))
        self.assertFalse(ve.eligible(dict(SVC, lifecycle={"status": "sunset"})))


if __name__ == "__main__":
    unittest.main()
