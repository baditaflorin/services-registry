import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sync-service-params.py"
_spec = importlib.util.spec_from_file_location("sync_service_params", SCRIPT)
ssp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssp)


def P(name, required=False, **kw):
    return {"name": name, "required": required, "type": "string", "description": None, "example": None, "default": None, **kw}


def F(params=(), test_url="", endpoint="/", method="GET", repo="go_x", sid="x"):
    return {"id": sid, "repo": repo, "test_url": test_url, "api": {"endpoint": endpoint, "method": method}, "params": list(params)}


class TestSanitise(unittest.TestCase):
    def s(self, raw, repo="go_x"):
        return ssp.sanitise_path(raw, repo)

    def test_token_prefix_service_segment_and_api_key_are_stripped(self):
        self.assertEqual(self.s("/t/default_token/?target=https://example.com"), "/?target=https://example.com")
        self.assertEqual(self.s("/t/default_token/go_x?domain=zonetransfer.me"), "/?domain=zonetransfer.me")
        self.assertEqual(self.s("/t/YOUR_CALLER_TOKEN/go-x?target=a"), "/?target=a")
        self.assertEqual(self.s("/go_x?api_key=default_token&target=google.com"), "/?target=google.com")
        self.assertEqual(self.s("/?url=https://example.com&api_key=default_token"), "/?url=https://example.com")
        self.assertEqual(self.s("/?API_KEY=k1&a=1&apikey=k2&b=2"), "/?a=1&b=2")
        self.assertEqual(self.s("/t/default_token/"), "/")
        self.assertEqual(self.s("/t/default_token"), "/")

    def test_a_real_path_is_kept_with_order_and_raw_encoding(self):
        self.assertEqual(self.s("/subdomains?domain=a.com&limit=5"), "/subdomains?domain=a.com&limit=5")
        self.assertEqual(self.s("/go_x/programs?api_key=default_token"), "/programs")
        self.assertEqual(self.s("/other/go_x?a=1"), "/other/go_x?a=1")               # only the FIRST segment is the service's own
        self.assertEqual(self.s("/?text=%7B%22a%22%3A1%7D&from=json"), "/?text=%7B%22a%22%3A1%7D&from=json")

    def test_things_that_are_not_a_path_are_refused(self):
        for bad in ("", "https://x.example/?a=1", "//x.example/a", "?a=1", "/a b", None):
            self.assertIsNone(self.s(bad), bad)


class TestCarries(unittest.TestCase):
    def test_every_name_needs_a_non_empty_value(self):
        self.assertTrue(ssp.carries("/?a=1&b=x", ["a", "b"]))
        self.assertFalse(ssp.carries("/?a=1&b=", ["a", "b"]))
        self.assertFalse(ssp.carries("/?a=1", ["a", "b"]))
        self.assertFalse(ssp.carries("/", ["a"]))
        self.assertTrue(ssp.carries("/", []))


class TestParamValue(unittest.TestCase):
    def test_example_then_default_then_table_then_nothing(self):
        self.assertEqual(ssp.param_value(P("domain", example="a.org", default="b.org")), ("a.org", "example"))
        self.assertEqual(ssp.param_value(P("domain", default="b.org")), ("b.org", "default"))
        self.assertEqual(ssp.param_value(P("domain")), ("example.com", "table"))
        self.assertEqual(ssp.param_value(P("q")), ("anthropic claude", "table"))
        self.assertEqual(ssp.param_value(P("port", example=22)), ("22", "example"))
        self.assertIsNone(ssp.param_value(P("program"))[0])
        self.assertIsNone(ssp.param_value(P("class"))[0])

    def test_credentials_never_get_a_value(self):
        self.assertIsNone(ssp.param_value(P("api_key", example="abc"))[0])
        self.assertIsNone(ssp.param_value(P("secret"))[0])
        self.assertIsNone(ssp.param_value(P("token", example="sk_live_abcdefghijklmnop"))[0])
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        self.assertIsNone(ssp.param_value(P("jwt", example=jwt))[0])
        self.assertTrue(ssp.is_secretish("x", "fb_" + "a" * 32))
        self.assertTrue(ssp.is_secretish("x", "a" * 40))
        self.assertFalse(ssp.is_secretish("target", "https://example.com"))


