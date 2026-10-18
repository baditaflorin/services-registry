import importlib.util
import json
import os
import shutil
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


# --- multi-place edges: layered accepted IPs, runtime list, nothing-eligible ---------------
# Hermetic: DNS, TLS inspection, curl and the ntfy POST are all fakes; nothing opens a socket.
GW = "176.9.123.221"            # built-in compose edge
OTHER_EDGE = "203.0.113.7"      # a second public edge (documentation range)
INTERNAL = "192.0.2.10"         # the split-horizon address an internal vantage resolves to
STRAY = "198.51.100.9"          # an address nobody accepts


def probe_dns(service, ips, table=None):
    """probe() with a fake resolver answering `ips`, a valid cert and a healthy /health."""
    args = types.SimpleNamespace(timeout=5.0, expiry_days=14)
    if table is not None:
        args.edge_table = table
    health_url = service["url"].rstrip("/") + "/health"
    with mock.patch.object(fep, "resolve_a", mock.Mock(return_value=set(ips))), \
         mock.patch.object(fep, "inspect_cert",
                           return_value=(True, ["demo.example.test"], FAR_FUTURE, "")), \
         mock.patch.object(fep, "curl", return_value=(200, health_url, '{"status":"ok"}')):
        return fep.probe(service, args)


def write_json(tc, name, doc):
    d = tempfile.mkdtemp()
    tc.addCleanup(shutil.rmtree, d, True)
    path = os.path.join(d, name)
    Path(path).write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return path


def edges_doc(**kw):
    doc = {"schema": 1, "runtime": {}, "mesh": {}, "any": []}
    doc.update(kw)
    return doc


