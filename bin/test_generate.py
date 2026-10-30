#!/usr/bin/env python3
"""Smoke tests for bin/generate.py — pure-function checks, no GitHub I/O.

Run from the repo root:

    python3 -m unittest bin.test_generate
    # or
    python3 bin/test_generate.py

These tests cover the bits that have caused real bugs:
- `split_overrides` correctly fans out `$rules` + `$expand` metadata
  without leaking `$`-prefixed keys into the per-slug map.
- `expand_entry` produces one child entry per spec, with slug-derived
  url + health_url + name, and re-applies `$rules` so wildcard certs
  still pin (regression guard for the first user, go-fleet-metrics-hub).
- `replace_parent` semantics are obeyed by the build loop.

If the generator gains new pure functions worth gating, add cases here
rather than another dry-run as the only gate.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # type: ignore[import]
import generate  # type: ignore[import]
from check_service_version_regressions import semver_precedence  # type: ignore[import]


PARENT_FIXTURE = {
    "id":         "go-fleet-metrics-hub",
    "name":       "Go Fleet Metrics Hub",
    "mesh":       "0exec",
    "kind":       "container",
    "language":   "go",
    "runtime":    "compose",
    "category":   "observability",
    "url":        "https://go-fleet-metrics-hub.0exec.com",
    "health_url": "https://go-fleet-metrics-hub.0exec.com/_gw_health",
    "repo_url":   "https://github.com/baditaflorin/go-fleet-metrics-hub",
    "auth":       {"type": "api_key", "header": "X-API-Key"},
    "auth_help":  "API key required in X-API-Key or Authorization: Bearer",
    "tags":       [],
}

EXPAND_SPEC = {
    "name": "go-fleet-metrics-hub-children",
    "parent_repo": "go-fleet-metrics-hub",
    "replace_parent": True,
    "children": [
        {"id": "fleet-discovery",  "host_port": 18201, "container_port": 8080,
         "category": "observability", "description": "http_sd"},
        {"id": "fleet-grafana",    "host_port": 18202, "container_port": 3000,
         "category": "observability"},
        {"id": "fleet-prometheus", "host_port": 18203, "container_port": 18203,
         "category": "observability"},
    ],
}


class TestSelectedEntryMerge(unittest.TestCase):
    def test_replaces_only_requested_entry_and_preserves_order(self):
        existing = [{"id": "first", "version": "1.0.0"}, {"id": "target", "version": "1.0.0"}, {"id": "last"}]
        generated = [{"id": "target", "version": "1.0.1", "env": {"APP_VERSION": "1.0.1"}}]
        result = generate.merge_selected_entry(existing, generated, "target")
        self.assertEqual([entry["id"] for entry in result], ["first", "target", "last"])
        self.assertEqual(result[0], existing[0])
        self.assertEqual(result[2], existing[2])
        self.assertEqual(result[1], generated[0])

    def test_json_update_preserves_every_non_target_byte(self):
        blob = '[\n  {"id":"keep","env":{"A":"B"}},\n  {"id":"target","version":"1.0.0"}\n]\n'
        updated = generate.replace_selected_entry_json(blob, {"id": "target", "version": "1.0.1"}, "target")
        self.assertTrue(updated.startswith('[\n  {"id":"keep","env":{"A":"B"}},\n'))
        self.assertEqual(json.loads(updated)[1]["version"], "1.0.1")
        self.assertEqual(updated.splitlines()[0], blob.splitlines()[0])

    def test_several_ids_from_one_build_preserve_every_other_byte(self):
        blob = '[\n  {"id":"a","version":"1.0.0"},\n  {"id":"keep","env":{"A":"B"}},\n  {"id":"b","version":"2.0.0"}\n]\n'
        generated = [{"id": "a", "version": "1.0.1"}, {"id": "keep", "version": "9"}, {"id": "b", "version": "2.0.1"}]
        entries, updated = generate.update_selected_entries(blob, generated, ["a", "b"])
        self.assertEqual([e.get("version") for e in entries], ["1.0.1", None, "2.0.1"])
        self.assertIn('\n  {"id":"keep","env":{"A":"B"}},\n', updated)
        self.assertEqual(json.loads(updated), entries)

    def test_several_ids_fail_on_an_unknown_one(self):
        blob = '[{"id":"a","version":"1.0.0"}]'
        with self.assertRaisesRegex(ValueError, "existing entry"):
            generate.update_selected_entries(blob, [{"id": "a"}, {"id": "missing"}], ["a", "missing"])

    def test_requires_one_existing_and_generated_entry(self):
        with self.assertRaisesRegex(ValueError, "existing entry"):
            generate.merge_selected_entry([], [{"id": "target"}], "target")
        with self.assertRaisesRegex(ValueError, "generated entry"):
            generate.merge_selected_entry([{"id": "target"}], [], "target")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            generate.merge_selected_entry([{"id": "target"}, {"id": "target"}], [{"id": "target"}], "target")


class TestHeaderOnlyAuth(unittest.TestCase):
    def test_api_key_help_never_advertises_url_auth(self):
        self.assertEqual(
            generate.auth_help_for({"type": "api_key", "header": "X-API-Key"}),
            "API key required in X-API-Key or Authorization: Bearer",
        )

    def test_source_examples_strip_query_credentials(self):
        path = build._sanitize_example_path(
            "https://example.test/t/old-token/search?q=go&api_key=one&token=two&access_token=three&auth_token=four&apikey=five"
        )
        self.assertEqual(path, "/search?q=go")


class TestSplitOverrides(unittest.TestCase):
    def test_rules_and_expand_separated_from_slugs(self):
        raw = {
            "$rules":  [{"name": "r1", "match": {"mesh": "0exec"}, "patch": {"cert_domain": "wildcard.0exec.com"}}],
            "$expand": [EXPAND_SPEC],
            "a11y-quick":  {"trl": 6},
            "node-search": {"vhost": {"proxy_buffering": "off"}},
        }
        by_slug, rules, expansions, externals = generate.split_overrides(raw)
        self.assertEqual(set(by_slug.keys()), {"a11y-quick", "node-search"})
        self.assertEqual(len(rules), 1)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["parent_repo"], "go-fleet-metrics-hub")
        self.assertEqual(externals, [])

    def test_missing_metadata_keys_are_empty(self):
        by_slug, rules, expansions, externals = generate.split_overrides({"a11y-quick": {"trl": 6}})
        self.assertEqual(rules, [])
        self.assertEqual(expansions, [])
        self.assertEqual(externals, [])
        self.assertEqual(by_slug, {"a11y-quick": {"trl": 6}})

    def test_external_separated_from_slugs(self):
        raw = {
            "$external": [{"id": "plausible", "host_port": 18204,
                           "repo_url": "https://github.com/plausible/community-edition"}],
            "a11y-quick":  {"trl": 6},
        }
        by_slug, rules, expansions, externals = generate.split_overrides(raw)
        self.assertEqual(set(by_slug.keys()), {"a11y-quick"})
        self.assertEqual(len(externals), 1)
        self.assertEqual(externals[0]["id"], "plausible")


class TestQualityContractProjection(unittest.TestCase):
    def test_generator_applies_curated_catalog_version_override(self):
        repo = {
            "name": "go-version-example",
            "url": "https://github.com/baditaflorin/go-version-example",
            "description": "fixture",
            "repositoryTopics": [{"name": "mesh-0crawl"}],
        }
        entry = generate.make_entry(repo, {"version-example": {"version": "1.2.3"}}, [])
        self.assertEqual(entry["version"], "1.2.3")

    def test_explicit_auth_help_overrides_generic_auth_text(self):
        repo = {
            "name": "go-quality-example",
            "url": "https://github.com/baditaflorin/go-quality-example",
            "description": "fixture",
            "repositoryTopics": [{"name": "mesh-0exec"}],
        }
        auth_help = "Use the X-API-Key header only; query credentials are rejected."
        entry = generate.make_entry(repo, {"quality-example": {"auth_help": auth_help}}, [])
        self.assertEqual(entry["auth_help"], auth_help)

    def test_generator_keeps_quality_contract_private(self):
        repo = {
            "name": "go-quality-example",
            "url": "https://github.com/baditaflorin/go-quality-example",
            "description": "fixture",
            "repositoryTopics": [{"name": "mesh-0crawl"}],
        }
        contract = {
            "status": "operational",
            "dataset_url": "https://example.test/golden",
            "regression_command": "go test ./... -run Golden",
            "last_verified_at": "2026-09-13",
        }
        entry = generate.make_entry(repo, {"quality-example": {"quality_contract": contract}}, [])
        self.assertEqual(entry["quality_contract"], contract)
        self.assertNotIn("quality_contract", generate.to_public_entry(entry))

    def test_gateway_upstream_url_is_not_copied_from_public_overrides(self):
        origin = "https://domainscope.scrapetheworld.org"
        repo = {
            "name": "go-quality-example",
            "url": "https://github.com/baditaflorin/go-quality-example",
            "description": "fixture",
            "repositoryTopics": [{"name": "mesh-0exec"}],
        }
        entry = generate.make_entry(repo, {"quality-example": {"gateway_upstream_url": origin}}, [])
        self.assertNotIn("gateway_upstream_url", entry)


class TestGatewayRoutesStayPrivate(unittest.TestCase):
    def test_public_registry_sources_do_not_contain_gateway_upstreams(self):
        for filename in ("overrides.json", "services.json"):
            with self.subTest(filename=filename):
                payload = json.loads((Path(generate.ROOT) / filename).read_text())
                self.assertFalse(
                    _contains_key(payload, "gateway_upstream_url"),
                    f"{filename} contains operational gateway routing; move it to private fleet-state",
                )


class TestFleetGraphWriterRollout(unittest.TestCase):
    def test_next_five_writers_are_explicitly_enabled(self):
        payload = json.loads((Path(generate.ROOT) / "overrides.json").read_text())
        expected = (
            "company-size",
            "keyword-density",
            "language-detector",
            "leadership-extractor",
            "security-headers",
        )
        for service_id in expected:
            with self.subTest(service_id=service_id):
                env = payload[service_id]["env"]
                self.assertEqual(env.get("GRAPH_ENABLED"), "true")
                self.assertEqual(
                    env.get("GRAPH_COLLECTOR_URL"),
                    "https://fleet-graph.0exec.com",
                )


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


class TestExpandEntry(unittest.TestCase):
    def test_emits_one_entry_per_child_with_slug_derived_urls(self):
        children = generate.expand_entry(PARENT_FIXTURE, EXPAND_SPEC, by_slug={}, rules=[])
        self.assertEqual(len(children), 3)

        d = next(c for c in children if c["id"] == "fleet-discovery")
        self.assertEqual(d["url"],        "https://fleet-discovery.0exec.com")
        self.assertEqual(d["health_url"], "https://fleet-discovery.0exec.com/health")
        self.assertEqual(d["name"],       "Fleet Discovery")  # humanize()
        self.assertEqual(d["host_port"],      18201)
        self.assertEqual(d["container_port"], 8080)
        # Inherited from parent:
        self.assertEqual(d["mesh"], "0exec")
        self.assertEqual(d["repo_url"], PARENT_FIXTURE["repo_url"])
        self.assertEqual(d["auth"], PARENT_FIXTURE["auth"])

    def test_child_explicit_name_wins_over_humanize(self):
        spec = {
            "name": "x",
            "parent_repo": "go-fleet-metrics-hub",
            "children": [{"id": "fleet-prometheus", "name": "Custom Display Name",
                          "host_port": 18203, "container_port": 18203}],
        }
        children = generate.expand_entry(PARENT_FIXTURE, spec, by_slug={}, rules=[])
        self.assertEqual(children[0]["name"], "Custom Display Name")

    def test_rules_reapply_to_children(self):
        """A `$rules` entry matching the child's mesh must still apply —
        otherwise wildcard-cert-0exec would silently drop on expand-children
        and nginx-render would try to issue a per-vhost cert."""
        rules = [
            {"name": "wildcard-cert-0exec",
             "match": {"mesh": "0exec"},
             "patch": {"cert_domain": "wildcard.0exec.com"}},
        ]
        children = generate.expand_entry(PARENT_FIXTURE, EXPAND_SPEC, by_slug={}, rules=rules)
        for c in children:
            self.assertEqual(c.get("cert_domain"), "wildcard.0exec.com",
                             f"{c['id']} missing wildcard cert_domain")

    def test_per_slug_override_wins_over_rule(self):
        """Per-slug patch must beat $rules (matches the documented
        resolution order in load_overrides()'s docstring)."""
        rules = [
            {"name": "wildcard-cert-0exec",
             "match": {"mesh": "0exec"},
             "patch": {"cert_domain": "wildcard.0exec.com"}},
        ]
        by_slug = {"fleet-grafana": {
            "cert_domain": "fleet-grafana-special.0exec.com",
            "health_url": "https://fleet-grafana.0exec.com/_gw_health",
        }}
        children = generate.expand_entry(PARENT_FIXTURE, EXPAND_SPEC, by_slug=by_slug, rules=rules)
        grafana = next(c for c in children if c["id"] == "fleet-grafana")
        self.assertEqual(grafana["cert_domain"], "fleet-grafana-special.0exec.com")
        self.assertEqual(grafana["health_url"], "https://fleet-grafana.0exec.com/_gw_health")

    def test_child_without_id_is_a_hard_error(self):
        bad_spec = {"name": "broken", "parent_repo": "x",
                    "children": [{"host_port": 18999}]}  # no id
        with self.assertRaises(SystemExit):
            generate.expand_entry(PARENT_FIXTURE, bad_spec, by_slug={}, rules=[])


class Test0DockerMesh(unittest.TestCase):
    def test_legacy_hostname_override_keeps_its_actual_edge(self):
        repo = {
            "name": "go_kokoro_ovms_adapter",
            "description": "adapter",
            "homepageUrl": "",
            "url": "https://github.com/baditaflorin/go_kokoro_ovms_adapter",
            "repositoryTopics": [
                {"name": "mesh-0docker"},
                {"name": "category-infrastructure"},
            ],
        }
        override = {
            "url": "https://kokoro.voice.0docker.com",
            "health_url": "https://kokoro.voice.0docker.com/health",
        }
        entry = generate.make_entry(repo, {"kokoro-ovms-adapter": override}, [])
        self.assertEqual(entry["mesh"], "0docker")
        self.assertEqual(entry["url"], override["url"])
        self.assertEqual(entry["health_url"], override["health_url"])
        self.assertEqual(entry["cert_domain"], "wildcard.0docker.com")


class TestCustomMesh(unittest.TestCase):
    """mesh `custom` lets a service register on its own domain (not a
    fleet apex) with ONE override key instead of separately overriding
    url + auth + cert_domain + network_exposure and hoping nothing was
    forgotten. Added 2026-09-23 after onboarding hub_scrapetheworld_org
    needed exactly that four-field manual override just to describe
    "this service lives on hub.scrapetheworld.org" -- see
    baditaflorin/hub_scrapetheworld_org#28."""

    def _repo(self):
        return {
            "name": "hub_scrapetheworld_org",
            "description": "hub dashboard",
            "homepageUrl": "",
            "url": "https://github.com/baditaflorin/hub_scrapetheworld_org",
            "repositoryTopics": [
                {"name": "mesh-custom"},
                {"name": "category-dashboard"},
            ],
        }

    def test_domain_override_derives_url_auth_and_cert_domain_together(self):
        override = {"domain": "hub.scrapetheworld.org"}
        entry = generate.make_entry(self._repo(), {"hub-scrapetheworld-org": override}, [])
        self.assertEqual(entry["mesh"], "custom")
        self.assertEqual(entry["url"], "https://hub.scrapetheworld.org")
        self.assertEqual(entry["health_url"], "https://hub.scrapetheworld.org/health")
        self.assertEqual(entry["auth"]["type"], "none")
        self.assertEqual(entry["cert_domain"], "wildcard.scrapetheworld.org")
        # auth.type=none + cert_domain set -> compute_network_exposure's
        # existing, mesh-agnostic derivation should land on gateway-public
        # with zero custom-mesh-specific code in that function.
        self.assertEqual(entry["network_exposure"], "gateway-public")

    def test_explicit_cert_domain_override_still_wins(self):
        # The copy-through loop applies ov["cert_domain"] after the
        # domain-derived default -- confirms mesh `custom` doesn't bypass
        # that existing escape hatch for a multi-label-TLD apex the
        # registrable_domain() heuristic would get wrong (e.g. co.uk).
        override = {"domain": "hub.scrapetheworld.org", "cert_domain": "wildcard.example.co.uk"}
        entry = generate.make_entry(self._repo(), {"hub-scrapetheworld-org": override}, [])
        self.assertEqual(entry["cert_domain"], "wildcard.example.co.uk")

    def test_missing_domain_override_is_a_hard_error(self):
        # Fail loud, not a garbage URL -- an unregistered custom-domain
        # service is a config gap to fix, never a default to paper over.
        with self.assertRaises(ValueError):
            generate.make_entry(self._repo(), {}, [])

    def test_registrable_domain_takes_last_two_labels(self):
        self.assertEqual(generate.registrable_domain("hub.scrapetheworld.org"), "scrapetheworld.org")
        self.assertEqual(generate.registrable_domain("a.b.c.example.com"), "example.com")
        self.assertEqual(generate.registrable_domain("example.com"), "example.com")

    def test_registrable_domain_handles_multi_label_public_suffixes(self):
        r = generate.registrable_domain
        self.assertEqual(r("shop.example.co.uk"), "example.co.uk")
        self.assertEqual(r("example.co.uk"), "example.co.uk")
        self.assertEqual(r("a.b.example.com.au"), "example.com.au")
        self.assertEqual(r("hub.firma.com.ro"), "firma.com.ro")
        self.assertEqual(r("myapp.github.io"), "myapp.github.io")
        self.assertEqual(r("x.y.myapp.vercel.app"), "myapp.vercel.app")

    def test_registrable_domain_edge_cases(self):
        r = generate.registrable_domain
        # case-insensitive suffix match, original label case preserved
        self.assertEqual(r("Shop.Example.CO.UK"), "Example.CO.UK")
        # trailing dot (FQDN form)
        self.assertEqual(r("hub.scrapetheworld.org."), "scrapetheworld.org")
        # a bare public suffix or a single label is returned unchanged
        self.assertEqual(r("co.uk"), "co.uk")
        self.assertEqual(r("localhost"), "localhost")
        # unknown two-label suffix falls back to the last two labels
        self.assertEqual(r("a.b.example.xyz"), "example.xyz")

    def test_registrable_domain_unchanged_for_every_domain_the_fleet_uses(self):
        # Regression guard: the fleet's own apexes and the custom domain are plain
        # single-label TLDs; the multi-label table must not touch them.
        for fqdn, want in [
            ("x.0exec.com", "0exec.com"), ("x.0crawl.com", "0crawl.com"),
            ("x.0docker.com", "0docker.com"), ("hub.scrapetheworld.org", "scrapetheworld.org"),
            ("a.badita.org", "badita.org"), ("openobserve.0own.com", "0own.com"),
        ]:
            self.assertEqual(generate.registrable_domain(fqdn), want)

    def test_custom_mesh_wildcard_cert_domain_uses_public_suffix(self):
        self.assertEqual(generate.wildcard_cert_domain("custom", "shop.example.co.uk"), "wildcard.example.co.uk")
        self.assertEqual(generate.wildcard_cert_domain("custom", "hub.scrapetheworld.org"), "wildcard.scrapetheworld.org")


class TestExternalEntry(unittest.TestCase):
    """$external is the registry's hook for third-party / upstream
    containers that run on the dockerhost but aren't in the fleet repo
    set (e.g. plausible). One row makes their host_port visible to
    allocate-port — see ADR-0031."""

    def test_minimal_spec_produces_complete_entry(self):
        e = generate.make_external_entry({
            "id": "plausible",
            "host_port": 18204,
            "repo_url": "https://github.com/plausible/community-edition",
        })
        self.assertEqual(e["id"], "plausible")
        self.assertEqual(e["host_port"], 18204)
        self.assertEqual(e["runtime"], "external")
        self.assertEqual(e["kind"], "container")
        self.assertEqual(e["auth"]["type"], "none")
        self.assertEqual(e["auth_help"], "no auth")
        self.assertEqual(e["name"], "Plausible")  # humanize(slug)

    def test_missing_host_port_is_hard_error(self):
        with self.assertRaises(SystemExit):
            generate.make_external_entry({
                "id": "plausible",
                "repo_url": "https://github.com/plausible/community-edition",
            })

    def test_missing_id_is_hard_error(self):
        with self.assertRaises(SystemExit):
            generate.make_external_entry({
                "host_port": 18204,
                "repo_url": "https://github.com/plausible/community-edition",
            })

    def test_missing_repo_url_is_hard_error(self):
        with self.assertRaises(SystemExit):
            generate.make_external_entry({"id": "plausible", "host_port": 18204})

    def test_explicit_fields_win_over_defaults(self):
        e = generate.make_external_entry({
            "id": "plausible",
            "name": "Plausible Analytics",
            "host_port": 18204,
            "container_port": 8000,
            "repo_url": "https://github.com/plausible/community-edition",
            "category": "observability",
            "url": "http://dockerhost.invalid:18204",
            "health_url": "http://dockerhost.invalid:18204/api/health",
            "external_compose_dir": "/opt/services/plausible/",
            "external_image": "ghcr.io/plausible/community-edition:v3.2.1",
        })
        self.assertEqual(e["name"], "Plausible Analytics")
        self.assertEqual(e["category"], "observability")
        self.assertEqual(e["container_port"], 8000)
        self.assertEqual(e["url"], "http://dockerhost.invalid:18204")
        self.assertEqual(e["external_compose_dir"], "/opt/services/plausible/")
        self.assertEqual(e["external_image"], "ghcr.io/plausible/community-edition:v3.2.1")


class TestPrivateServiceContract(unittest.TestCase):
    """Private rows are runner-only opaque metadata, never catalog data."""

    ENTRY = {
        "id": "fleet-build-broker",
        "name": "Fleet Build Broker",
        "description": "Constrained artifact broker for approved fleet builds.",
        "category": "infrastructure",
        "kind": "container",
        "language": "go",
        "runtime": "compose",
        "visibility": "private",
        "cluster": "0mcp",
        "deployment_mode": "iac-rendered",
        "host_port": 18100,
        "container_port": 5001,
    }

    def test_checked_in_private_record_is_schema_shaped_and_explicitly_placed(self):
        entries = generate.load_private_services()
        self.assertEqual(entries, [self.ENTRY])
        generate.validate_private_cluster_placements(entries)

        placement = json.loads((Path(generate.ROOT) / "clusters.json").read_text())
        self.assertEqual(
            placement,
            {
                "placement": {
                    "fleet-build-broker": "0mcp",
                    "country-iso-matcher": "lv3",
                    "reverse-dns": "lv3",
                    "github-app-token-broker": "domainscope",
                }
            },
        )

        rendered = json.loads((Path(generate.ROOT) / "services.json").read_text())
        self.assertEqual(
            [entry for entry in rendered if entry["id"] == "fleet-build-broker"],
            [self.ENTRY],
        )

        schema = json.loads((Path(generate.ROOT) / "schema/private-services.v1.json").read_text())
        item = schema["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), generate.PRIVATE_SERVICE_REQUIRED_FIELDS)
        self.assertEqual(item["properties"]["visibility"], {"const": "private"})
        self.assertEqual(item["properties"]["cluster"], {"const": "0mcp"})
        self.assertEqual(item["properties"]["deployment_mode"]["const"], "iac-rendered")

        schema_v1 = json.loads((Path(generate.ROOT) / "schema/v1.json").read_text())
        private_rule = schema_v1["allOf"][0]["then"]
        self.assertIn("deployment_mode", private_rule["required"])
        self.assertEqual(
            private_rule["properties"]["deployment_mode"],
            {"const": "iac-rendered"},
        )

    def test_rejects_catalog_topology_or_credential_fields(self):
        for field, value in {
            "url": "https://not-allowed.example",
            "health_url": "https://not-allowed.example/health",
            "repo_url": "https://not-allowed.example/repo",
            "auth": {"type": "api_key"},
            "cert_domain": "not-allowed.example",
            "server_name": "not-allowed.example",
            "key_file": "/not-allowed/key",
        }.items():
            with self.subTest(field=field):
                bad = dict(self.ENTRY, **{field: value})
                with self.assertRaises(SystemExit):
                    generate.validate_private_service_entry(bad)

    def test_rejects_missing_or_non_0mcp_placement(self):
        bad = dict(self.ENTRY)
        bad.pop("cluster")
        with self.assertRaises(SystemExit):
            generate.validate_private_service_entry(bad)

    def test_rejects_missing_or_non_iac_rendered_deployment_mode(self):
        missing = dict(self.ENTRY)
        missing.pop("deployment_mode")
        with self.assertRaises(SystemExit):
            generate.validate_private_service_entry(missing)

        wrong = dict(self.ENTRY, deployment_mode="runner-compose")
        with self.assertRaises(SystemExit):
            generate.validate_private_service_entry(wrong)

    def test_rejects_a_missing_or_divergent_cluster_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clusters.json"
            path.write_text('{"placement":{"fleet-build-broker":"0docker"}}')
            with self.assertRaises(SystemExit):
                generate.validate_private_cluster_placements([self.ENTRY], path)

        bad = dict(self.ENTRY, cluster="0docker")
        with self.assertRaises(SystemExit):
            generate.validate_private_service_entry(bad)

    def test_every_catalog_projection_starts_from_the_shared_private_filter(self):
        public = dict(PARENT_FIXTURE)
        private = dict(self.ENTRY)
        self.assertEqual(generate.catalog_entries([public, private]), [public])
        for name, projection in generate.PROJECTIONS.items():
            projected = [value for value in (projection(entry) for entry in generate.catalog_entries([public, private])) if value is not None]
            self.assertNotIn("fleet-build-broker", str(projected), name)

    def test_public_mirror_omits_the_private_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "services-public.json"
            with patch.object(generate, "SERVICES_PUBLIC_JSON", target):
                count, _ = generate.write_public_mirror([PARENT_FIXTURE, self.ENTRY])
            self.assertEqual(count, 1)
            self.assertEqual([entry["id"] for entry in json.loads(target.read_text())], ["go-fleet-metrics-hub"])


class TestPublicMirror(unittest.TestCase):
    """G3: services-public.json sanitization. Allowlist-only — these tests
    guard the public surface from accidental widening when new fields get
    added to services.json."""

    FULL_ENTRY = {
        "id": "example-svc",
        "name": "Example Svc",
        "description": "Example microservice",
        "category": "domains",
        "mesh": "0crawl",
        "kind": "container",
        "language": "go",
        "runtime": "compose",
        "tags": ["go", "domains"],
        "url": "https://example-svc.0crawl.com",
        "health_url": "https://example-svc.0crawl.com/health",
        "repo_url": "https://github.com/baditaflorin/go_example",
        "example_path": "/?target=example.com",
        "auth": {
            "type": "api_key",
            "header": "X-API-Key",
            "key_value": "must-not-be-published",
        },
        "auth_help": "API key required in X-API-Key or Authorization: Bearer",
        # Internal / disclosure-risk fields — must be dropped:
        "host_port": 18999,
        "container_port": 8999,
        "cert_domain": "wildcard.0crawl.com",
        "proxy_egress": True,
        "internal_direct": True,
        "ui_cookie_bridge": True,
        "served_by": "domainscope",
        "network_exposure": "gateway-ip-allowlisted",
        "allowed_source_ips": ["65.108.75.123/32"],
        "extra_server_names": ["alt.0crawl.com"],
        "vhost": {"proxy_buffering": "off"},
        "depends_on": ["other-svc"],
        "graph_target_hosts": ["html-proxy.0exec.com"],
        "trl_evidence": "long internal commentary referencing ADR-0018",
        "trl": 6,
        "trl_ceiling": 7,
        "trl_ceiling_reason": "needs paid threat intel",
        "trl_assessed_at": "2026-05-16",
        "trl_assessor": "claude-opus-4-7-session-2026-05-16",
        "quality_contract": {"status": "operational", "dataset_url": "https://example.test/data", "regression_command": "go test ./...", "last_verified_at": "2026-09-13"},
    }

    def test_drops_all_internal_fields(self):
        pub = generate.to_public_entry(self.FULL_ENTRY)
        for forbidden in ("host_port", "container_port", "cert_domain",
                          "proxy_egress", "internal_direct", "ui_cookie_bridge", "served_by", "network_exposure",
                          "allowed_source_ips",
                          "extra_server_names", "vhost", "depends_on",
                          "trl_evidence", "quality_contract"):
            self.assertNotIn(forbidden, pub,
                f"public mirror leaked internal field {forbidden!r}")

    def test_keeps_canonical_public_fields(self):
        pub = generate.to_public_entry(self.FULL_ENTRY)
        for required in ("id", "name", "description", "category",
                         "mesh", "kind", "language", "runtime",
                         "url", "health_url", "repo_url", "graph_target_hosts",
                         "auth", "auth_help", "trl"):
            self.assertIn(required, pub,
                f"public mirror dropped canonical field {required!r}")

    def test_public_auth_projection_drops_key_values_and_query_auth(self):
        """Public auth metadata keeps only the service key type and header."""
        pub = generate.to_public_entry(self.FULL_ENTRY)
        self.assertEqual(pub["auth"], {
            "type": "api_key",
            "header": "X-API-Key",
        })

    def test_unknown_future_field_is_dropped(self):
        """Allowlist-only: a brand-new field in services.json must NOT
        appear in services-public.json until someone explicitly extends
        PUBLIC_FIELDS. Guards against silent public-surface widening."""
        entry = dict(self.FULL_ENTRY, mystery_new_field="oops")
        pub = generate.to_public_entry(entry)
        self.assertNotIn("mystery_new_field", pub)

    def test_rename_fields_are_public(self):
        """Old hostnames are already in public DNS as 301 redirect targets
        — exposing the alias map lets external bookmark-followers resolve
        old slugs."""
        entry = dict(self.FULL_ENTRY,
                     aliases=["go-example"],
                     alias_urls=["go-example.0crawl.com"],
                     rename_status="redirect",
                     rename_retire_at="2026-06-18")
        pub = generate.to_public_entry(entry)
        for k in ("aliases", "alias_urls", "rename_status", "rename_retire_at"):
            self.assertIn(k, pub)

    def test_access_tier_is_public(self):
        """access_tier tells a prospective caller which trust tier a gated
        tool needs — withholding it wouldn't protect anything the
        keystore doesn't already gate, so unlike scope/proxy_egress/etc.
        it's allowlisted, not stripped."""
        entry = dict(self.FULL_ENTRY, access_tier="vetted-pentest")
        pub = generate.to_public_entry(entry)
        self.assertEqual(pub.get("access_tier"), "vetted-pentest")


class TestMCPProjection(unittest.TestCase):
    """services.mcp.json — what go-fleet-mcp-gateway and the site read to
    decide what's aggregated and what tier it requires."""

    def test_access_tier_included_when_present(self):
        project = generate.PROJECTIONS["services.mcp.json"]
        entry = {"id": "pentest-nuclei", "name": "Nuclei", "url": "https://x",
                  "mcp_ready": True, "access_tier": "vetted-pentest"}
        self.assertEqual(project(entry).get("access_tier"), "vetted-pentest")

    def test_access_tier_absent_when_unset(self):
        """Most services have no access_tier override at all — _pick's
        'omit, don't null' semantics mean the key simply shouldn't appear,
        not appear as access_tier: null."""
        project = generate.PROJECTIONS["services.mcp.json"]
        entry = {"id": "open-tool", "name": "Open Tool", "url": "https://x",
                  "mcp_ready": True}
        self.assertNotIn("access_tier", project(entry))

    def test_non_mcp_ready_entry_still_filtered_out(self):
        """Adding access_tier to the projected keys must not loosen the
        existing mcp_ready predicate — a tiered-but-not-ready service is
        still excluded entirely, not included in some half-ready state."""
        project = generate.PROJECTIONS["services.mcp.json"]
        entry = {"id": "not-ready", "name": "Not Ready", "url": "https://x",
                  "access_tier": "vetted-pentest"}
        self.assertIsNone(project(entry))


class TestAllowedSourceIPs(unittest.TestCase):
    BASE = {
        "id": "apikey-service",
        "network_exposure": "gateway-ip-allowlisted",
        "auth": {"type": "api_key"},
    }

    def test_accepts_only_exact_host_cidrs(self):
        for value in ("65.108.75.123/32", "2001:db8::1/128"):
            with self.subTest(value=value):
                generate.validate_allowed_source_ips({
                    **self.BASE, "allowed_source_ips": [value]
                })

    def test_rejects_broad_or_noncanonical_entries(self):
        for value in (
            "65.108.75.0/24", "0.0.0.0/0", "65.108.75.123/31",
            "2001:db8::/64", "65.108.75.123", "65.108.75.123/32/",
            "example.com/32", "2001:DB8::1/128",
        ):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                generate.validate_allowed_source_ips({
                    **self.BASE, "allowed_source_ips": [value]
                })

    def test_rejects_empty_duplicates_non_strings_and_wrong_scope(self):
        bad_entries = (
            {"allowed_source_ips": []},
            {"allowed_source_ips": ["65.108.75.123/32"] * 2},
            {"allowed_source_ips": [123]},
            {"allowed_source_ips": ["65.108.75.123/32"],
             "network_exposure": "gateway-public"},
            {"allowed_source_ips": ["65.108.75.123/32"],
             "auth": {"type": "none"}},
        )
        for change in bad_entries:
            with self.subTest(change=change), self.assertRaises(SystemExit):
                generate.validate_allowed_source_ips({**self.BASE, **change})

    def test_omitted_metadata_remains_valid(self):
        generate.validate_allowed_source_ips(self.BASE)


class TestGatewayRateLimit(unittest.TestCase):
    BASE = {
        "id": "infrastructure-fetch-cache",
        "network_exposure": "gateway-ip-allowlisted",
        "auth": {"type": "api_key"},
    }

    def test_accepts_exemption_on_keystore_gated_allowlisted_vhost(self):
        generate.validate_gateway_rate_limit({
            **self.BASE, "gateway_rate_limit": "allowlisted-sources-exempt"
        })

    def test_accepts_explicit_default_anywhere(self):
        for exposure in ("gateway-ip-allowlisted", "gateway-authenticated", "gateway-public"):
            with self.subTest(exposure=exposure):
                generate.validate_gateway_rate_limit({
                    **self.BASE, "network_exposure": exposure,
                    "gateway_rate_limit": "api_per_ip",
                })

    def test_refuses_exemption_where_the_internet_reaches(self):
        for change in (
            {"network_exposure": "gateway-authenticated"},
            {"network_exposure": "gateway-public"},
            {"network_exposure": None},
            {"auth": {"type": "none"}},
            {"auth": None},
        ):
            with self.subTest(change=change), self.assertRaises(SystemExit):
                generate.validate_gateway_rate_limit({
                    **self.BASE, **change,
                    "gateway_rate_limit": "allowlisted-sources-exempt",
                })

    def test_refuses_unknown_values(self):
        for value in ("off", "", "Allowlisted-Sources-Exempt", True, None, ["api_per_ip"]):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                generate.validate_gateway_rate_limit({**self.BASE, "gateway_rate_limit": value})

    def test_omitted_metadata_remains_valid(self):
        generate.validate_gateway_rate_limit(self.BASE)

    def test_fetch_cache_override_validates(self):
        overrides = json.loads((Path(generate.ROOT) / "overrides.json").read_text())
        entry = overrides["infrastructure-fetch-cache"]
        self.assertEqual(entry.get("gateway_rate_limit"), "allowlisted-sources-exempt")
        generate.validate_gateway_rate_limit({**self.BASE, **entry, "id": "infrastructure-fetch-cache"})

    def test_kept_out_of_the_public_projection(self):
        self.assertNotIn("gateway_rate_limit", generate.PUBLIC_FIELDS)


class TestComputeNetworkExposure(unittest.TestCase):
    """compute_network_exposure() derives network_exposure from the same
    signals that drive nginx-render's template — regression guard for the
    services.json migration off the old binary `scope` field. Ratios below
    match the real 519-entry registry at migration time (2026-08-26):
    346 gateway-authenticated / 15 gateway-public / 13 gateway-ip-allowlisted
    / 143 static (omitted) / 2 manual lan-internal overrides."""

    def test_static_kind_is_not_applicable(self):
        entry = {"kind": "static", "cert_domain": None, "auth": {"type": "none"}}
        self.assertIsNone(generate.compute_network_exposure(entry, {}))

    def test_cert_domain_with_api_key_is_gateway_authenticated(self):
        entry = {"kind": "container", "cert_domain": "wildcard.0exec.com",
                  "auth": {"type": "api_key"}}
        self.assertEqual(generate.compute_network_exposure(entry, {}),
                          "gateway-authenticated")

    def test_cert_domain_with_path_token_is_gateway_authenticated(self):
        entry = {"kind": "container", "cert_domain": "wildcard.0crawl.com",
                  "auth": {"type": "path_token"}}
        self.assertEqual(generate.compute_network_exposure(entry, {}),
                          "gateway-authenticated")

    def test_cert_domain_with_no_auth_is_gateway_public(self):
        entry = {"kind": "container", "cert_domain": "wildcard.0exec.com",
                  "auth": {"type": "none"}}
        self.assertEqual(generate.compute_network_exposure(entry, {}),
                          "gateway-public")

    def test_scope_internal_only_wins_over_auth_type(self):
        """scope: internal-only is the exact successor signal for
        gateway-ip-allowlisted — it must win even when auth.type would
        otherwise imply gateway-authenticated (13 real entries do both)."""
        entry = {"kind": "container", "cert_domain": "wildcard.0exec.com",
                  "auth": {"type": "api_key"}}
        ov = {"scope": "internal-only"}
        self.assertEqual(generate.compute_network_exposure(entry, ov),
                          "gateway-ip-allowlisted")

    def test_no_cert_domain_uses_manual_override(self):
        """No vhost intent declared — registry data alone can't tell
        loopback from lan-internal, so it must come from an explicit
        overrides.json value (the claudia/plausible case)."""
        entry = {"kind": "container", "cert_domain": None, "auth": {"type": "none"}}
        ov = {"network_exposure": "lan-internal"}
        self.assertEqual(generate.compute_network_exposure(entry, ov),
                          "lan-internal")

    def test_no_cert_domain_no_override_is_none(self):
        entry = {"kind": "container", "cert_domain": None, "auth": {"type": "none"}}
        self.assertIsNone(generate.compute_network_exposure(entry, {}))

    def test_invalid_manual_override_is_ignored(self):
        entry = {"kind": "container", "cert_domain": None, "auth": {"type": "none"}}
        ov = {"network_exposure": "definitely-not-a-real-value"}
        self.assertIsNone(generate.compute_network_exposure(entry, ov))


class TestFleetGraphExpansionBatch(unittest.TestCase):
    def test_expansion_five_writers_are_explicitly_enabled(self):
        payload = json.loads((Path(generate.ROOT) / "overrides.json").read_text())
        # The version each writer was enabled for Fleet Graph at. The pin may
        # move past it as the service is released (it tracks the deployed
        # version); it must not fall below it.
        enabled_at = {
            "scc": "2.5.4",
            "seasonal-detector": "2.7.2",
            "team-member-extractor": "0.2.3",
            "tos-finder": "1.9.2",
            "substack-scraper": "1.1.6",
        }
        for service_id, version in enabled_at.items():
            with self.subTest(service_id=service_id):
                env = payload[service_id]["env"]
                pinned = semver_precedence(payload[service_id]["version"])
                self.assertIsNotNone(pinned)
                self.assertGreaterEqual(pinned, semver_precedence(version))
                self.assertEqual(env.get("GRAPH_ENABLED"), "true")
                self.assertEqual(env.get("GRAPH_SERVICE_ID"), service_id)
                self.assertEqual(
                    env.get("GRAPH_COLLECTOR_URL"),
                    "https://fleet-graph.0exec.com",
                )



class TestRegistryFilesAgree(unittest.TestCase):
    """services.json is generated from overrides.json. A value only
    overrides.json can supply must match it, or the next regeneration
    silently changes it: on 2026-10-16 four slugs were defined twice (the
    generator kept the second block and would have dropped their ports,
    placement and version) and 26 versions had been edited in services.json
    only (a regeneration would have lowered or dropped them)."""

    @classmethod
    def setUpClass(cls):
        root = Path(generate.ROOT)
        cls.overrides_text = (root / "overrides.json").read_text()
        cls.services = json.loads((root / "services.json").read_text())

    def test_overrides_json_has_no_duplicate_keys(self):
        duplicates = []

        def hook(pairs):
            seen = set()
            for key, _ in pairs:
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
            return dict(pairs)

        json.loads(self.overrides_text, object_pairs_hook=hook)
        self.assertEqual(duplicates, [], "json.loads keeps only the last copy of a duplicated key: merge them")

    def test_services_json_versions_match_overrides(self):
        by_slug, rules, expansions, externals = generate.split_overrides(json.loads(self.overrides_text))
        children = {child["id"]: child for spec in expansions for child in spec.get("children") or []}
        external_ids = {spec["id"] for spec in externals}
        mismatches = []
        for entry in self.services:
            service_id = entry.get("id")
            if service_id in external_ids:
                want = None  # make_external_entry never copies a version
            else:
                patch, _ = generate.resolved_overrides_for(entry, by_slug, rules)
                want = patch.get("version")
                if want is None and service_id in children:
                    want = children[service_id].get("version")
                    if want is None:
                        continue  # an $expand child without its own pin inherits the parent's
            if entry.get("version") != want:
                mismatches.append(f"{service_id}: services.json {entry.get('version')!r}, overrides.json {want!r}")
        self.assertEqual(mismatches, [], "set the version in overrides.json and run bin/generate.py --only <ids>")

_ssp_spec = importlib.util.spec_from_file_location("sync_service_params", Path(__file__).resolve().parent / "sync-service-params.py")
_ssp = importlib.util.module_from_spec(_ssp_spec)
_ssp_spec.loader.exec_module(_ssp)
PUBLIC_SAMPLE_JWT = _ssp.PUBLIC_SAMPLE_JWT

_ALLOW_BODY = "POST-only service: its required params are the JSON request body, a GET link cannot carry them"
_ALLOW_PATH = "the required param is a {placeholder} of the endpoint's path, not a query param"
_ALLOW_NO_SAFE = "a required param with no safe example value (a credential, or an id nobody can guess)"


class TestParamsAndExamples(unittest.TestCase):
    """`params` (each service's declared inputs, from its own service.yaml) rides through overrides.json -> services.json
    and the urls slice, and a service's `example_path` must carry every required param: the hub builds its Open /
    Run example links from it, and a link without one answers a 400 (on 2026-10-29, 161 of the 251 services that
    declare a required param had such an example)."""

    # (id, reason) for every service whose example_path cannot carry a required param; keep it explicit and short.
    ALLOWED = (
        [(i, _ALLOW_BODY) for i in (
            "agent-approval", "agent-mailbox", "agent-quorum", "fleet-backoff-coordinator", "fleet-body-redactor",
            "fleet-budget-tracker", "fleet-content-normalizer", "fleet-diff-engine", "fleet-eventlog", "fleet-exec",
            "fleet-fingerprint-cache", "fleet-har-builder", "fleet-lock", "fleet-memory", "fleet-poc-curl",
            "fleet-preflight", "fleet-resolver-quorum", "fleet-schema-validator", "fleet-target-reputation",
            "fleet-webhook-verifier", "pentest-agent-state", "pentest-asset-inventory", "pentest-audit-log",
            "pentest-continuous-monitor", "pentest-dedup-fingerprint", "pentest-exploit-verifier",
            "pentest-finding-triage", "pentest-findings-store", "pentest-http-replay", "pentest-notify", "pentest-nuclei",
            "pentest-orchestrator", "pentest-rate-coordinator", "pentest-scope-guard", "pentest-submit-bot",
            "pentest-triager-listener", "pentest-trufflehog", "pentest-walkthrough")]
        + [(i, _ALLOW_PATH) for i in ("fleet-dig", "fleet-payload-corpus", "fleet-persona", "fleet-secrets")]
        + [(i, _ALLOW_NO_SAFE) for i in ("pentest-asset-scope-resolver", "pentest-payloads")]
    )

    @staticmethod
    def missing_required(entry):
        """Names of the required params the entry's example_path does not carry with a value."""
        query = {k for k, v in parse_qsl(entry.get("example_path", "").partition("?")[2], keep_blank_values=True) if v}
        return [p["name"] for p in entry.get("params") or [] if p.get("required") and p["name"] not in query]

    @classmethod
    def setUpClass(cls):
        root = Path(generate.ROOT)
        cls.services = json.loads((root / "services.json").read_text())
        cls.overrides = json.loads((root / "overrides.json").read_text())
        cls.urls = json.loads((root / "services.urls.json").read_text())

    def test_every_example_carries_its_required_params(self):
        allowed = {i for i, _ in self.ALLOWED}
        broken = [f"{e['id']}: example_path {e.get('example_path')!r} lacks required {self.missing_required(e)}"
                  for e in self.services if self.missing_required(e) and e["id"] not in allowed]
        self.assertEqual(broken, [], "fix example_path in overrides.json (bin/sync-service-params.py derives one from "
                                     "service.yaml), or allow-list the service here with the reason it cannot carry it")

    def test_the_allow_list_has_no_stale_entries(self):
        by_id = {e["id"]: e for e in self.services}
        self.assertEqual(len({i for i, _ in self.ALLOWED}), len(self.ALLOWED), "an id is allow-listed twice")
        stale = [i for i, _ in self.ALLOWED if i not in by_id or not self.missing_required(by_id[i])]
        self.assertEqual(stale, [], "these now carry their required params (or are gone): remove them from the allow-list")

    def test_the_gate_catches_a_broken_example(self):
        """The mutation check: break an example the way it was broken before and the gate must see it."""
        entry = next(e for e in self.services if e["id"] == "zone-transfer")
        self.assertEqual(self.missing_required(entry), [])
        self.assertEqual(self.missing_required(dict(entry, example_path="/?url=https://example.com")), ["domain"])
        self.assertEqual(self.missing_required(dict(entry, example_path="/go_zone_transfer?domain=")), ["domain"])
        self.assertEqual(self.missing_required(dict(entry, example_path="/")), ["domain"])
        self.assertEqual(self.missing_required({"example_path": "/", "params": [{"name": "q", "required": False}]}), [])

    def test_no_example_carries_a_key_and_no_param_a_secret_shaped_value(self):
        examples = json.dumps([e.get("example_path") for e in self.services])
        self.assertNotRegex(examples, r"(?i)api_?key=|/t/[^/\"]+/")        # the hub adds the caller's key at click time
        declared = json.dumps([e.get("params") for e in self.services])
        # the jwt.io sample JWT is the one documented public value that may appear (bin/sync-service-params.py PUBLIC_SAMPLE_JWT)
        self.assertNotRegex(declared.replace(PUBLIC_SAMPLE_JWT, ""),
                            r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{10,}|sk_live_|sk_test_|\bfb_[a-z0-9]{20,}|\b[a-f0-9]{40,}\b")
        self.assertIsNone(generate.assert_no_secrets(self.services))

    def test_jwt_debugger_example_is_the_public_sample_and_it_is_the_only_jwt_in_any_declared_param(self):
        by_id = {e["id"]: e for e in self.services}
        e = by_id["jwt-debugger"]
        self.assertEqual(e["example_path"], "/?token=" + PUBLIC_SAMPLE_JWT)
        self.assertEqual(self.missing_required(e), [])
        token = next(p for p in e["params"] if p["name"] == "token")
        self.assertEqual(token["example"], PUBLIC_SAMPLE_JWT)
        jwt_shaped = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*")
        found = {(s["id"], m) for s in self.services for m in jwt_shaped.findall(json.dumps(s.get("params")))}
        self.assertEqual({sid for sid, _ in found}, {"jwt-debugger"}, "another service stores a JWT-shaped value in a param example")
        self.assertEqual({m for _, m in found}, {PUBLIC_SAMPLE_JWT})

    def test_services_json_and_the_urls_slice_carry_what_overrides_json_declares(self):
        by_id = {e["id"]: e for e in self.services}
        url_by_id = {e["id"]: e for e in self.urls}
        mismatches = []
        for slug, patch in self.overrides.items():
            if slug.startswith("$") or slug not in by_id or not isinstance(patch, dict):
                continue
            for key in ("params", "example_path_source"):
                if by_id[slug].get(key) != patch.get(key):
                    mismatches.append(f"{slug}: services.json {key} differs from overrides.json")
                if url_by_id[slug].get(key) != patch.get(key):
                    mismatches.append(f"{slug}: services.urls.json {key} differs from overrides.json")
        self.assertEqual(mismatches[:10], [], "run bin/generate.py --only <ids>")

    def test_the_urls_slice_projects_params_and_source_but_the_public_mirror_does_not_yet(self):
        entry = {"id": "x", "url": "https://x.example", "example_path": "/?q=a", "example_path_source": "derived-from-params",
                 "params": [{"name": "q", "required": True}], "host_port": 1}
        self.assertEqual(generate.PROJECTIONS["services.urls.json"](entry),
                         {"id": "x", "url": "https://x.example", "example_path": "/?q=a",
                          "example_path_source": "derived-from-params", "params": [{"name": "q", "required": True}]})
        self.assertNotIn("params", generate.PROJECTIONS["services.minimal.json"](entry))
        # services-public.json is allowlist-only and widening it needs an operator decision: not made here
        self.assertNotIn("params", generate.PUBLIC_FIELDS)
        self.assertNotIn("example_path_source", generate.PUBLIC_FIELDS)

    def test_an_expand_child_inherits_params_from_its_override(self):
        spec = dict(EXPAND_SPEC)
        params = [{"name": "q", "required": True}]
        by_slug = {"fleet-grafana": {"params": params, "example_path_source": "derived-from-params"}}
        children = {c["id"]: c for c in generate.expand_entry(PARENT_FIXTURE, spec, by_slug, [])}
        self.assertEqual(children["fleet-grafana"]["params"], params)
        self.assertNotIn("params", children["fleet-discovery"])

    def test_the_schema_knows_both_fields(self):
        schema = json.loads((Path(generate.ROOT) / "schema" / "v1.json").read_text())
        self.assertIn("params", schema["properties"])
        self.assertEqual(schema["properties"]["example_path_source"]["enum"], ["derived-from-params"])
        for field in ("params", "example_path_source"):
            self.assertIn(field, generate.PRIVATE_SERVICE_FORBIDDEN_FIELDS)


if __name__ == "__main__":
    unittest.main()
