from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent


class WoodpeckerAgentTemplateTests(unittest.TestCase):
    def test_grpc_secret_alias_uses_the_agent_secret_source(self):
        compose = (ROOT / "templates/woodpecker-cross-host-agent.compose.yml").read_text()
        secret_example = (ROOT / "templates/woodpecker-agent-secret.env.example").read_text()
        runbook = (ROOT / "docs/runbooks/woodpecker-cross-host-capacity.md").read_text()

        self.assertIn("WOODPECKER_GRPC_SECRET: ${WOODPECKER_AGENT_SECRET:", compose)
        self.assertIn("- ./.agent-secret.env", compose)
        self.assertIn("WOODPECKER_AGENT_SECRET=replace-with-control-plane-agent-secret", secret_example)
        self.assertNotIn("WOODPECKER_GRPC_SECRET=", secret_example)
        self.assertIn("--env-file .env --env-file .agent-secret.env config --quiet", runbook)
        self.assertIn("--env-file .env --env-file .agent-secret.env up -d", runbook)


if __name__ == "__main__":
    unittest.main()
