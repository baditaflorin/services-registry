#!/usr/bin/env python3
"""Tests for bin/ci_resources.py and the ci_resources merge in bin/generate.py. Offline."""
from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_resources as cr  # type: ignore[import]
import generate  # type: ignore[import]

MIB = 1024 * 1024
GIB = 1024 * MIB
NOW = dt.datetime(2026, 10, 29, 12, 0, 0, tzinfo=dt.timezone.utc)
SERVICES = [
    {"id": "alpha", "repo_url": "https://github.com/baditaflorin/go-alpha"},
    {"id": "alpha-ui", "repo_url": "https://github.com/baditaflorin/go-alpha"},
    {"id": "beta", "repo_url": "https://github.com/baditaflorin/beta"},
    {"id": "nourl"},
]


def series(repo, step, pipeline, value, plane="ci_0exec", num="0", host="h1"):
    return {"metric": {"repo": "baditaflorin/" + repo, "step_name": step, "pipeline_number": str(pipeline),
                       "step_number": num, "host": host, "control_plane": plane},
            "value": [1.0, str(value)]}


def doc_for(mem, cpu=(), last_seen=(), services=SERVICES, now=NOW):
    return cr.derive({"memory": list(mem), "cpu": list(cpu), "last_seen": list(last_seen)}, services, now)


class TestMath(unittest.TestCase):
    def test_nearest_rank(self):
        v = list(range(1, 101))
        self.assertEqual(cr.percentile(v, 95), 95)
        self.assertEqual(cr.percentile(v, 100), 100)
        self.assertEqual(cr.percentile([5], 95), 5)
        self.assertEqual(cr.percentile([3, 1, 2], 50), 2)
        self.assertEqual(cr.percentile(list(range(1, 21)), 95), 19)  # ceil(19.0) = 19th
        self.assertEqual(cr.percentile(list(range(1, 22)), 95), 20)  # ceil(19.95) = 20th
        with self.assertRaises(ValueError):
            cr.percentile([], 95)

    def test_class_boundaries(self):
        self.assertEqual(cr.mem_class(0), "xs")
        self.assertEqual(cr.mem_class(256 * MIB), "xs")
        self.assertEqual(cr.mem_class(256 * MIB + 1), "s")
        self.assertEqual(cr.mem_class(512 * MIB), "s")
        self.assertEqual(cr.mem_class(512 * MIB + 1), "m")
        self.assertEqual(cr.mem_class(4294967296), "m")
        self.assertEqual(cr.mem_class(4294967296 + 1), "l")


