# Fleet Agent Contract

This repository is part of the baditaflorin fleet. Project-specific instructions in this file take precedence over generic conventions; retain them when updating this document.

## Freshness and safety

- Before reading, building, or changing source, run `git fetch origin --tags` and work from `origin/main` in an isolated worktree.
- Do not place credentials, private topology, or secret-bearing environment files in commits or logs.
- Container services use Woodpecker CI. Do not add GitHub Actions unless explicitly requested.
- A pushed commit is not a production deployment. Use the fleet deployment path and retain rollback evidence.

## Canonical release receipts

Every meaningful released change must create a traceable chain:

`CHANGELOG.md` entry -> version/tag -> PR or commit SHA -> deployed image/digest -> production verification.

For a container release, use `fleet-runner bump-version <repo> patch|minor|major --push` where possible. The changelog entry must state what changed, why, and the verification result. The release commit and tag must be pushed together. Deployment is complete only after the standard build/test, health, self-test, version, and gateway checks pass.

Use `fleet-runner change-receipts --sort prs --top 50` for a deterministic fleet-wide inventory. Changelog release headings are the version-change receipts; PR-number receipts and commits provide the code trail.

For static applications, use the same changelog + commit/PR receipt chain, with the published Pages build as the deployment receipt.
