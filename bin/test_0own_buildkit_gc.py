import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/buildkit-gc/prune.sh"


class BuildkitGcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.psi = root / "psi"
        self.log = root / "calls"
        docker = root / "docker"
        docker.write_text("""#!/bin/sh
echo "$*" >> "$CALLS"
case "$1" in
  ps) printf '%s' "${STEPS:-}" ;;
  inspect) echo true ;;
  exec) exit "${GC_EXIT:-0}" ;;
esac
""")
        docker.chmod(0o755)
        self.psi.write_text("some avg10=0.00\nfull avg10=1.50 avg60=1.00\n")
        self.env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                        BUILDKIT_GC_PSI_FILE=str(self.psi), CALLS=str(self.log))

    def run_gc(self, **env):
        return subprocess.run(["bash", str(SCRIPT)], env={**self.env, **env},
                              text=True, capture_output=True)

    def test_runs_buildkit_gc_with_both_bounds(self):
        result = self.run_gc()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("buildctl prune --keep-duration 168h --keep-storage 80000", self.log.read_text())

    def test_skips_pressure_and_running_steps(self):
        self.psi.write_text("full avg10=5.00 avg60=1.00\n")
        self.assertEqual(self.run_gc().returncode, 0)
        self.assertFalse(self.log.exists())
        self.psi.write_text("full avg10=0.00 avg60=1.00\n")
        self.assertEqual(self.run_gc(STEPS="wp_123").returncode, 0)
        self.assertNotIn("buildctl", self.log.read_text())

    def test_fails_closed_on_bad_telemetry_and_surfaces_gc_failure(self):
        self.psi.write_text("full avg10=nan\n")
        self.assertNotEqual(self.run_gc().returncode, 0)
        self.psi.write_text("full avg10=0.00\n")
        self.assertEqual(self.run_gc(GC_EXIT="42").returncode, 42)


if __name__ == "__main__":
    unittest.main()
