# Fleet Agent Contract

This repository is part of the baditaflorin fleet. Project-specific instructions in this file take precedence over generic conventions; retain them when updating this document.

## Outcome

This contract keeps four things true: work starts from fresh `origin/main`; production matches the registry; every release leaves a receipt chain; no secret or private topology reaches a commit, log or chat. Judge your work against those, not against "pushed" or "tests passed".

## Freshness and safety

- Before reading, building, or changing source, run `git fetch origin --tags` and work from `origin/main` in an isolated worktree.
- Do not place credentials, private topology, or secret-bearing environment files in commits or logs.
- Container services use Woodpecker CI. Do not add GitHub Actions unless explicitly requested.
- Before changing Woodpecker worker placement or controller behavior, read `docs/runbooks/woodpecker-cross-host-capacity.md`. It documents the `ci.0exec.com` shared worker pool and retains production image builds on the canonical fleet builder. Do not move a repository to `ci.0mcp.com` just to reach a physical worker; `site` and `lv3=true` are worker capability constraints within a control plane, not control-plane routing. Keep `ci.0mcp.com` for repositories intentionally owned by the 0mcp platform.
- A pushed commit is not a production deployment. Use the fleet deployment path and retain rollback evidence.
- The shared `/root/workspace/<repo>` tree can lag or sit on another agent's leftover branch, and `fleet-runner build-test` runs against it, so it is not freshness-correct. Confirm any failure on a fresh `origin/main` worktree before triaging.

## Risk tier: what needs care before you touch it

Most changes are low risk: probe-first deploy with rollback on a failed `/selftest` is the safety net, so `fleet-runner deploy <repo>` is enough. These are not covered by it. Not sure which row applies? Take the higher tier.

| Touching | Do this first |
|---|---|
| One service, no shared code | `fleet-runner deploy <repo>` |
| `safehttp`, middleware, auth, the gateway | Deploy one service first and verify `/health`, `/selftest`, `/version` and gateway smoke before widening (there is no `fleet-runner canary` verb). Never a fleet-wide rollout on first touch |
| `go-common` or a `go-fleet-*` primitive with more than one consumer | List the affected set first with `fleet-runner rollout` in its default plan mode (see CLAUDE.md, "Fleet-wide changes") |
| DNS records or secrets | Read `RUNBOOK-UNATTENDED.md`. DNS only through the Hetzner Cloud API; secrets only in `go-fleet-secrets`, never in env, repos or `services.json` |
| `proxy_egress` in `overrides.json` | Read the `proxy_egress` section of `FLEET.md`; the right direction depends on the upstream |
| A SQLite-backed service | Read "SQLite safety" in CLAUDE.md; its three rules are mandatory |

## Evidence and done

- Start from the rawest source: `fleet-runner state`, `whois <fqdn>`, `pool-status`. Do not re-probe hosts by hand, and do not treat a green `/health` or a registry field as proof.
- Tag every fact that gates a decision OBSERVED (read live this session, with source and age) or PREDICTED (inferred from config, docs or an old snapshot). Only observed facts block or approve. Rules mirrored into scripts drift, so test them.
- A dry run or `plan` proves less than a real run. Name the checks that sit after its return and are therefore unproven.
- State acceptance criteria up front, with a number where you can ("0 findings", "0 non-200 reads in N requests"). Verify them with something other than yourself: `fleet-runner converge`, `fleet-runner audit --all`, `fleet-runner state snapshot`, the deploy smoke gates, the target system's own log. If a check does not exist, say so in the receipt rather than claiming done.

## Hard rules