class TestDecide(unittest.TestCase):
    """The rules in the order the task fixed them."""

    def d(self, facts, current):
        return ssp.decide(facts, current)

    def test_a_an_example_that_carries_every_required_param_is_never_replaced(self):
        f = F([P("domain", True)], test_url="/?domain=other.org")
        r = self.d(f, "/?domain=example.com")
        self.assertEqual((r["rule"], r["example_path"]), ("a", None))
        r = self.d(F([P("q", False)]), "/?url=https://example.com")   # nothing required and no test.url: untouched
        self.assertEqual((r["rule"], r["example_path"]), ("a", None))
        r = self.d(F([P("q", False)], test_url="/?q=zzz"), "/?q=other")   # sends a declared param already: untouched
        self.assertEqual((r["rule"], r["example_path"]), ("a", None))
        r = self.d(F([P("q", False)], test_url="/health"), "/?url=https://example.com")   # test.url is no example
        self.assertEqual((r["rule"], r["example_path"]), ("a", None))
        self.assertEqual(self.d(F([P("target", True)]), "/go_x?target=a.com")["rule"], "a")  # carried, odd prefix or not

    def test_b_when_nothing_is_required_an_example_that_sends_no_declared_param_gives_way_to_the_test_url(self):
        # "one of url or origin" is declared with both optional; the category default ?target= made the real service answer 400.
        f = F([P("url"), P("origin")], test_url="/?url=https://web.dev&api_key=default_token")
        r = self.d(f, "/?target=example.com")
        self.assertEqual((r["rule"], r["example_path"]), ("b", "/?url=https://web.dev"))
        # names are compared without case, dashes or underscores
        r = self.d(F([P("form_factor")], test_url="/?formFactor=PHONE"), "/?target=a.com")
        self.assertEqual((r["rule"], r["example_path"]), ("b", "/?formFactor=PHONE"))
        # a test.url that sends none of the declared params does not replace anything
        self.assertEqual(self.d(F([P("url")], test_url="/?zzz=1"), "/?target=a.com")["example_path"], None)
        # a bare example (no query) is left to the hub's own rule
        self.assertEqual(self.d(F([P("url")], test_url="/?url=https://web.dev"), "/")["rule"], "a")

    def test_b_the_sanitised_test_url_when_it_carries_every_required_param(self):
        f = F([P("target", True)], test_url="/t/default_token/go_x?target=https://example.com&api_key=default_token")
        r = self.d(f, "/?url=https://example.com")
        self.assertEqual((r["rule"], r["example_path"], r["source"]), ("b", "/?target=https://example.com", None))

    def test_b_is_refused_for_a_key_a_placeholder_a_secret_or_a_missing_param(self):
        cur = "/?url=https://example.com"
        r = self.d(F([P("target", True)], test_url="/?api_key={token}&target=a.com"), cur)
        self.assertEqual(r["rule"], "b")                                    # the key is stripped, then it is clean
        self.assertNotIn("api_key", r["example_path"])
        r = self.d(F([P("target", True)], test_url="/?target={target}"), cur)
        self.assertEqual((r["rule"], r["example_path"]), ("c", "/?target=example.com"))
        r = self.d(F([P("target", True)], test_url="/?other=a.com"), cur)
        self.assertEqual((r["rule"], r["example_path"]), ("c", "/?target=example.com"))
        r = self.d(F([P("token", True)], test_url="/?token=sk_live_FAKEFAKEFAKE"), cur)
        self.assertEqual((r["rule"], r["example_path"]), ("d", None))

    def test_d_a_health_path_is_not_an_example(self):
        for t in ("/health", "/selftest?api_key=default_token", "/_gw_health", "/health/"):
            r = self.d(F([P("domain", True)], test_url=t), "/")
            self.assertEqual((r["rule"], r["example_path"], r["source"]), ("c", "/?domain=example.com", "derived-from-params"), t)
        r = self.d(F([P("program", True)], test_url="/health"), "/")
        self.assertEqual((r["rule"], r["example_path"]), ("d", None))
        self.assertIn("health path", r["reason"])

    def test_c_derived_from_params_is_marked_and_uses_the_endpoint_path(self):
        f = F([P("domain", True), P("sources", False)], endpoint="/enum?api_key={token}")
        r = self.d(f, "/sources")
        self.assertEqual((r["rule"], r["example_path"], r["source"]), ("c", "/enum?domain=example.com", "derived-from-params"))
        f = F([P("host", True), P("port", True, example=22)], endpoint=None)
        self.assertEqual(self.d(f, "/")["example_path"], "/?host=example.com&port=22")
        f = F([P("q", True)])
        self.assertEqual(self.d(f, "/")["example_path"], "/?q=anthropic%20claude")
        f = F([P("url", True, default="https://a.org/x?y=1")])
        self.assertEqual(self.d(f, "/")["example_path"], "/?url=https://a.org/x%3Fy%3D1")

    def test_d_a_required_param_without_a_safe_value_is_reported_not_invented(self):
        r = self.d(F([P("program", True), P("domain", True)]), "/")
        self.assertEqual((r["rule"], r["example_path"]), ("d", None))
        self.assertIn("'program'", r["reason"])

    def test_post_only_services_are_never_given_a_derived_link(self):
        f = F([P("domain", True)], method="POST", endpoint="/scan")
        r = self.d(f, "/stats")
        self.assertEqual((r["rule"], r["example_path"]), ("d", None))
        self.assertIn(ssp.BODY_REASON, r["reason"])
        # but its own tested GET request is still a good example when it carries the param
        r = self.d(F([P("to", True)], method="POST", test_url="/?text=a&to=yaml"), "/")
        self.assertEqual((r["rule"], r["example_path"]), ("b", "/?text=a&to=yaml"))
        # GET and POST both: a link can carry it
        self.assertEqual(self.d(F([P("domain", True)], method="GET, POST"), "/")["rule"], "c")
        self.assertEqual(self.d(F([P("domain", True)], method=None), "/")["rule"], "c")

    def test_a_path_placeholder_is_not_a_query_param_and_is_filled_only_from_a_value(self):
        f = F([P("name", True)], endpoint="/secrets/{name}")
        self.assertEqual(ssp.required_query(f), [])
        self.assertEqual(self.d(f, "/")["rule"], "a")
        f = F([P("name", True), P("domain", True)], endpoint="/x/{name}")
        r = self.d(f, "/")
        self.assertEqual(r["rule"], "d")                                    # name has no safe value, so the template stays unfilled
        f = F([P("name", True, example="abc"), P("domain", True)], endpoint="/x/{name}")
        self.assertEqual(self.d(f, "/")["example_path"], "/x/abc?domain=example.com")
        self.assertEqual(ssp.path_params(F([P("token", True)], endpoint="/?api_key={token}")), [])   # the key placeholder is no param
        self.assertEqual(ssp.path_params(F([P("token", True)], endpoint="/t/{token}/")), [])


