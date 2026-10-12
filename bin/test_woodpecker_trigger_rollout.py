#!/usr/bin/env python3
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("woodpecker_trigger_rollout.py")
SPEC = importlib.util.spec_from_file_location("woodpecker_trigger_rollout", MODULE_PATH)
assert SPEC and SPEC.loader
rollout = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rollout
SPEC.loader.exec_module(rollout)

HEADER = '# Woodpecker CI (services-registry ADR-0035). Mirrors the same gate\n# "green," not two.\n'
BASIC = HEADER + """steps:
  test:
    image: golang:1.25
    commands:
      - go build ./...
      - go test ./...
    when:
      event: [push, pull_request]
"""
DOCS = BASIC + """      path:
        exclude:
          - CLAUDE.md
          - AGENTS.md
"""
MANUAL = BASIC.replace("pull_request]", "pull_request, manual]")
PULL = BASIC.replace("image: golang:1.25\n", "image: golang:1.25\n    pull: true\n")
NODE = """steps:
  test:
    image: node:22-bookworm
    commands:
      - git clone --depth 1 https://github.com/baditaflorin/mesh-common.git ../mesh-common
      - npm ci
      - npm run smoke
    when:
      event: [push, pull_request]
"""


class ClassifyTests(unittest.TestCase):
    def kind(self, text, **kw):
        return rollout.classify(text, **kw)

    def test_standard_shapes(self):
        for text in (BASIC, DOCS, MANUAL, PULL, NODE):
            self.assertEqual(self.kind(text)[0], "STANDARD", text)

    def test_special_shapes(self):
        cases = {
            "tag event": BASIC.replace("pull_request]", "pull_request, tag]"),
            "pr only": BASIC.replace("push, pull_request", "pull_request"),
            "no when": BASIC.split("    when:")[0],
            "list when": BASIC.replace("    when:\n      event: [push, pull_request]\n", "    when:\n      - event: [push, pull_request]\n"),
            "branch cond": BASIC + "      branch: main\n",
            "other path": BASIC + "      path:\n        exclude:\n          - docs/**\n",
            "second step": BASIC + "  lint:\n    image: golang:1.25\n    commands:\n      - go vet ./...\n",
            "top when": "when:\n  - event: pull_request\n" + BASIC,
            "env": BASIC.replace("    commands:", "    environment:\n      A: b\n    commands:"),
            "docker": BASIC.replace("go test ./...", "docker build ."),
            "image": BASIC.replace("golang:1.25", "alpine:3"),
            "multiline cmd": BASIC.replace("      - go test ./...", "      - |\n        go test ./..."),
            "release": BASIC.replace("go test ./...", "make release"),
            "tabs": BASIC.replace("    image", "\timage"),
        }
        for name, text in cases.items():
            self.assertEqual(self.kind(text)[0], "SPECIAL", name)

    def test_repo_level_special(self):
        self.assertEqual(self.kind(BASIC, has_dir=True)[0], "SPECIAL")
        self.assertEqual(self.kind(BASIC, has_yaml=True)[0], "SPECIAL")
        self.assertEqual(self.kind(BASIC, archived=True)[0], "SPECIAL")


class TransformTests(unittest.TestCase):
    def test_basic_output(self):
        self.assertEqual(
            rollout.transform(BASIC, "main"),
            HEADER
            + """# Triggers: one pipeline per PR update and one per push to main;
# a push to a PR branch is covered by its pull_request run, so it no longer
# starts a second identical pipeline (services-registry templates/woodpecker-go-ci.yml).
when:
  - event: pull_request
  - event: push
    branch: main
  - event: manual

steps:
  test:
    image: golang:1.25
    commands:
      - go build ./...
      - go test ./...
""",
        )

    def test_docs_only_skip_is_kept_on_pr_and_push(self):
        out = rollout.transform(DOCS, "main")
        self.assertEqual(out.count("        - CLAUDE.md"), 2)
        self.assertIn("  - event: push\n    branch: main\n    path:\n      exclude:", out)
        self.assertNotIn("    when:", out)

    def test_default_branch_is_used(self):
        self.assertIn("    branch: master\n", rollout.transform(BASIC, "master"))
        with self.assertRaises(rollout.Special):
            rollout.transform(BASIC, "ma in; rm")

    def test_manual_is_added_even_when_absent_and_kept_when_present(self):
        for text in (BASIC, MANUAL):
            self.assertIn("  - event: manual\n", rollout.transform(text, "main"))

    def test_idempotent_and_step_unchanged(self):
        for text in (BASIC, DOCS, MANUAL, PULL, NODE):
            out = rollout.transform(text, "main")
            self.assertEqual(rollout.classify(out)[0], "SPECIAL")  # already migrated: top-level when
            self.assertIn("pull: true" if "pull: true" in text else "image:", out)
            # the commands survive byte for byte
            for line in text.splitlines():
                if line.startswith("      - "):
                    self.assertIn(line + "\n", out)

    def test_not_standard_raises(self):
        with self.assertRaises(rollout.Special):
            rollout.transform(BASIC.replace("push, ", ""), "main")


if __name__ == "__main__":
    unittest.main()