class GatewayLayerTests(unittest.TestCase):
    def table(self, extra="", **edges):
        path = write_json(self, "edges.json", edges_doc(**edges))
        return fep.build_edge_table(path, extra)

    def test_union_of_accepted_ips_passes(self):
        t = self.table(INTERNAL, runtime={"compose": [OTHER_EDGE]})
        for ip in (GW, OTHER_EDGE, INTERNAL):  # built-in, data file, per-vantage extra
            r = probe_dns(svc(), {ip}, t)
            self.assertNotIn("gateway_drift", r["fails"], ip)
            self.assertTrue(r["ok"], r)
        # a multi-record answer passes when ANY record is accepted
        self.assertNotIn("gateway_drift", probe_dns(svc(), {STRAY, OTHER_EDGE}, t)["fails"])

    def test_unexpected_ip_is_still_gateway_drift_with_warn_severity(self):
        t = self.table(INTERNAL, runtime={"compose": [OTHER_EDGE]})
        r = probe_dns(svc(), {STRAY}, t)
        self.assertEqual(r["fails"], ["gateway_drift"])
        self.assertFalse(r["hard_fail"])            # severity unchanged: warn, never a page
        self.assertFalse(r["ok"])
        self.assertEqual(fep.SEVERITY["gateway_drift"], "warn")
        self.assertEqual(r["meta"]["want"], sorted({GW, OTHER_EDGE, INTERNAL}))

    def test_extras_only_apply_when_set(self):
        r = probe_dns(svc(), {INTERNAL}, fep.build_edge_table("", ""))
        self.assertEqual(r["fails"], ["gateway_drift"], "an internal address is drift without the extra")
        r = probe_dns(svc(), {INTERNAL}, fep.build_edge_table("", INTERNAL))
        self.assertEqual(r["fails"], [])
        # blank / whitespace / trailing commas mean "not set"
        for spec in ("", " ", ",", " , "):
            self.assertEqual(fep.parse_gateway_extra(spec), frozenset(), repr(spec))
        self.assertEqual(fep.parse_gateway_extra(" 192.0.2.10 , 192.0.2.11,"),
                         frozenset({"192.0.2.10", "192.0.2.11"}))

    def test_layers_only_add_never_subtract(self):
        t = self.table(INTERNAL, runtime={"compose": [OTHER_EDGE]}, any=[STRAY])
        self.assertTrue({GW, OTHER_EDGE, INTERNAL, STRAY} <= t.accepted(svc()))
        # the coolify default survives a file that only mentions compose
        self.assertIn("65.108.75.123", t.accepted(svc(runtime="coolify")))

    def test_extras_and_any_do_not_enable_the_check_for_an_unchecked_runtime(self):
        t = self.table(INTERNAL, any=[OTHER_EDGE])
        for runtime in ("external", "weird", ""):
            r = probe_dns(svc(runtime=runtime), {STRAY}, t)
            self.assertNotIn("gateway_drift", r["fails"], runtime)
            self.assertEqual(t.accepted(svc(runtime=runtime)), frozenset())

    def test_mesh_entry_enables_and_widens(self):
        t = self.table("", mesh={"0crawl": [OTHER_EDGE]})
        # a runtime with no base set is checked once its mesh has one
        self.assertEqual(probe_dns(svc(runtime="weird", mesh="0crawl"), {STRAY}, t)["fails"],
                         ["gateway_drift"])
        self.assertEqual(probe_dns(svc(runtime="weird", mesh="0crawl"), {OTHER_EDGE}, t)["fails"], [])
        # and it widens (does not replace) the runtime set for that mesh
        self.assertEqual(probe_dns(svc(mesh="0crawl"), {GW}, t)["fails"], [])
        self.assertEqual(probe_dns(svc(mesh="0crawl"), {OTHER_EDGE}, t)["fails"], [])
        self.assertEqual(probe_dns(svc(mesh="0exec"), {OTHER_EDGE}, t)["fails"], ["gateway_drift"])

    def test_old_style_args_without_a_table_use_the_builtin_default(self):
        # ARGS has no edge_table attribute, as every pre-existing caller
        self.assertEqual(probe_dns(svc(), {GW})["fails"], [])
        self.assertEqual(probe_dns(svc(), {INTERNAL})["fails"], ["gateway_drift"])
        self.assertEqual(probe_dns(svc(runtime="coolify"), {"65.108.75.123"})["fails"], [])
        self.assertEqual(probe_dns(svc(runtime="coolify"), {GW})["fails"], ["gateway_drift"])

    def test_malformed_configuration_is_a_loud_error_not_a_silent_fallback(self):
        bad_ips = ("not-an-ip", "2001:db8::1", "10.0.0.1/8", "256.1.1.1")
        for bad in bad_ips:
            with self.assertRaises(fep.EdgeConfigError, msg=bad):
                fep.parse_gateway_extra(f"{INTERNAL},{bad}")
            with self.assertRaises(fep.EdgeConfigError, msg=bad):
                fep.build_edge_table(write_json(self, "e.json", edges_doc(any=[bad])), "")
        for doc in (
            edges_doc(runtimes={"compose": [OTHER_EDGE]}),   # typo'd key must not silently do nothing
            {"runtime": {}},                                  # missing schema
            edges_doc(schema=2),
            edges_doc(runtime={"compose": OTHER_EDGE}),       # not a list
            edges_doc(mesh=["0exec"]),
            edges_doc(any=OTHER_EDGE),
            [],
            "{not json",
        ):
            with self.assertRaises(fep.EdgeConfigError, msg=repr(doc)):
                fep.build_edge_table(write_json(self, "e.json", doc), "")
        with self.assertRaises(fep.EdgeConfigError):
            fep.build_edge_table(os.path.join(tempfile.gettempdir(), "no-such-edges.json"), "")

    def test_default_file_is_used_only_when_present(self):
        with mock.patch.object(fep, "DEFAULT_EDGES_FILE", os.path.join(tempfile.gettempdir(), "absent.json")):
            self.assertEqual(fep.build_edge_table("", "").accepted(svc()), frozenset({GW}))
        path = write_json(self, "d.json", edges_doc(runtime={"compose": [OTHER_EDGE]}))
        with mock.patch.object(fep, "DEFAULT_EDGES_FILE", path):
            self.assertEqual(fep.build_edge_table("", "").accepted(svc()), frozenset({GW, OTHER_EDGE}))
        bad = write_json(self, "d.json", "{broken")
        with mock.patch.object(fep, "DEFAULT_EDGES_FILE", bad):
            with self.assertRaises(fep.EdgeConfigError):  # a present-but-invalid default is never ignored
                fep.build_edge_table("", "")

    def test_the_shipped_edges_file_is_valid_and_keeps_the_builtin_behaviour(self):
        shipped = Path(SCRIPT).with_name("fleet-edge-probe.edges.json")
        self.assertTrue(shipped.exists())
        fep.load_edges_file(str(shipped))  # raises on any schema/IP error: a bad merge fails CI here
        t = fep.build_edge_table(str(shipped), "")
        for runtime, ips in fep.GATEWAY_IPS.items():
            self.assertTrue(set(ips) <= t.accepted(svc(runtime=runtime)), runtime)