class TestRegistryParams(unittest.TestCase):
    def test_shape_truncation_and_examples(self):
        long = "word " * 40
        f = F([P("domain", True, description="Domain to scan"), P("note", description=long.strip()),
               P("url", example="https://given.example"), P("token", example="sk_live_FAKEFAKEFAKE"),
               P("odd", type=None)])
        out = ssp.registry_params(f, "/?domain=example.com&token=sk_live_FAKEFAKEFAKE")
        self.assertEqual(out[0], {"name": "domain", "required": True, "type": "string", "description": "Domain to scan",
                                  "example": "example.com"})
        self.assertLessEqual(len(out[1]["description"]), ssp.DESCRIPTION_MAX)
        self.assertTrue(out[1]["description"].endswith("..."))
        self.assertNotIn("example", out[1])
        self.assertEqual(out[2]["example"], "https://given.example")
        self.assertNotIn("example", out[3])                                 # secret-shaped: never stored
        self.assertEqual(out[4], {"name": "odd", "required": False})
        self.assertEqual(ssp.short_description("short"), "short")
        self.assertIsNone(ssp.short_description(None))

    def test_required_is_copied_never_invented(self):
        f = F([P("a", False), P("b", True)])
        self.assertEqual([p["required"] for p in ssp.registry_params(f, "/")], [False, True])


OVERRIDES = """{
  "$rules": [],
  "a": {
    "example_path": "/?url=https://example.com",
    "trl": 4
  },
  "b": {
    "trl": 2
  },
  "inline": {
    "version": "0.1.0",
    "spiffe_workload": true  },
  "tail": {
    "trl": 1,
    "example_path": "/",
    "example_path_source": "derived-from-params"
  },
  "c": {"version": "3.0.0"}
}
"""
PARAMS = [{"name": "target", "required": True, "type": "string", "example": "example.com"}]


