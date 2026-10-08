import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("fleet-safe-apply")


class FleetSafeApplyPreserveDockerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-safe-apply-test-")
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.ssh_log = self.root / "ssh.log"
        ssh = self.bin_dir / "ssh"
        ssh.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$TEST_SSH_LOG\"\n"
            "case \"$*\" in *'tee /etc/nftables.conf.new'*) cat >/dev/null;; esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        self.candidate = self.root / "candidate.nft"
        self.candidate.write_text(
            "#!/usr/sbin/nft -f\n\n"
            "destroy table inet filter\n\n"
            "table inet filter {\n"
            "  chain input {\n"
            "    type filter hook input priority 0;\n"
            "    policy drop;\n"
            "    ip saddr 100.103.203.78 tcp dport { 18002 } accept\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_apply(self, preserve=True):
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}:{env['PATH']}"
        env["TEST_SSH_LOG"] = str(self.ssh_log)
        if preserve:
            env["FLEET_SAFE_APPLY_PRESERVE_DOCKER"] = "1"
        else:
            env.pop("FLEET_SAFE_APPLY_PRESERVE_DOCKER", None)
        return subprocess.run(
            [str(SCRIPT), "nft-apply", "test-host", str(self.candidate)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_preserve_mode_rejects_nonisolated_candidates_before_ssh(self):
        unsafe_candidates = (
            "flush ruleset\n",
            "destroy table ip nat\n",
            "table ip nat {\n}\n",
            "add rule ip filter INPUT accept\n",
            "include \"/etc/nftables.d/*.nft\"\n",
        )
        for unsafe in unsafe_candidates:
            with self.subTest(unsafe=unsafe):
                self.ssh_log.unlink(missing_ok=True)
                self.candidate.write_text(
                    "#!/usr/sbin/nft -f\n\n"
                    "destroy table inet filter\n\n"
                    "table inet filter {\n  chain input { }\n}\n"
                    + unsafe,
                    encoding="utf-8",
                )
                result = self.run_apply()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.ssh_log.exists())

    def test_preserve_mode_keeps_docker_running_and_runs_health_sweep(self):
        result = self.run_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.ssh_log.read_text(encoding="utf-8")
        self.assertNotIn("systemctl restart docker", calls)
        self.assertGreaterEqual(calls.count("nft list chain ip nat DOCKER"), 2)
        self.assertIn("sweeping every container", result.stderr)

    def test_default_mode_retains_docker_restart_behavior(self):
        result = self.run_apply(preserve=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("systemctl restart docker", self.ssh_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