def reg_entry(i, **kw):
    return svc(id=f"c{i}", url=f"https://c{i}.example.test", **kw)


MIXED_REGISTRY = (
    [reg_entry(i) for i in range(3)]                                  # compose
    + [reg_entry(10, runtime="coolify"), reg_entry(11, runtime="coolify")]
    + [{"id": "site", "kind": "static", "runtime": "github-pages", "url": "https://x.github.io/"}]
    + [{"id": "plausible", "kind": "container", "runtime": "external",
        "url": "http://dockerhost:18204", "network_exposure": "lan-internal"}]
)


def ids(selection):
    return sorted(e["id"] for e in selection)


class RuntimeFilterTests(unittest.TestCase):
    def test_parse(self):
        p = fep.parse_runtime_filter
        self.assertIsNone(p(""))
        self.assertIsNone(p("all"))
        self.assertIsNone(p("ALL"))
        self.assertIsNone(p("compose,all"))
        self.assertIsNone(p(" , "))
        self.assertEqual(p("compose"), frozenset({"compose"}))                 # the old single value
        self.assertEqual(p("compose, coolify ,external"), frozenset({"compose", "coolify", "external"}))

    def test_single_runtime_is_backward_compatible(self):
        elig, _ = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter("compose"), set())
        self.assertEqual(ids(elig), ["c0", "c1", "c2"])
        elig, _ = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter("coolify"), set())
        self.assertEqual(ids(elig), ["c10", "c11"])

    def test_comma_list_and_all(self):
        both = ["c0", "c1", "c10", "c11", "c2"]
        elig, _ = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter("compose,coolify"), set())
        self.assertEqual(ids(elig), both)
        # `external` in the list is allowed and adds nothing: those entries are never probed
        elig, _ = fep.select_services(MIXED_REGISTRY,
                                      fep.parse_runtime_filter("compose,coolify,external"), set())
        self.assertEqual(ids(elig), both)
        for spec in ("all", ""):
            elig, _ = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter(spec), set())
            self.assertEqual(ids(elig), both, spec)
        elig, _ = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter("external"), set())
        self.assertEqual(elig, [])

    def test_only_and_sample_still_apply(self):
        elig, _ = fep.select_services(MIXED_REGISTRY, None, {"c1", "c10"})
        self.assertEqual(ids(elig), ["c1", "c10"])
        elig, _ = fep.select_services(MIXED_REGISTRY, None, set(), sample=2)
        self.assertEqual(len(elig), 2)

    def test_every_entry_is_counted_once_under_the_first_reason(self):
        for spec in ("", "coolify", "compose,coolify", "external", "nope"):
            elig, d = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter(spec), set())
            self.assertEqual(d["registry_entries"], len(MIXED_REGISTRY))
            self.assertEqual(sum(d["excluded"].values()) + d["matched"], len(MIXED_REGISTRY), spec)
        _, d = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter("coolify"), set())
        self.assertEqual(d["excluded"], {"kind": 1, "external": 1, "url": 0,
                                         "network_exposure": 0, "runtime": 3, "only": 0})
        self.assertEqual(d["requested_runtime_counts"], {"coolify": 2})
        self.assertEqual(d["registry_runtimes"], {"compose": 3, "coolify": 2, "external": 1, "github-pages": 1})

    def test_not_public_exposures_are_skipped_with_the_schema_spelling(self):
        reg = [reg_entry(1, network_exposure="lan-internal"), reg_entry(2, network_exposure="loopback"),
               reg_entry(3, network_exposure="internal"), reg_entry(4, network_exposure="gateway-public"),
               reg_entry(5, network_exposure="gateway-authenticated"), reg_entry(6)]
        elig, d = fep.select_services(reg, None, set())
        self.assertEqual(ids(elig), ["c4", "c5", "c6"])
        self.assertEqual(d["excluded"]["network_exposure"], 3)

    def test_describe_names_filters_and_per_filter_counts(self):
        _, d = fep.select_services(MIXED_REGISTRY, fep.parse_runtime_filter("coolify"), {"zzz"})
        text = fep.describe_selection(d)
        for needle in ("of 7 registry entries", "runtime=coolify", "only=zzz",
                       "kind != container: 1", "runtime external (never probed): 1",
                       "runtime not in filter: 3", "--only: 2",
                       "coolify=2", "compose=3"):
            self.assertIn(needle, text)


