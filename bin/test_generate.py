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

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate  # type: ignore[import]


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
    "auth":       {"type": "api_key", "query_param": "api_key", "header": "X-API-Key"},
    "auth_help":  "api_key required (header X-API-Key or ?api_key=)",
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


class TestExpandEntry(unittest.TestCase):
    def test_emits_one_entry_per_child_with_slug_derived_urls(self):
        children = generate.expand_entry(PARENT_FIXTURE, EXPAND_SPEC, by_slug={}, rules=[])
        self.assertEqual(len(children), 3)

        d = next(c for c in children if c["id"] == "fleet-discovery")
        self.assertEqual(d["url"],        "https://fleet-discovery.0exec.com")
        self.assertEqual(d["health_url"], "https://fleet-discovery.0exec.com/_gw_health")
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
        by_slug = {"fleet-grafana": {"cert_domain": "fleet-grafana-special.0exec.com"}}
        children = generate.expand_entry(PARENT_FIXTURE, EXPAND_SPEC, by_slug=by_slug, rules=rules)
        grafana = next(c for c in children if c["id"] == "fleet-grafana")
        self.assertEqual(grafana["cert_domain"], "fleet-grafana-special.0exec.com")

    def test_child_without_id_is_a_hard_error(self):
        bad_spec = {"name": "broken", "parent_repo": "x",
                    "children": [{"host_port": 18999}]}  # no id
        with self.assertRaises(SystemExit):
            generate.expand_entry(PARENT_FIXTURE, bad_spec, by_slug={}, rules=[])


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
            "query_param": "api_key",
            "header": "X-API-Key",
            "public_demo_token": "demo-token-must-be-stripped",
        },
        "auth_help": "api_key required (header X-API-Key or ?api_key=)",
        # Internal / disclosure-risk fields — must be dropped:
        "host_port": 18999,
        "container_port": 8999,
        "cert_domain": "wildcard.0crawl.com",
        "proxy_egress": True,
        "ui_cookie_bridge": True,
        "scope": "internal-only",
        "extra_server_names": ["alt.0crawl.com"],
        "vhost": {"proxy_buffering": "off"},
        "depends_on": ["other-svc"],
        "trl_evidence": "long internal commentary referencing ADR-0018",
        "trl": 6,
        "trl_ceiling": 7,
        "trl_ceiling_reason": "needs paid threat intel",
        "trl_assessed_at": "2026-05-16",
        "trl_assessor": "claude-opus-4-7-session-2026-05-16",
    }

    def test_drops_all_internal_fields(self):
        pub = generate.to_public_entry(self.FULL_ENTRY)
        for forbidden in ("host_port", "container_port", "cert_domain",
                          "proxy_egress", "ui_cookie_bridge", "scope",
                          "extra_server_names", "vhost", "depends_on",
                          "trl_evidence"):
            self.assertNotIn(forbidden, pub,
                f"public mirror leaked internal field {forbidden!r}")

    def test_keeps_canonical_public_fields(self):
        pub = generate.to_public_entry(self.FULL_ENTRY)
        for required in ("id", "name", "description", "category",
                         "mesh", "kind", "language", "runtime",
                         "url", "health_url", "repo_url",
                         "auth", "auth_help", "trl"):
            self.assertIn(required, pub,
                f"public mirror dropped canonical field {required!r}")

    def test_strips_auth_public_demo_token(self):
        """public_demo_token in the schema is labeled `intentionally public`
        but we fail-closed: drop it from the mirror until an operator
        explicitly lifts the restriction. Auth `type`/`query_param`/`header`
        survive."""
        pub = generate.to_public_entry(self.FULL_ENTRY)
        self.assertEqual(pub["auth"], {
            "type": "api_key",
            "query_param": "api_key",
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


if __name__ == "__main__":
    unittest.main()
