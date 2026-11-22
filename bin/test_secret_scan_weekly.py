import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "secret-scan.deploy" / "secret-scan-weekly.sh"


def run(state, findings, prom):
    """Run the weekly wrapper with a stub scanner that writes `findings` (list of (repo, rule, file, line))."""
    stub = Path(state).parent / "stub.sh"
    rows = "".join("%s\t%s\t%s\t%s\tfp\n" % f for f in findings)
    stub.write_text("#!/bin/sh\nmkdir -p \"$1/json\"\ncat > \"$1/summary.tsv\" <<'EOF'\n%sEOF\n" % rows)
    stub.chmod(0o755)
    env = dict(os.environ, SECRET_SCAN_STATE=str(state), SECRET_SCAN_BIN=str(stub), SECRET_SCAN_PROM=str(prom))
    return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


class TestWeekly(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.state = Path(self.d.name) / "state"
        self.prom = Path(self.d.name) / "out.prom"

    def tearDown(self):
        self.d.cleanup()

    def metric(self, name):
        for line in self.prom.read_text().splitlines():
            if line.startswith(name + " "):
                return int(line.split()[1])

    def test_first_run_becomes_the_baseline_and_reports_nothing_new(self):
        r = run(self.state, [("a", "private-key", "k.pem", 1)], self.prom)
        self.assertIn("accepted baseline", r.stdout)
        self.assertEqual((self.metric("fleet_secret_scan_findings"), self.metric("fleet_secret_scan_new_findings")), (1, 0))

    def test_a_new_file_or_rule_is_new_but_a_moved_line_is_not(self):
        run(self.state, [("a", "private-key", "k.pem", 1)], self.prom)
        r = run(self.state, [("a", "private-key", "k.pem", 9), ("a", "private-key", "other.pem", 1), ("b", "jwt", "x.go", 3)], self.prom)
        self.assertEqual(self.metric("fleet_secret_scan_new_findings"), 2)
        self.assertEqual(self.metric("fleet_secret_scan_run_success"), 1)
        self.assertIn("a\tprivate-key\tother.pem", r.stdout)
        self.assertIn("b\tjwt\tx.go", r.stdout)
        self.assertNotIn("k.pem", r.stdout.split("NEW")[1])

    def test_baselined_findings_stay_quiet_and_pause_stops_the_run(self):
        run(self.state, [("a", "jwt", "f.go", 1)], self.prom)
        r = run(self.state, [("a", "jwt", "f.go", 1)], self.prom)
        self.assertNotIn("NEW", r.stdout)
        (self.state / "PAUSE").write_text("")
        r = run(self.state, [("z", "jwt", "z.go", 1)], self.prom)
        self.assertIn("paused", r.stdout)
        self.assertEqual(self.metric("fleet_secret_scan_new_findings"), 0)


if __name__ == "__main__":
    unittest.main()