REAL = object()


def run_main(tc, argv, registry, workdir, probe_fn=None, env=None, ntfy_fails=False, ntfy_error=None,
             on_post=None):
    """Run main() against a registry file with every side effect faked.

    Returns (rc, logs, stdout, ntfy_posts). `probe_fn=REAL` leaves probe() un-mocked (the
    caller then fakes DNS/TLS/curl itself). The ntfy POST is collected, never sent.
    """
    services = os.path.join(workdir, "services.json")
    Path(services).write_text(registry if isinstance(registry, str) else json.dumps(registry))
    clean = {k: v for k, v in os.environ.items() if not k.startswith("EDGE_PROBE_")}
    clean.update(env or {})
    logs, stdout, posts = [], [], []

    def fake_urlopen(req, timeout=None):
        if on_post:
            on_post()
        if ntfy_error:
            raise ntfy_error
        if ntfy_fails:
            raise OSError("ntfy down")
        posts.append({"url": req.full_url, "body": req.data.decode(),
                      "auth": req.get_header("Authorization")})
        return mock.MagicMock()

    def default_probe(s, args):
        return fep._result(s["id"], urlsplit_host(s), s.get("runtime", ""), [], {})

    patches = [
        mock.patch.dict(os.environ, clean, clear=True),
        mock.patch.object(fep.sys, "argv", ["fleet-edge-probe", "--services", services] + argv),
        mock.patch.object(fep, "log", side_effect=lambda *a: logs.append(" ".join(map(str, a)))),
        mock.patch.object(fep.urllib.request, "urlopen", fake_urlopen),
        mock.patch("builtins.print", side_effect=lambda *a, **k: stdout.append(" ".join(map(str, a)))),
    ]
    if probe_fn is not REAL:
        patches.append(mock.patch.object(fep, "probe", side_effect=probe_fn or default_probe))
    for p in patches:
        p.start()
    try:
        rc = fep.main()
    finally:
        for p in reversed(patches):
            p.stop()
    return rc, logs, stdout, posts


def urlsplit_host(s):
    return s["url"].split("//", 1)[1].split("/", 1)[0].split(":")[0]


class MainRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.probed = []

        def probe_fn(s, args):
            self.probed.append(s["id"])
            return fep._result(s["id"], urlsplit_host(s), s["runtime"], [], {})
        self.probe_fn = probe_fn

    def run_it(self, argv, registry=MIXED_REGISTRY, **kw):
        self.probed.clear()
        return run_main(self, argv, registry, self.d, probe_fn=self.probe_fn, **kw)

    def test_runtime_values_through_main(self):
        for argv, want in (
            (["--runtime", "compose"], ["c0", "c1", "c2"]),
            (["--runtime=coolify"], ["c10", "c11"]),
            (["--runtime", "compose,coolify"], ["c0", "c1", "c10", "c11", "c2"]),
            (["--runtime", "all"], ["c0", "c1", "c10", "c11", "c2"]),
            ([], ["c0", "c1", "c10", "c11", "c2"]),
        ):
            rc, logs, _, _ = self.run_it(argv)
            self.assertEqual(rc, 0, argv)
            self.assertEqual(sorted(self.probed), want, argv)

    def test_a_listed_runtime_with_no_services_is_warned_about_not_fatal(self):
        reg = [reg_entry(i) for i in range(2)]
        rc, logs, _, _ = self.run_it(["--runtime", "compose,coolify"], registry=reg)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(self.probed), ["c0", "c1"])
        self.assertTrue(any("--runtime coolify matches 0 services" in l for l in logs), logs)

    def test_gateway_extra_env_reaches_the_real_probe(self):
        reg = [reg_entry(1)]

        def verdict(env):
            with mock.patch.object(fep, "resolve_a", return_value={INTERNAL}), \
                 mock.patch.object(fep, "inspect_cert",
                                   return_value=(True, ["c1.example.test"], FAR_FUTURE, "")), \
                 mock.patch.object(fep, "curl", return_value=(200, "https://c1.example.test/health",
                                                             '{"status":"ok"}')):
                rc, _, out, _ = run_main(self, ["--json"], reg, self.d, probe_fn=REAL, env=env)
            return rc, json.loads(out[-1])["results"][0]["fails"]

        self.assertEqual(verdict({}), (0, ["gateway_drift"]))  # warn only: exit stays 0
        self.assertEqual(verdict({"EDGE_PROBE_GATEWAY_EXTRA": INTERNAL}), (0, []))
        self.assertEqual(verdict({"EDGE_PROBE_GATEWAY_EXTRA": "198.51.100.1"}), (0, ["gateway_drift"]))

    def test_gateway_extra_flag_equals_env(self):
        reg = [reg_entry(1)]
        with mock.patch.object(fep, "resolve_a", return_value={INTERNAL}), \
             mock.patch.object(fep, "inspect_cert", return_value=(True, ["c1.example.test"], FAR_FUTURE, "")), \
             mock.patch.object(fep, "curl", return_value=(200, "https://c1.example.test/health", "ok")):
            rc, _, out, _ = run_main(self, ["--json", "--gateway-extra", INTERNAL], reg, self.d,
                                     probe_fn=REAL)
        self.assertEqual(json.loads(out[-1])["results"][0]["fails"], [])

    def test_bad_edge_configuration_exits_2_before_probing(self):
        for argv, env in (
            ([], {"EDGE_PROBE_GATEWAY_EXTRA": "nope"}),
            (["--gateway-extra", "2001:db8::1"], {}),
            (["--edges", os.path.join(self.d, "missing.json")], {}),
            ([], {"EDGE_PROBE_EDGES": write_json(self, "e.json", "{broken")}),
        ):
            rc, logs, _, _ = self.run_it(argv, env=env)
            self.assertEqual(rc, 2, (argv, env))
            self.assertEqual(self.probed, [])
            self.assertTrue(any("edge configuration error" in l for l in logs), logs)

    def test_unusable_registry_is_still_exit_2(self):
        self.assertEqual(self.run_it([], registry='{"not": "a list"}')[0], 2)
        self.assertEqual(self.run_it([], registry="{broken")[0], 2)


