import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "extract-service-facts.py"
_spec = importlib.util.spec_from_file_location("extract_service_facts", SCRIPT)
esf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(esf)

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:  # CI's python:alpine image has no PyYAML; the builder does
    HAVE_YAML = False

REGISTRY = [
    {"id": "zone-transfer", "repo_url": "https://github.com/baditaflorin/go_zone_transfer"},
    {"id": "sitemap-finder", "repo_url": "https://github.com/baditaflorin/go-sitemap-finder.git"},
    {"id": "fleet-grafana", "repo_url": "https://github.com/baditaflorin/go-fleet-metrics-hub"},
    {"id": "fleet-discovery", "repo_url": "https://github.com/baditaflorin/go-fleet-metrics-hub"},
    {"id": "no-repo"},
]

ZONE = {
    "id": "go_zone_transfer",
    "api": {"endpoint": "/?api_key={token}", "method": "get",
            "params": [{"name": "domain", "type": "string", "required": True, "description": "Domain  to\n test"},
                       {"name": "mode", "required": "yes"},
                       {"description": "no name"},
                       "junk"]},
    "test": {"url": "/t/default_token/go_zone_transfer?domain=zonetransfer.me"},
}


class TestParse(unittest.TestCase):
    def test_params_keep_order_and_only_a_real_true_is_required(self):
        facts = esf.parse_service_yaml(ZONE)
        self.assertEqual(facts["params"], [
            {"name": "domain", "required": True, "type": "string", "description": "Domain to test", "example": None, "default": None},
            {"name": "mode", "required": False, "type": None, "description": None, "example": None, "default": None},
        ])
        self.assertEqual(facts["api"], {"endpoint": "/?api_key={token}", "method": "GET"})
        self.assertEqual(facts["test_url"], "/t/default_token/go_zone_transfer?domain=zonetransfer.me")

    def test_example_url_and_missing_blocks(self):
        self.assertEqual(esf.parse_service_yaml({"test": {"example_url": "/x?a=1"}})["test_url"], "/x?a=1")
        empty = esf.parse_service_yaml({"id": "x"})
        self.assertEqual((empty["test_url"], empty["params"], empty["api"]), ("", [], {"endpoint": None, "method": None}))
        self.assertIsNone(esf.parse_service_yaml(["not", "a", "mapping"]))
        self.assertIsNone(esf.parse_service_yaml(None))

    def test_yaml_dates_become_text(self):
        import datetime
        self.assertEqual(esf.plain({"d": datetime.date(2026, 1, 2), "l": [1, None]}), {"d": "2026-01-02", "l": [1, None]})


class TestBuildFacts(unittest.TestCase):
    def test_both_spellings_of_a_clone_collapse_to_the_exact_repo_name(self):
        a = {"test": {"url": "/?q=underscore"}}
        b = {"test": {"url": "/?q=dash"}}
        rows, left_out = esf.build_facts({"go_sitemap_finder": a, "go-sitemap-finder": b, "go_zone_transfer": ZONE,
                                          "go-fleet-metrics-hub": ZONE}, REGISTRY)
        self.assertEqual([r["id"] for r in rows], ["sitemap-finder", "zone-transfer"])
        self.assertEqual(rows[0]["test_url"], "/?q=dash")                # the repo_url basename is go-sitemap-finder
        self.assertEqual(rows[0]["repo"], "go-sitemap-finder")
        self.assertEqual(left_out[0][0], "gofleetmetricshub")

    def test_without_an_exact_spelling_the_first_by_name_wins(self):
        rows, _ = esf.build_facts({"Go_Zone_Transfer": {"test": {"url": "/?a=1"}}, "go-zone-transfer": {"test": {"url": "/?a=2"}}},
                                  REGISTRY)
        self.assertEqual(rows[0]["test_url"], "/?a=1")                   # "Go_Zone_Transfer" sorts before "go-zone-transfer"

    def test_shared_repository_unparsable_and_unknown_clones_are_left_out(self):
        rows, left_out = esf.build_facts({"go-fleet-metrics-hub": ZONE, "go_zone_transfer": None, "some-library": ZONE}, REGISTRY)
        self.assertEqual(rows, [])
        reasons = dict(left_out)
        self.assertIn("shared by 2 registry entries (fleet-discovery, fleet-grafana)", reasons["gofleetmetricshub"])
        self.assertIn("does not parse", reasons["zone-transfer"])
        self.assertNotIn("some-library", reasons)                          # not a registry service: silently not ours

    def test_output_does_not_depend_on_clone_order(self):
        clones = {"go_zone_transfer": ZONE, "go-sitemap-finder": {"test": {"url": "/?q=x"}}}
        a = esf.dumps(esf.facts_document(esf.build_facts(clones, REGISTRY)[0]))
        b = esf.dumps(esf.facts_document(esf.build_facts(dict(reversed(list(clones.items()))), list(reversed(REGISTRY)))[0]))
        self.assertEqual(a, b)
        self.assertEqual(json.loads(a)["schema"], 1)


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class TestReadClones(unittest.TestCase):
    def test_reads_every_service_yaml_and_survives_a_broken_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go_zone_transfer").mkdir()
            (root / "go_zone_transfer" / "service.yaml").write_text(
                "id: go_zone_transfer\napi:\n  method: GET\n  params:\n    - name: domain\n      required: true\n"
                "      description: >\n        Domain to\n        test\ntest:\n  url: /?domain=x.org\n")
            (root / "broken").mkdir()
            (root / "broken" / "service.yaml").write_text("a: [unclosed\n")
            (root / "no-yaml").mkdir()
            clones = esf.read_clones(root)
        self.assertEqual(sorted(clones), ["broken", "go_zone_transfer"])
        self.assertIsNone(clones["broken"])
        facts = esf.parse_service_yaml(clones["go_zone_transfer"])
        self.assertEqual(facts["params"][0]["description"], "Domain to test")
        self.assertTrue(facts["params"][0]["required"])


if __name__ == "__main__":
    unittest.main()
