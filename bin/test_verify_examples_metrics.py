import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("vem", Path(__file__).resolve().parent / "verify-examples-metrics.py")
vem = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vem)


def doc(**states):
    return {k: {"state": v} for k, v in states.items()}


class TestMetrics(unittest.TestCase):
    def test_only_ok_to_broken_is_a_regression(self):
        old = doc(a="ok", b="ok", c="ok", d="gated", e="error", f="ok")
        new = doc(a="error", b="no-data", c="unreachable", d="error", e="error", f="ok", g="error")
        self.assertEqual(vem.regressed(old, new), ["a", "c"])        # b: no-data is not broken; d/e were not ok; g is new

    def test_render_has_one_series_per_service_and_the_totals(self):
        text, reg = vem.render(doc(a="ok", b="error", c='we"ird'), doc(a="ok", b="ok"), now=1000)
        self.assertIn('fleet_example_state{service="a",state="ok"} 1', text)
        self.assertIn('fleet_example_state{service="c",state="we\\"ird"} 1', text)
        self.assertIn('fleet_examples_services{state="ok"} 1', text)
        self.assertIn("fleet_examples_regressed 1", text)
        self.assertIn("fleet_examples_last_run_timestamp_seconds 1000", text)
        self.assertIn("fleet_examples_run_success 1", text)
        self.assertEqual(reg, ["b"])

    def test_main_writes_atomically_and_prints_regressions(self):
        with tempfile.TemporaryDirectory() as d:
            n, o, p = (os.path.join(d, x) for x in ("new.json", "old.json", "out.prom"))
            json.dump({"services": doc(a="error")}, open(n, "w"))
            json.dump({"services": doc(a="ok")}, open(o, "w"))
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                vem.main(["--new", n, "--old", o, "--out", p])
            self.assertEqual(buf.getvalue().split(), ["a"])
            self.assertFalse(os.path.exists(p + ".tmp"))
            self.assertIn("fleet_examples_regressed 1", open(p).read())

    def test_no_previous_file_means_no_regressions_and_failed_writes_only_failure_metrics(self):
        with tempfile.TemporaryDirectory() as d:
            n, p = os.path.join(d, "new.json"), os.path.join(d, "out.prom")
            json.dump({"services": doc(a="error")}, open(n, "w"))
            vem.main(["--new", n, "--old", os.path.join(d, "missing.json"), "--out", p])
            self.assertIn("fleet_examples_regressed 0", open(p).read())
            vem.main(["--new", n, "--out", p, "--failed"])
            t = open(p).read()
            self.assertIn("fleet_examples_run_success 0", t)
            self.assertNotIn("fleet_example_state", t)


if __name__ == "__main__":
    unittest.main()