class NothingEligibleTests(unittest.TestCase):
    """The 2026-10-17 incident: EDGE_PROBE_RUNTIME=coolify while the registry has no coolify service."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, "state.json")
        self.prom = os.path.join(self.d, "edge.prom")
        self.reg = [reg_entry(i) for i in range(3)] + [MIXED_REGISTRY[-2], MIXED_REGISTRY[-1]]
        self.argv = ["--runtime", "coolify", "--state", self.state, "--prom", self.prom,
                     "--vantage", "vt", "--ntfy", "https://ops:hunter2@ntfy.example.test/edge"]

    def run_it(self, argv=None, registry=None, **kw):
        return run_main(self, argv if argv is not None else self.argv,
                        self.reg if registry is None else registry, self.d, **kw)

    def test_distinct_exit_code_and_a_message_naming_filters_and_counts(self):
        rc, logs, _, _ = self.run_it()
        self.assertEqual(rc, fep.EXIT_NOTHING_ELIGIBLE)
        self.assertEqual(fep.EXIT_NOTHING_ELIGIBLE, 3)
        self.assertNotIn(fep.EXIT_NOTHING_ELIGIBLE, (fep.EXIT_OK, fep.EXIT_FAIL, fep.EXIT_USAGE))
        text = "\n".join(logs)
        for needle in ("NOTHING ELIGIBLE", "runtime=coolify", "runtime not in filter: 3",
                       "kind != container: 1", "runtime external (never probed): 1",
                       "coolify=0", "compose=3"):
            self.assertIn(needle, text)

    def test_prom_gets_eligible_zero_and_up_zero_before_exit(self):
        rc, *_ = self.run_it()
        text = Path(self.prom).read_text()
        self.assertIn('fleet_edge_probe_eligible_services{vantage="vt"} 0', text)
        self.assertIn('fleet_edge_probe_up{vantage="vt"} 0', text)
        self.assertIn('fleet_edge_probe_hard_fail_total{vantage="vt"} 0', text)
        self.assertRegex(text, r'fleet_edge_probe_last_run\{vantage="vt"\} \d+')

    def test_prom_is_written_before_the_network_post(self):
        seen = []
        self.run_it(on_post=lambda: seen.append(os.path.exists(self.prom)))
        self.assertEqual(seen, [True])

    def test_a_failing_or_malformed_ntfy_never_costs_the_metric_or_leaks_credentials(self):
        rc, logs, _, _ = self.run_it(ntfy_fails=True)
        self.assertEqual(rc, 3)
        self.assertIn('eligible_services{vantage="vt"} 0', Path(self.prom).read_text())
        os.unlink(self.prom)
        os.unlink(self.state)
        bad = ["--runtime", "coolify", "--state", self.state, "--prom", self.prom, "--vantage", "vt",
               "--ntfy", "https://ops:hunter2@host:notaport/edge"]  # urllib raises ValueError
        rc, logs, _, posts = self.run_it(bad)
        self.assertEqual(rc, 3)
        self.assertIn('eligible_services{vantage="vt"} 0', Path(self.prom).read_text())
        self.assertEqual(posts, [])
        self.assertFalse(any("hunter2" in l for l in logs), logs)
        # an exception whose text echoes the URL (and its user:pass) is logged by class only
        os.unlink(self.state)
        rc, logs, _, _ = self.run_it(ntfy_error=ValueError("unknown url type: 'ops:hunter2@nohost'"))
        self.assertEqual(rc, 3)
        self.assertTrue(any("ntfy post failed: ValueError" in l for l in logs), logs)
        self.assertFalse(any("hunter2" in l for l in logs), logs)

    def test_one_alert_per_transition_not_per_run(self):
        # run 1: enters "nothing eligible" -> exactly one POST, carrying the filter and counts
        rc, _, _, posts = self.run_it()
        self.assertEqual((rc, len(posts)), (3, 1))
        self.assertIn("NOTHING to probe", posts[0]["body"])
        self.assertIn("runtime=coolify", posts[0]["body"])
        self.assertIn("runtime not in filter: 3", posts[0]["body"])
        self.assertEqual(posts[0]["url"], "https://ntfy.example.test/edge")   # creds moved to a header
        self.assertTrue(posts[0]["auth"].startswith("Basic "))
        # runs 2-4: still nothing eligible -> no further POST
        for _ in range(3):
            rc, _, _, posts = self.run_it()
            self.assertEqual((rc, posts), (3, []))
        self.assertTrue(json.loads(Path(self.state).read_text())["nothing_eligible"])
        # fixed (filter now matches): one "probes N again" notice, exit 0
        rc, _, _, posts = self.run_it(["--runtime", "compose"] + self.argv[2:])
        self.assertEqual(rc, 0)
        self.assertEqual(len(posts), 1)
        self.assertIn("probes 3 services again", posts[0]["body"])
        self.assertFalse(json.loads(Path(self.state).read_text())["nothing_eligible"])
        # a steady healthy run is silent
        rc, _, _, posts = self.run_it(["--runtime", "compose"] + self.argv[2:])
        self.assertEqual((rc, posts), (0, []))
        # breaking it again is a NEW transition: one more alert
        rc, _, _, posts = self.run_it()
        self.assertEqual((rc, len(posts)), (3, 1))

    def test_first_run_with_no_state_and_an_old_format_state_both_alert_once(self):
        rc, _, _, posts = self.run_it()                      # no state file at all
        self.assertEqual((rc, len(posts)), (3, 1))
        Path(self.state).write_text(json.dumps({"ts": "x", "results": [
            {"slug": "c0", "ok": True, "hard_fail": False, "fails": []}]}))  # state of the old version
        rc, _, _, posts = self.run_it()                      # not yet known to be blind: alert
        self.assertEqual((rc, len(posts)), (3, 1))
        rc, _, _, posts = self.run_it()                      # and now it is known: silent
        self.assertEqual((rc, len(posts)), (3, 0))
        Path(self.state).write_text("{corrupt")              # unreadable state counts as "not blind"
        rc, _, _, posts = self.run_it()
        self.assertEqual((rc, len(posts)), (3, 1))

    def test_no_state_means_no_ntfy_but_the_metric_and_exit_code_stand(self):
        argv = [a for a in self.argv if a != self.state and a != "--state"]
        rc, _, _, posts = self.run_it(argv)
        self.assertEqual((rc, posts), (3, []))
        self.assertIn('eligible_services{vantage="vt"} 0', Path(self.prom).read_text())

    def test_zero_services_and_all_sunset_are_also_nothing_eligible(self):
        rc, logs, _, _ = self.run_it(registry=[])
        self.assertEqual(rc, 3)
        self.assertIn("of 0 registry entries", "\n".join(logs))
        sunset = [reg_entry(1, lifecycle={"status": "sunset"}), reg_entry(2, lifecycle={"status": "sunset"})]
        rc, logs, out, _ = self.run_it(["--runtime", "all", "--prom", self.prom, "--vantage", "vt"],
                                       registry=sunset)
        self.assertEqual(rc, 3)
        self.assertIn("2 matched (2 of them sunset", "\n".join(logs))
        self.assertTrue(any(line.startswith("SKIP") for line in out))
        self.assertIn('fleet_edge_probe_skipped_total{vantage="vt"} 2', Path(self.prom).read_text())
        self.assertIn('eligible_services{vantage="vt"} 0', Path(self.prom).read_text())

    def test_json_report_carries_the_reason(self):
        rc, _, out, _ = self.run_it(["--runtime", "coolify", "--json"])
        report = json.loads(out[-1])
        self.assertEqual((rc, report["eligible"], report["probed"]), (3, 0, 0))
        self.assertTrue(report["nothing_eligible"])
        self.assertEqual(report["detail"]["requested_runtime_counts"], {"coolify": 0})

    def test_a_normal_run_exports_the_eligible_count_and_exit_codes_are_unchanged(self):
        reg = [reg_entry(1), reg_entry(2), reg_entry(3, lifecycle={"status": "sunset"})]
        rc, _, _, posts = self.run_it(["--runtime", "compose", "--prom", self.prom, "--vantage", "vt"],
                                      registry=reg)
        self.assertEqual(rc, 0)
        text = Path(self.prom).read_text()
        self.assertIn('fleet_edge_probe_eligible_services{vantage="vt"} 2', text)
        self.assertIn('fleet_edge_probe_up{vantage="vt"} 1', text)
        bad = fep._result("c1", "c1.example.test", "compose", ["health_status"], {})
        rc, *_ = self.run_it(["--runtime", "compose"], registry=reg, probe_fn=lambda s, a: bad)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
