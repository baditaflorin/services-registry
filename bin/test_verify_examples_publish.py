import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("vep", Path(__file__).resolve().parent / "verify-examples-publish.py")
vep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vep)


def doc(**s):
    return {"checked_at": "x", "services": {k: ({"state": v} if isinstance(v, str) else v) for k, v in s.items()}}


class TestPublish(unittest.TestCase):
    def test_timestamps_and_ms_alone_are_no_change(self):
        a = {"checked_at": "1", "services": {"a": {"state": "ok", "ms": 5, "http": 200}}}
        b = {"checked_at": "2", "services": {"a": {"state": "ok", "ms": 900, "http": 200}}}
        self.assertEqual(vep.diff(a, b), [])

    def test_state_replica_new_and_removed_services_are_changes(self):
        old = doc(a="ok", b="ok", c="ok", d={"state": "ok", "replicas": [{"host": "h", "state": "ok"}]})
        new = doc(a="error", b="ok", e="ok", d={"state": "ok", "replicas": [{"host": "h", "state": "ok"}, {"host": "i", "state": "ok"}]})
        self.assertEqual(vep.diff(old, new), [("a", "ok", "error"), ("c", "ok", None), ("d", "ok", "ok"), ("e", None, "ok")])

    def test_a_mass_failure_is_not_healthy_but_a_few_failures_are(self):
        old = doc(**{"s%d" % i: "ok" for i in range(10)})
        few = doc(**{"s%d" % i: ("error" if i < 2 else "ok") for i in range(10)})
        mass = doc(**{"s%d" % i: ("unreachable" if i < 5 else "ok") for i in range(10)})
        self.assertTrue(vep.healthy(old, few))          # 8 of 10 still fine: exactly the threshold
        self.assertFalse(vep.healthy(old, mass))
        self.assertTrue(vep.healthy(doc(), mass))        # nothing was ok before: nothing to protect
        self.assertTrue(vep.healthy(old, doc(**{"s%d" % i: ("gated" if i < 5 else "no-data") for i in range(10)})))

    def test_tally(self):
        self.assertEqual(vep.tally(doc(a="ok", b="ok", c="error")), {"error": 1, "ok": 2})


if __name__ == "__main__":
    unittest.main()