class TestSetFields(unittest.TestCase):
    def test_replace_insert_in_order_and_inline_close(self):
        out = ssp.set_fields(OVERRIDES, {
            "a": {"example_path": "/?target=example.com", "example_path_source": "derived-from-params", "params": PARAMS},
            "b": {"example_path": "/x", "params": PARAMS},
            "inline": {"params": PARAMS},
        })
        doc = json.loads(out)
        self.assertEqual(doc["a"], {"example_path": "/?target=example.com", "example_path_source": "derived-from-params",
                                    "params": PARAMS, "trl": 4})
        self.assertEqual(list(doc["b"]), ["example_path", "params", "trl"])
        self.assertEqual(doc["c"], {"version": "3.0.0"})
        self.assertIn('"spiffe_workload": true  },', out)                    # the inline close is kept byte for byte
        lines = out.splitlines()
        i = lines.index('    "example_path": "/?target=example.com",')     # rewritten in place, the marker right after it
        self.assertEqual(lines[i + 1], '    "example_path_source": "derived-from-params",')
        self.assertEqual(lines[3], '    "params": [')                        # a new key goes first in the block
        # outside the touched blocks every byte is the original's
        self.assertTrue(out.startswith('{\n  "$rules": [],\n'))
        self.assertTrue(out.endswith('  "c": {"version": "3.0.0"}\n}\n'))

    def test_a_removed_key_leaves_valid_layout_either_way(self):
        out = ssp.set_fields(OVERRIDES, {"tail": {"example_path_source": None, "example_path": "/y"}})
        self.assertEqual(json.loads(out)["tail"], {"trl": 1, "example_path": "/y"})
        self.assertIn('    "example_path": "/y"\n  },', out)
        out = ssp.set_fields(OVERRIDES, {"tail": {"example_path": None}})
        self.assertEqual(json.loads(out)["tail"], {"trl": 1, "example_path_source": "derived-from-params"})

    def test_is_idempotent_and_refuses_what_it_cannot_place(self):
        changes = {"a": {"params": PARAMS}, "b": {"params": PARAMS}}
        once = ssp.set_fields(OVERRIDES, changes)
        self.assertEqual(ssp.set_fields(once, changes), once)
        with self.assertRaisesRegex(ValueError, "expected one top-level block"):
            ssp.set_fields(OVERRIDES, {"missing": {"params": PARAMS}})
        with self.assertRaisesRegex(ValueError, "layout"):
            ssp.set_fields(OVERRIDES, {"c": {"params": PARAMS}})


class TestPlan(unittest.TestCase):
    SERVICES = [
        {"id": "dom", "example_path": "/?url=https://example.com"},
        {"id": "kept", "example_path": "/?domain=example.com"},
        {"id": "child", "example_path": "/"},
        {"id": "default", "example_path": "/"},
    ]
    BLOCKS = {"dom": {"example_path": "/?url=https://example.com"}, "kept": {"example_path": "/?domain=example.com"},
              "default": {}}

    def rows(self, extra=(), blocks=None):
        facts = [F([P("domain", True, description="d")], sid="dom"), F([P("domain", True)], sid="kept"),
                 F([P("domain", True)], sid="child"), F([P("domain", True)], sid="default"), F([P("domain", True)], sid="ghost"),
                 *extra]
        return {r["id"]: r for r in ssp.plan(facts, self.SERVICES, blocks or self.BLOCKS)}

    def test_services_without_a_block_or_registry_row_are_never_touched(self):
        rows = self.rows()
        self.assertEqual(rows["child"]["rule"], "skip")
        self.assertEqual(rows["child"]["changes"], {})
        self.assertEqual(rows["ghost"]["rule"], "skip")
        self.assertIn("not in the registry", rows["ghost"]["reason"])

    def test_changes_hold_only_what_differs(self):
        rows = self.rows()
        self.assertEqual(rows["dom"]["changes"]["example_path"], "/?domain=example.com")
        self.assertEqual(rows["dom"]["changes"]["example_path_source"], "derived-from-params")
        self.assertEqual(set(rows["kept"]["changes"]), {"params"})           # rule (a): params only
        self.assertEqual(rows["default"]["changes"]["example_path"], "/?domain=example.com")

    def test_running_the_result_again_changes_nothing(self):
        rows = self.rows()
        services = [dict(s) for s in self.SERVICES]
        blocks = {k: dict(v) for k, v in self.BLOCKS.items()}
        for sid, r in rows.items():
            if r["changes"]:
                blocks[sid].update({k: v for k, v in r["changes"].items() if v is not None})
                next(s for s in services if s["id"] == sid)["example_path"] = blocks[sid].get("example_path")
        again = {r["id"]: r for r in ssp.plan(
            [F([P("domain", True, description="d")], sid="dom"), F([P("domain", True)], sid="kept"),
             F([P("domain", True)], sid="default")], services, blocks)}
        self.assertEqual({sid: r["changes"] for sid, r in again.items()}, {"dom": {}, "kept": {}, "default": {}})

    def test_summary_counts_and_lists_what_was_left(self):
        rows = list(self.rows(extra=[F([P("program", True)], sid="kept")]).values())
        s = ssp.summarise(rows)
        self.assertEqual(s["rules"]["skip"], 2)
        self.assertIn("child", [sid for sid, _ in s["skipped"]])


class TestNoKeyIsEverStored(unittest.TestCase):
    def test_derived_paths_carry_no_key_whatever_the_test_url_says(self):
        for url in ("/?api_key=abc&target=a.com", "/t/tok/go_x?target=a.com&apikey=abc", "/go_x?x-api-key=k&target=a.com&API_KEY=z"):
            r = ssp.decide(F([P("target", True)], test_url=url), "/")
            self.assertEqual(r["rule"], "b", url)
            self.assertNotRegex(r["example_path"], r"(?i)api_?key|x-api-key|abc|/t/", url)


if __name__ == "__main__":
    unittest.main()