- Never hand-pick a port: take it from `fleet-runner allocate-port`. A collision silently evicts the live service that owns it.
- Release with `fleet-runner bump-version <repo> patch|minor|major --push`. A tag that is not pushed is not a release.
- On the builder, do not run `docker buildx ls` (one hung call holds buildx's global lock and stalls every build) and do not start a service with `./binary &` (it orphans and squats a registered port). Use `go test ./...` or the deploy smoke gate.
- Print environment variable names, never values. Scan output for `://user:pass@`, JWTs and webhook URLs before showing it.
- Do not create billable cloud resources, credentials, logins or access-list entries unprompted. State the block and hand the user one command for that step.

## Before an irreversible or fleet-wide action

Answer these in the change receipt. They are techniques, not role-play. When subagents are available, give a reviewer that is not you the diff, not your conclusion.

- **Blast radius and rollback** (what an SRE checks): `graph-context` and `deps` reviewed, rollback digest captured, one service first.
- **Secrets** (what a secret scanner does): grep your own output and diff the same way.
- **Cost and capacity**: does it add a billable resource or consume host headroom? `fleet-runner capacity` answers the second.
- **Audit trail** (what a forensic accountant does): reconcile your claim against the log of the system you acted on, not against your own summary.

## Where the detail lives

- `FLEET.md`: read first for any fleet-wide task.
- `RUNBOOK-UNATTENDED.md`: DNS, secrets, preflight.
- `SERVICE-TEMPLATE.md`: building a new service.
- `CLAUDE.md` recipes: deploying, rolling out a shared-lib change, closing a capability gap, anti-patterns observed in earlier sessions.

## Graph-first workflow

- Before scoping, searching, or changing a registered service, run `fleet-runner graph-context <canonical-service-id> --json`. It returns a compact, review-only brief: identity, bounded runtime evidence, declared topology, attention signals, and typed next actions.
- Call `fleet-runner deps <canonical-service-id> --depth 1 --top 10 --since 24h --json` only when the brief says `inspect_bounded_dependencies`. Use `fleet-runner ctx <workspace-repo> --callers 3 --callees 3 --graph-since 24h --budget 1500 --workdir /root/workspace` only for the bounded source context then needed. Do not begin with broad `ai-context`.
- `graph-context`, `deps`, and `explain` use the canonical registry ID (for example `fleet-preflight`); `ctx` accepts the repository name (for example `go-fleet-preflight`) and normalizes it.
- Declared `depends_on` topology is design intent from `services-registry`. Observed calls are window-scoped runtime evidence; an absent observed edge is not proof that no caller exists.
- For an already-approved choice between runtime hosts, run `fleet-runner capacity --hosts <registered-labels> --json`. It reads the Fleet Metrics Hub instead of SSHing hosts, rejects absent/down or incomplete telemetry and hosts below the memory floor (10% available by default), flags load per CPU above 0.80 as a warning only, and makes a stable capacity-only recommendation. It does not authorize a migration or override the service's registry cluster/runtime.
- A capacity recommendation is actionable only with the normal deterministic placement gates: fresh origin state, declared/observed dependency review, `fleet-runner allocate-port`, deploy preflight, immutable rollback capture, image/container verification, `/health`, `/selftest`, `/version`, and gateway smoke. For `fleet-runner capacity`, no data (exit 2) and no eligible host (`REJECT`, exit 3) both mean fail closed. `placement-plan` fails closed on missing capacity only for service-pool targets; an ordinary explicit target gets an advisory note.
- `host-placement-policy.json` is the canonical public policy for logical host labels, permitted runtime/placement classes, and capacity labels. It intentionally contains no IPs, SSH details, credentials, or compose paths; those stay in private fleet state. `active` means a target can be considered, `restricted` requires an explicit service allowlist, and `build-only` is never a workload target.
- After deploy, run `fleet-runner explain <canonical-service-id> --since 24h --json` plus the normal health, self-test, version, and gateway checks. Put the graph window, relevant declared/observed edges, and verification result in the change receipt.

## Canonical release receipts

Every meaningful released change must create a traceable chain:

`CHANGELOG.md` entry -> version/tag -> PR or commit SHA -> deployed image/digest -> production verification.

For a container release, use `fleet-runner bump-version <repo> patch|minor|major --push` where possible. The changelog entry must state what changed, why, and the verification result. The release commit and tag must be pushed together. Deployment is complete only after the standard build/test, health, self-test, version, and gateway checks pass.

Use `fleet-runner change-receipts --sort prs --top 50` for a deterministic ranking of the 50 container services with the most PR receipts (default filter `kind=container`; add `--fetch=false` to skip fetching clones). Changelog release headings are the version-change receipts; PR-number receipts and commits provide the code trail.

For static applications, use the same changelog + commit/PR receipt chain, with the published Pages build as the deployment receipt.
