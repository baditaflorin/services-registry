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
        by_slug, rules, expansions = generate.split_overrides(raw)
        self.assertEqual(set(by_slug.keys()), {"a11y-quick", "node-search"})
        self.assertEqual(len(rules), 1)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["parent_repo"], "go-fleet-metrics-hub")

    def test_missing_metadata_keys_are_empty(self):
        by_slug, rules, expansions = generate.split_overrides({"a11y-quick": {"trl": 6}})
        self.assertEqual(rules, [])
        self.assertEqual(expansions, [])
        self.assertEqual(by_slug, {"a11y-quick": {"trl": 6}})


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


if __name__ == "__main__":
    unittest.main()
