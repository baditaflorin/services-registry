#!/usr/bin/env python3

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("ci_authority_audit.py")
SPEC = importlib.util.spec_from_file_location("ci_authority_audit", MODULE_PATH)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class AuthorityConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "version": 1,
            "known_ci_hosts": ["ci.0exec.com", "ci.0mcp.com"],
            "owners": {
                "baditaflorin": {
                    "default": "ci.0exec.com",
                    "overrides": {"mcp-site-service": "ci.0mcp.com"},
                }
            },
        }

    def test_owner_default(self):
        self.assertEqual(
            audit.expected_host(self.config, "baditaflorin/go-common"),
            "ci.0exec.com",
        )

    def test_repository_override(self):
        self.assertEqual(
            audit.expected_host(self.config, "baditaflorin/mcp-site-service"),
            "ci.0mcp.com",
        )


class HookEvaluationTests(unittest.TestCase):
    known = ["ci.0exec.com", "ci.0mcp.com"]

    @staticmethod
    def hook(host, active=True, suffix="?access_token=must-not-leak"):
        return {"active": active, "config": {"url": f"https://{host}/api/hook{suffix}"}}

    def test_exact_authority_passes(self):
        result = audit.evaluate_hooks(
            "baditaflorin/mcp-site-service",
            "ci.0mcp.com",
            self.known,
            [self.hook("ci.0mcp.com"), self.hook("ci.0exec.com", False)],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.active_hosts, ("ci.0mcp.com",))
        self.assertNotIn("access_token", str(result.as_dict()))

    def test_duplicate_authorities_fail(self):
        result = audit.evaluate_hooks(
            "baditaflorin/mcp-site-service",
            "ci.0mcp.com",
            self.known,
            [self.hook("ci.0mcp.com"), self.hook("ci.0exec.com")],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "multiple active CI webhooks")

    def test_duplicate_hooks_on_same_authority_fail(self):
        result = audit.evaluate_hooks(
            "baditaflorin/mcp-site-service",
            "ci.0mcp.com",
            self.known,
            [
                self.hook("ci.0mcp.com"),
                self.hook("ci.0mcp.com", suffix="/second"),
            ],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.active_hosts, ("ci.0mcp.com", "ci.0mcp.com"))

    def test_wrong_authority_fails(self):
        result = audit.evaluate_hooks(
            "baditaflorin/mcp-site-service",
            "ci.0mcp.com",
            self.known,
            [self.hook("ci.0exec.com")],
        )
        self.assertFalse(result.ok)
        self.assertIn("expected ci.0mcp.com", result.reason)

    def test_unrelated_webhooks_are_ignored(self):
        result = audit.evaluate_hooks(
            "baditaflorin/mcp-site-service",
            "ci.0mcp.com",
            self.known,
            [self.hook("example.com")],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.active_hosts, ())


if __name__ == "__main__":
    unittest.main()
