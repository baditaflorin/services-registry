#!/usr/bin/env python3

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("propagate_agent_guidance.py")
SPEC = importlib.util.spec_from_file_location("propagate_agent_guidance", MODULE_PATH)
assert SPEC and SPEC.loader
guidance = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guidance
SPEC.loader.exec_module(guidance)


class GuidanceTests(unittest.TestCase):
    def test_docs_only_ci_accepts_either_event_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".woodpecker.yml"
            path.write_text("when:\n  event: [pull_request, push]\n")
            self.assertTrue(guidance.add_docs_only_ci(path))
            self.assertEqual(
                path.read_text(),
                "when:\n  event: [pull_request, push]\n"
                "  path:\n    exclude:\n      - CLAUDE.md\n      - AGENTS.md\n",
            )
            self.assertFalse(guidance.add_docs_only_ci(path))

    def test_graph_block_replaces_unmanaged_section_and_is_idempotent(self):
        content = "## Graph-first workflow\n\nnew guidance"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CLAUDE.md"
            path.write_text(
                "# Service\n\n## Graph-first workflow\n\nold guidance\n\n## Local detail\n\nkeep\n"
            )
            self.assertTrue(guidance.upsert_graph_guidance(path, content))
            updated = path.read_text()
            self.assertIn(guidance.GRAPH_START, updated)
            self.assertIn(content, updated)
            self.assertNotIn("old guidance", updated)
            self.assertIn("## Local detail\n\nkeep", updated)
            self.assertFalse(guidance.upsert_graph_guidance(path, content))

    def test_graph_block_rejects_partial_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AGENTS.md"
            path.write_text(guidance.GRAPH_START + "\n")
            with self.assertRaisesRegex(ValueError, "malformed"):
                guidance.upsert_graph_guidance(path, "## Graph-first workflow\n\nnew")

    def test_without_section_keeps_other_canonical_sections(self):
        original = "# Top\n\n## Graph-first workflow\n\nremove\n\n## Release\n\nkeep\n"
        self.assertEqual(
            guidance.without_section(original, "## Graph-first workflow"),
            "# Top\n\n## Release\n\nkeep\n",
        )

    def test_targets_resolves_repo_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go-fleet-graph").mkdir()
            registry = root / "services.json"
            registry.write_text(
                '[{"id":"fleet-graph","kind":"container",'
                '"repo_url":"https://github.com/baditaflorin/go-fleet-graph"},'
                '{"id":"static","kind":"static","repo_url":"https://github.com/x/static"}]'
            )
            self.assertEqual(
                guidance.targets(registry, root),
                [("fleet-graph", "go-fleet-graph")],
            )

    def test_rollout_state_retries_old_guidance_revision_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                '{"version":1,"completed":{"old-repo":"updated"},'
                '"runs":[{"at":"earlier","results":[]}]}'
            )
            migrated = guidance.load_rollout_state(path)
            self.assertEqual(migrated["version"], 3)
            self.assertEqual(migrated["guidance_revision"], guidance.GUIDANCE_REVISION)
            self.assertEqual(migrated["completed"], {})
            self.assertEqual(len(migrated["runs"]), 1)
            path.write_text(
                '{"version":3,"guidance_revision":2,'
                '"completed":{"current-repo":"updated"},"runs":[]}'
            )
            self.assertEqual(
                guidance.load_rollout_state(path)["completed"],
                {"current-repo": "updated"},
            )


if __name__ == "__main__":
    unittest.main()
