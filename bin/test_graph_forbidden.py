"""Four services must never run the Fleet Graph writer (go_fleet_runner graph_writer_forbidden.go, docs/graph-writer-targets.md):

  fleet-graph     the collector (retired as a writer in runner v0.7.446; the direct self-loop is blocked by the SDK, but it is the sink and
                  learns no edge that its callers' outbound events do not already give)
  apikey-service  the keystore (the collector authenticates every writer batch through it: keystore -> collector -> keystore)
  fleet-secrets   the vault front (writer keys are materialised from it)
  keystore-mesh   the keystore nodes on other hosts

The registry is where a writer is switched on (`env.GRAPH_ENABLED`), so this is where the exclusion is checked in CI. On 2026-10-17 the
collector's own entry still said GRAPH_ENABLED=true; it was inert only because no writer key was mounted.
"""
import json
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FORBIDDEN = ("fleet-graph", "apikey-service", "fleet-secrets", "keystore-mesh")


def normalise(name):
    """The names one service goes by (registry id, go-x, go_x) as one key; the same rule as the runner."""
    name = str(name).strip().lower().replace("_", "-")
    return name[3:] if name.startswith("go-") else name


def load(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return json.load(fh)


def entries(doc):
    """(id, entry) pairs of overrides.json (by-id keys, `$` keys are directives) or services.json (a list or a map of entries)."""
    if isinstance(doc, dict) and "services" in doc:
        doc = doc["services"]
    if isinstance(doc, list):
        for e in doc:
            if isinstance(e, dict) and e.get("id"):
                yield e["id"], e
    elif isinstance(doc, dict):
        for k, e in doc.items():
            if not str(k).startswith("$") and isinstance(e, dict):
                yield (e.get("id") or k), e


def graph_enabled(entry):
    env = entry.get("env")
    return env.get("GRAPH_ENABLED") if isinstance(env, dict) else None


class GraphForbiddenTest(unittest.TestCase):
    def test_the_list_is_pinned(self):
        # a change is a decision: update the runner list, docs/graph-writer-targets.md and this test together
        self.assertEqual(FORBIDDEN, ("fleet-graph", "apikey-service", "fleet-secrets", "keystore-mesh"))
        for name in FORBIDDEN:
            self.assertEqual(normalise(name), name)
        for variant in ("go-fleet-graph", "go_fleet_graph", "GO_APIKEY_SERVICE", " fleet-secrets "):
            self.assertIn(normalise(variant), FORBIDDEN)
        for other in ("apikey-scanner", "fleet-graph-ui", "fleet-memory", "fleet-secrets-backup"):
            self.assertNotIn(normalise(other), FORBIDDEN)

    def test_no_registry_entry_enables_the_writer_for_a_forbidden_service(self):
        bad = []
        for fname in ("overrides.json", "services.json"):
            for sid, entry in entries(load(fname)):
                if normalise(sid) in FORBIDDEN and str(graph_enabled(entry)).lower() == "true":
                    bad.append("%s: %s has env.GRAPH_ENABLED=%r" % (fname, sid, graph_enabled(entry)))
        self.assertEqual(bad, [], "these services must never run the Graph writer: " + "; ".join(bad))

    def test_no_override_rule_turns_the_writer_on_for_a_group(self):
        # A $rules patch could switch the writer on for every service it matches, including a forbidden one. None does today; if one is
        # added it must be reviewed against the four services above (and this test updated to say how the match excludes them).
        hits = []
        for i, rule in enumerate(load("overrides.json").get("$rules") or []):
            patch = rule.get("patch") if isinstance(rule, dict) else None
            env = patch.get("env") if isinstance(patch, dict) else None
            if isinstance(env, dict) and str(env.get("GRAPH_ENABLED", "")).lower() == "true":
                hits.append("rule #%d %r" % (i, rule.get("name", "<unnamed>")))
        self.assertEqual(hits, [], "an override rule enables the Graph writer for a group: " + "; ".join(hits))

    def test_the_fleet_default_stays_off(self):
        with open(os.path.join(ROOT, "host-conventions.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertRegex(text, re.compile(r'^\s+GRAPH_ENABLED:\s*"false"\s*$', re.M),
                         "host-conventions.yaml must keep the fleet default GRAPH_ENABLED: \"false\"")
        self.assertNotRegex(text, re.compile(r'^\s+GRAPH_ENABLED:\s*"?true"?\s*$', re.M))


if __name__ == "__main__":
    unittest.main()