class TestDerive(unittest.TestCase):
    def test_step_and_repo_numbers(self):
        mem = [series("go-alpha", "test", p, (p + 1) * 10 * MIB) for p in range(20)]
        mem.append(series("go-alpha", "lint", 1, 5 * MIB))
        cpu = [series("go-alpha", "test", p, p + 0.5) for p in range(20)]
        r = doc_for(mem, cpu)["repos"]["go-alpha"]
        t = r["steps"]["test"]
        self.assertEqual(t["runs"], 20)
        self.assertEqual(t["mem_peak_p95_bytes"], 19 * 10 * MIB)
        self.assertEqual(t["mem_peak_max_bytes"], 200 * MIB)
        self.assertEqual(t["cpu_seconds_p95"], 18.5)
        self.assertIsNone(r["steps"]["lint"]["cpu_seconds_p95"])
        self.assertEqual(r["mem_peak_p95_bytes"], 190 * MIB)
        self.assertEqual(r["mem_class"], "xs")
        self.assertEqual(r["runs"], 20)  # distinct pipelines; lint pipeline 1 is one of them
        self.assertEqual(r["confidence"], "ok")
        self.assertEqual(r["ids"], ["alpha", "alpha-ui"])

    def test_class_follows_heaviest_step_p95(self):
        mem = [series("beta", "a", 1, 10 * MIB), series("beta", "b", 1, 300 * MIB)]
        self.assertEqual(doc_for(mem)["repos"]["beta"]["mem_class"], "s")

    def test_confidence_low_under_20_runs(self):
        mem = [series("beta", "t", p, MIB) for p in range(19)]
        self.assertEqual(doc_for(mem)["repos"]["beta"]["confidence"], "low")
        mem.append(series("beta", "t", 99, MIB))
        self.assertEqual(doc_for(mem)["repos"]["beta"]["confidence"], "ok")

    def test_same_pipeline_number_on_two_control_planes_is_two_runs(self):
        mem = [series("beta", "t", 5, MIB, plane="ci_0exec"), series("beta", "t", 5, MIB, plane="ci_0mcp")]
        self.assertEqual(doc_for(mem)["repos"]["beta"]["runs"], 2)

    def test_oom_suspect(self):
        mem = [series("beta", "big", 1, int(4.9 * GIB)), series("beta", "small", 1, 4 * GIB)]
        r = doc_for(mem)["repos"]["beta"]
        self.assertTrue(r["steps"]["big"]["oom_suspect"])
        self.assertFalse(r["steps"]["small"]["oom_suspect"])
        self.assertTrue(r["oom_suspect"])
        self.assertFalse(doc_for([series("beta", "x", 1, int(4.9 * GIB) - 1)])["repos"]["beta"]["oom_suspect"])

    def test_zero_samples_are_ignored(self):
        mem = [series("beta", "t", 1, 0), series("beta", "t", 2, 7 * MIB)]
        self.assertEqual(doc_for(mem)["repos"]["beta"]["steps"]["t"]["runs"], 1)
        self.assertEqual(doc_for([series("beta", "t", 1, 0)])["repos"], {})

    def test_steps_capped_to_12_heaviest_but_repo_numbers_keep_all(self):
        mem = [series("beta", "s%02d" % i, 1, (i + 1) * MIB) for i in range(15)]
        r = doc_for(mem)["repos"]["beta"]
        self.assertEqual(len(r["steps"]), 12)
        self.assertNotIn("s00", r["steps"])
        self.assertIn("s14", r["steps"])
        self.assertEqual(r["mem_peak_p95_bytes"], 15 * MIB)

    def test_mapping_and_unmapped(self):
        d = doc_for([series("beta", "t", 1, MIB), series("mystery", "t", 1, MIB), series("nourl", "t", 1, MIB)])
        self.assertEqual(d["repos"]["beta"]["ids"], ["beta"])
        # the published document never names a repository without a registry id
        self.assertNotIn("mystery", d["repos"])
        self.assertNotIn("nourl", d["repos"])
        self.assertNotIn("unmapped_repos", d)
        self.assertEqual(d["unmapped"], {"count": 2, "runs": 2, "mem_peak_p95_max_bytes": MIB})
        text = cr.render(d)
        self.assertNotIn("mystery", text)
        self.assertNotIn("nourl", text)

    def test_last_seen_from_series_else_now(self):
        ts = dt.datetime(2026, 10, 25, 3, 0, tzinfo=dt.timezone.utc).timestamp()
        ls = [{"metric": {"repo": "baditaflorin/beta"}, "value": [1, str(ts)]}]
        d = doc_for([series("beta", "t", 1, MIB), series("go-alpha", "t", 1, MIB)], last_seen=ls)
        self.assertEqual(d["repos"]["beta"]["last_seen"], "2026-10-25")
        self.assertEqual(d["repos"]["go-alpha"]["last_seen"], "2026-10-29")

    def test_deterministic_and_input_order_independent(self):
        mem = [series("beta", "t", p, p * MIB) for p in range(1, 30)] + [series("go-alpha", "x", 1, MIB)]
        a = cr.render(doc_for(mem))
        b = cr.render(doc_for(list(reversed(mem))))
        self.assertEqual(a, b)
        self.assertTrue(a.endswith("}\n"))
        self.assertIn('\n  "classes"', a)
        self.assertEqual(json.loads(a)["window_days"], 7)

    def test_input_file_shapes(self):
        with tempfile.TemporaryDirectory() as t:
            raw = {"status": "success", "data": {"resultType": "vector", "result": [series("beta", "t", 1, MIB)]}}
            p = Path(t) / "m.json"
            p.write_text(json.dumps(raw))
            self.assertEqual(len(cr.load_input(p)["memory"]), 1)
            p.write_text(json.dumps(raw["data"]["result"]))
            self.assertEqual(len(cr.load_input(p)["memory"]), 1)
            p.write_text(json.dumps({"memory": raw["data"]["result"], "cpu": []}))
            self.assertEqual(len(cr.load_input(p)["memory"]), 1)


