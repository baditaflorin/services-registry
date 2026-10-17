import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "fleet-version-pin-sync.py"
_spec = importlib.util.spec_from_file_location("fleet_version_pin_sync", SCRIPT)
vps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vps)


class TestParse(unittest.TestCase):
    def test_version_bodies(self):
        self.assertEqual(vps.parse_version_body('{"version":"1.2.3"}'), "1.2.3")
        self.assertEqual(vps.parse_version_body('{"data":{"version":"v0.7.5"}}'), "0.7.5")
        self.assertIsNone(vps.parse_version_body("<!doctype html>"))
        self.assertIsNone(vps.parse_version_body('{"version":""}'))
        self.assertIsNone(vps.parse_version_body('["1.2.3"]'))

    def test_semver_and_repo(self):
        self.assertLess(vps.semver("1.9.16"), vps.semver("1.9.18"))
        self.assertLess(vps.semver("0.4.7"), vps.semver("0.7.5"))
        self.assertIsNone(vps.semver("latest"))
        self.assertEqual(vps.repo_name("https://github.com/baditaflorin/go_apikey_service.git"), "go_apikey_service")
        self.assertIsNone(vps.repo_name("https://github.com/plausible/community-edition"))
        self.assertIsNone(vps.repo_name("https://github.com/baditaflorin/go_x/blob/main/mesh.go"))

    def test_candidates_skip_external_sunset_and_foreign(self):
        rows = vps.candidates([
            {"id": "a", "kind": "container", "runtime": "compose", "repo_url": "https://github.com/baditaflorin/go_a"},
            {"id": "ext", "kind": "container", "runtime": "external", "repo_url": "https://github.com/baditaflorin/go_a"},
            {"id": "old", "kind": "container", "runtime": "compose", "lifecycle": {"status": "sunset"},
             "repo_url": "https://github.com/baditaflorin/go_old"},
            {"id": "pages", "kind": "static", "repo_url": "https://github.com/baditaflorin/site"},
            {"id": "up", "kind": "container", "runtime": "compose", "repo_url": "https://github.com/other/x"},
        ])
        self.assertEqual([r["id"] for r in rows], ["a"])


class TestDecide(unittest.TestCase):
    """The rule: change a pin only to a version that is both merged (repository main) and running, never lower it,
    and never raise an existing pin to a version ghcr.io lacks."""

    def d(self, **row):
        return vps.decide(row)[0]

    def test_table(self):
        self.assertEqual(self.d(pin="0.4.7", main="0.7.5", live="0.7.5", image=True), "raise")
        self.assertEqual(self.d(pin="0.4.7", main="0.7.5", live="0.7.5", image=False), "skip")  # would turn image red
        self.assertEqual(self.d(pin="0.4.7", main="0.7.5", live="0.7.5", image=None), "skip")
        self.assertEqual(self.d(pin=None, main="0.1.1", live="0.1.1", image=False), "add")      # missing is worse
        self.assertEqual(self.d(pin="0.7.5", main="0.7.5", live="0.7.5"), "ok")
        self.assertEqual(self.d(pin="1.1.5", main="1.1.6", live="1.2.5", image=True), "skip")   # composite runner
        self.assertEqual(self.d(pin="0.1.12", main="0.1.14", live="0.1.12", image=True), "ok")  # undeployed release
        self.assertEqual(self.d(pin="0.1.0", main="0.1.14", live="0.1.12", image=True), "skip")  # main ahead of live
        self.assertEqual(self.d(pin="2.7.1", main="2.5.7", live="2.5.7", image=True), "skip")   # never lowered
        self.assertEqual(self.d(pin="0.1.0", main=None, live="0.1.0"), "skip")
        self.assertEqual(self.d(pin="0.1.0", main="0.2.0", live=None), "skip")
        self.assertEqual(self.d(pin="latest", main="0.2.0", live="0.2.0", image=True), "skip")


OVERRIDES = """{
  "$rules": [],
  "a": {
    "version": "1.0.0",
    "trl": 4
  },
  "b": {
    "trl": 2
  },
  "inline": {
    "version": "0.1.0",
    "spiffe_workload": true  },
  "c": {"version": "3.0.0"}
}
"""


class TestSetPins(unittest.TestCase):
    def test_replace_insert_and_inline_close(self):
        out = vps.set_pins(OVERRIDES, {"a": "1.0.1", "b": "2.0.0", "inline": "0.2.0"})
        doc = json.loads(out)
        self.assertEqual((doc["a"]["version"], doc["b"]["version"], doc["inline"]["version"]), ("1.0.1", "2.0.0", "0.2.0"))
        self.assertEqual(doc["c"], {"version": "3.0.0"})
        self.assertIn('"spiffe_workload": true  },', out)  # the inline close is kept byte for byte
        self.assertEqual(len(out.splitlines()), len(OVERRIDES.splitlines()) + 1)  # only b gained a line

    def test_refuses_unknown_and_unexpected_layout(self):
        with self.assertRaisesRegex(ValueError, "expected one top-level block"):
            vps.set_pins(OVERRIDES, {"missing": "1.0.0"})
        with self.assertRaisesRegex(ValueError, "layout"):
            vps.set_pins('{\n  "c": {"trl": 1}\n}\n', {"c": "1.0.0"})


class TestVersionOnlyChange(unittest.TestCase):
    def test_detects_anything_but_target_versions(self):
        old = [{"id": "a", "version": "1"}, {"id": "b", "env": {"X": "1"}}]
        self.assertEqual(vps.version_only_change(old, [{"id": "a", "version": "2"}, {"id": "b", "env": {"X": "1"}}],
                                                 {"a": "2"}), [])
        self.assertTrue(vps.version_only_change(old, [{"id": "a", "version": "2"}, {"id": "b"}], {"a": "2"}))
        self.assertTrue(vps.version_only_change(old, [{"id": "a", "version": "2", "trl": 3}, old[1]], {"a": "2"}))
        self.assertTrue(vps.version_only_change(old, old, {"a": "2"}))  # a target that did not change


class TestChangelog(unittest.TestCase):
    def test_today_heading_reused_or_created(self):
        self.assertEqual(vps.changelog_with("## 2026-10-17\n\n- x\n", "- y\n", "2026-10-17"), "## 2026-10-17\n\n- y\n- x\n")
        self.assertEqual(vps.changelog_with("## 2026-10-16\n\n- x\n", "- y\n", "2026-10-17"),
                         "## 2026-10-17\n\n- y\n\n## 2026-10-16\n\n- x\n")


if __name__ == "__main__":
    unittest.main()