class TestCheck(unittest.TestCase):
    def good(self):
        mem = [series("beta", "t", p, 100 * MIB) for p in range(25)] + [series("mystery", "t", 1, MIB)]
        return doc_for(mem)

    def test_valid_document_passes(self):
        self.assertEqual(cr.check(self.good(), SERVICES, NOW), [])

    def test_stale(self):
        late = NOW + dt.timedelta(days=9)
        errs = cr.check(self.good(), SERVICES, late)
        self.assertTrue(any("days old" in e for e in errs), errs)
        self.assertEqual(cr.check(self.good(), SERVICES, late, max_age_days=10), [])

    def test_bad_class(self):
        d = self.good()
        d["repos"]["beta"]["mem_class"] = "l"
        self.assertTrue(any("inconsistent with mem_peak_p95_bytes" in e for e in cr.check(d, SERVICES, NOW)))

    def test_unknown_id(self):
        d = self.good()
        d["repos"]["beta"]["ids"] = ["ghost"]
        self.assertTrue(any("'ghost' is not in services.json" in e for e in cr.check(d, SERVICES, NOW)))

    def test_bad_version_confidence_and_oom(self):
        d = self.good()
        d["version"] = 2
        d["repos"]["beta"]["confidence"] = "low"
        d["repos"]["beta"]["steps"]["t"]["oom_suspect"] = True
        errs = cr.check(d, SERVICES, NOW)
        self.assertTrue(any("version" in e for e in errs))
        self.assertTrue(any("confidence" in e for e in errs))
        self.assertTrue(any("oom_suspect" in e for e in errs))

    def test_a_repo_without_registry_ids_is_refused(self):
        d = self.good()
        d["repos"]["beta"]["ids"] = []
        self.assertTrue(any("no registry ids" in e for e in cr.check(d, SERVICES, NOW)))

    def test_an_unmapped_repository_list_is_refused(self):
        d = self.good()
        d["unmapped_repos"] = ["some-private-repo"]
        self.assertTrue(any("must not exist" in e for e in cr.check(d, SERVICES, NOW)))

    def test_unmapped_aggregate_shape(self):
        d = self.good()
        d["unmapped"] = {"count": "12"}
        self.assertTrue(any("unmapped must be an object" in e for e in cr.check(d, SERVICES, NOW)))

    def test_secret_looking_string(self):
        d = self.good()
        d["repos"]["beta"]["steps"]["tok" + "en=" + "x" * 12] = d["repos"]["beta"]["steps"]["t"]
        self.assertTrue(any("secret" in e for e in cr.check(d, SERVICES, NOW)))

    def test_not_an_object_and_missing_fields(self):
        self.assertTrue(cr.check([], SERVICES, NOW))
        d = self.good()
        del d["repos"]["beta"]["mem_class"]
        self.assertTrue(any("missing mem_class" in e for e in cr.check(d, SERVICES, NOW)))

    def test_checked_in_file_is_valid_shape(self):
        """The committed file must pass everything except freshness."""
        path = Path(__file__).resolve().parent.parent / "ci-resources.json"
        if not path.exists():
            self.skipTest("no ci-resources.json")
        services = json.loads((path.parent / "services.json").read_text())
        errs = cr.check(json.loads(path.read_text()), services, NOW + dt.timedelta(days=3650), max_age_days=1e9)
        self.assertEqual(errs, [])


class TestGenerateMerge(unittest.TestCase):
    def test_merge_adds_ci_resources_only_to_mapped_entries(self):
        mem = [series("beta", "t", p, 300 * MIB) for p in range(3)]
        doc = doc_for(mem)
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "ci-resources.json"
            path.write_text(cr.render(doc))
            entries = [{"id": "beta", "name": "B"}, {"id": "alpha", "name": "A"}]
            generate.apply_ci_resources(entries, path)
        self.assertEqual(entries[0]["ci_resources"], {
            "mem_class": "s", "mem_peak_p95_bytes": 300 * MIB, "mem_peak_max_bytes": 300 * MIB,
            "runs": 3, "confidence": "low", "measured_at": "2026-10-29"})
        self.assertNotIn("ci_resources", entries[1])

    def test_missing_file_is_a_noop(self):
        entries = [{"id": "beta"}]
        before = copy.deepcopy(entries)
        generate.apply_ci_resources(entries, Path("/nonexistent/ci-resources.json"))
        self.assertEqual(entries, before)

    def test_field_stays_out_of_public_projection(self):
        self.assertNotIn("ci_resources", generate.PUBLIC_FIELDS)
        pub = generate.to_public_entry({"id": "beta", "name": "B", "category": "x", "kind": "container",
                                        "ci_resources": {"mem_class": "s"}})
        self.assertNotIn("ci_resources", pub)

    def test_schema_declares_the_field_with_the_merged_keys(self):
        schema = json.loads((Path(__file__).resolve().parent.parent / "schema" / "v1.json").read_text())
        props = schema["properties"]["ci_resources"]["properties"]
        self.assertEqual(set(props), set(generate.CI_RESOURCES_FIELDS) | {"measured_at"})


if __name__ == "__main__":
    unittest.main()
