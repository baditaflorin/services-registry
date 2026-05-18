# ADR-0028 — Image tagging + version-bump policy

* **Status**: Accepted (implemented 2026-05-18, go_fleet_runner@8a029a7)
* **Date**: 2026-05-18
* **Authors**: fleet-agent, baditaflorin
* **Tags**: fleet-runner, deploy, ci, gitops

## Context

`fleet-runner deploy <slug>` currently treats "image tag matches the
service.yaml `version:`" as "no drift". The 2026-05-17 cleanup pass
proved that assumption wrong: a single semver string (e.g. `0.3.1`)
can have **multiple distinct binaries** behind it — anything from one
or more bug-fix commits to a full apikey-migration refactor that
changes auth semantics — and the deploy verb has no way to tell.

The session ran into this four times:

* `go-fleet-dns-sync@c905197` (apikey migration) shares `version: 0.3.1`
  with `c067e61` (a prior fix). The deployed image was tagged `:0.3.1`
  but built from the pre-migration code. `fleet-runner deploy` said
  "no drift" and refused to rebuild.
* `go-fleet-preflight`, `selftest-aggregator`, `priority-queue` —
  same pattern; every one needed `--force-build` to roll forward.

Workarounds tried this pass:

1. `fleet-runner deploy <slug> --force-build` — works, but opt-in
   per call. Operators forget; AI agents forget.
2. Manually bumping `version:` in `service.yaml` before every
   functional commit — operationally noisy, brittle, doesn't survive
   rebases or rapid iteration.
3. Reading `deploy-meta` JSON post-rollout to verify `sha=<commit>`
   matches HEAD — works for forensics, not for prevention.

None solve the underlying ambiguity: the deployed artifact's identity
is its content (git sha + build inputs), but the registry references
it by a tag that's allowed to be reused. Tag-reuse is a feature of OCI
registries that GitOps best practice tells us not to lean on.

## Implementation

Landed in `go_fleet_runner@8a029a7` on 2026-05-18. Three files touched
(~145 net additions):

* `deploy_sha_pin.go` (new) — helpers: `worktreeShortSha`,
  `repoOriginShortSha`, `currentComposePinTag`, `isShaPin`.
* `deploy_helpers.go` — `rebuildAndRoll` and `bootstrapFirstTime` both
  compute the worktree short sha after `git worktree add origin/main`,
  push it as a third buildx tag, and rewrite the dockerhost compose
  pin to `:<short-sha>` instead of `:latest`. The post-pull digest
  probe inspects `:<short-sha>` (the canonical pin) instead of
  `:latest`.
* `deploy.go` — drift detection pre-empts the version-compare with a
  sha-compare. Legacy pins (`:latest`, `:<semver>`, `:rollback`) fail
  `isShaPin()` and force a rebuild that migrates to ADR-0028 layout.

The phased rollout described under "Migration path" turned out to be
unnecessary in practice: the same code path that handles fresh-sha
drift also handles legacy-pin migration (it just prints a slightly
different log line). New services land on the sha pin from
`bootstrapFirstTime`; existing services migrate the first time
`fleet-runner deploy <slug>` runs against them. No fleet-wide backfill
or `--pin-mode={latest|sha}` flag was needed.

Verified end-to-end on `go-fleet-dns-sync`:

```
$ fleet-runner deploy go-fleet-dns-sync --dry-run
  ! container legacy pin "latest" (not a sha) — ADR-0028 migration → rebuild + roll
  + container would rebuild go-fleet-dns-sync @ b6c617f + re-pin compose to :b6c617f (dry-run)

$ fleet-runner deploy go-fleet-dns-sync
    > docker buildx build + push (tags: b6c617f, 0.3.1, latest)
    > re-pinned compose image to ghcr.io/baditaflorin/go-fleet-dns-sync:b6c617f
  + container rolled forward
  ✓ deploy complete

$ fleet-runner deploy go-fleet-dns-sync                    # idempotent re-run
  = container running go-fleet-dns-sync-app-1 @ 0.3.1 sha=b6c617f (no drift)
  ✓ all checkpoints already correct (idempotent re-run)
```

Backfill strategy: organic. Every existing service migrates on its
next `fleet-runner deploy <slug>` invocation. Operators wanting to
flip the entire fleet at once can run

```
for slug in $(fleet-runner select); do fleet-runner deploy "$slug"; done
```

which rebuilds + re-pins every service (~3 minutes per repo at
present cluster sizing). No new sweep verb was added — `select` plus
`deploy` covers it.

## Decision

**Image tagging: every build pushes three tags**

For repo `<repo>` at git sha `<short_sha>` with `service.yaml` version
`<version>`, `fleet-runner deploy` will push:

```
ghcr.io/baditaflorin/<repo>:<short_sha>     ← the canonical pin (immutable)
ghcr.io/baditaflorin/<repo>:<version>       ← discoverability (human-readable)
ghcr.io/baditaflorin/<repo>:latest          ← discoverability (default-pull convenience)
```

**Compose pins the sha tag, never `:latest` or `:<version>`**

The dockerhost-side `docker-compose.yml` MUST be rewritten by
`fleet-runner deploy` to reference `:<short_sha>` immediately
after a successful build+push:

```yaml
services:
  app:
    image: ghcr.io/baditaflorin/<repo>:c905197
```

Source-side compose in the repo stays at `:latest` (for ergonomic
local dev) — the dockerhost is the source of truth for what's deployed.

**Drift detection becomes definitional**

`fleet-runner deploy` (no `--force-build`) detects drift by comparing:

```
<short_sha> in /opt/services/<slug>/docker-compose.yml image:
  vs.
<short_sha> at the repo's origin/main HEAD
```

If they differ → rebuild + roll. If they match → idempotent re-run, no
build. **There is no longer a case where "no drift" is wrong**, because
the comparison is content-addressed.

**Semver bumps become a pure human-readable signal**

`fleet-runner bump-version <repo> patch|minor|major` continues to
bump `service.yaml` `version:` + the `Version` const + push a git
tag. But the bump no longer affects what gets deployed — that's
controlled by the sha pin. Semver becomes documentation: it tells a
human "this commit changed the contract" (major), "added a feature"
(minor), or "fixed a bug" (patch), with no enforcement loop tied to it.

### Concrete diff to fleet-runner

In `deploy_helpers.go::rebuildAndRoll` (line ~309):

```go
// Before
imgVer := "ghcr.io/baditaflorin/" + repo + ":" + version
imgLatest := "ghcr.io/baditaflorin/" + repo + ":latest"
run(wt, "docker", "buildx", "build",
    "--platform", "linux/amd64", "--provenance=false",
    "-t", imgVer, "-t", imgLatest, "--push", ".",
)

// After
shortSha, _ := run(wt, "git", "rev-parse", "--short", "HEAD")
shortSha = strings.TrimSpace(string(shortSha))
imgSha := "ghcr.io/baditaflorin/" + repo + ":" + shortSha
imgVer := "ghcr.io/baditaflorin/" + repo + ":" + version
imgLatest := "ghcr.io/baditaflorin/" + repo + ":latest"
run(wt, "docker", "buildx", "build",
    "--platform", "linux/amd64", "--provenance=false",
    "-t", imgSha, "-t", imgVer, "-t", imgLatest, "--push", ".",
)
```

In the compose-rewrite step (currently writes `:latest`):

```go
// Before: rewrite to :latest
pinRemoteComposeImage(bastion, dockerhost, composeDir, repo, "latest")

// After: rewrite to :<short-sha>
pinRemoteComposeImage(bastion, dockerhost, composeDir, repo, shortSha)
```

In the drift-detection step (currently uses `imageTag == version`):

```go
// Before
if currentTag == version { return "no drift" }

// After
headSha, _ := run(repoDir, "git", "rev-parse", "--short", "origin/main")
headSha = strings.TrimSpace(string(headSha))
if currentTag == headSha { return "no drift" }
```

Total diff: ~30 lines changed in 2 files. Tests in
`deploy_helpers_test.go` mostly pass with the swap once the fixture
images are renamed.

## Consequences

**Positive**

* Drift is impossible to mis-detect. The pin IS the source identifier.
  "deploy says no drift but the binary is stale" can no longer happen.
* `--force-build` becomes unnecessary in the common case. It stays as
  an escape hatch for "rebuild even though the sha matches" (e.g.
  build env changed but git tree didn't).
* Rollback is trivial: `pinRemoteComposeImage(…, oldShortSha)` +
  `docker compose up -d`. The image still exists in the registry
  because every build pushes its sha tag.
* `version: "0.3.1"` survives multiple commits without ambiguity. The
  bump-version workflow becomes purely about communicating intent to
  humans — no production deploy depends on it.
* Audit / forensics: `docker inspect <container>` reveals the exact
  source sha that produced the running binary, no `deploy-meta` lookup
  needed.

**Negative**

* Image registry storage grows faster — one extra tag per build. GHCR
  pricing is per-storage so non-trivial over time. Mitigation:
  [`bin/ghcr-prune-sha-tags.sh`](../../bin/ghcr-prune-sha-tags.sh)
  iterates org packages, lists sha-tagged versions, and DELETEs any
  beyond `KEEP_LAST_N` newest **AND** older than `MIN_AGE_DAYS`.
  Defaults to dry-run; pass `--apply` to actually delete. Protects
  `:<semver>` + `:latest` + `:rollback` (the discoverability tags
  ADR-0028 keeps alongside the sha pin). GHCR's org-level retention
  policies aren't API-settable, so per-version DELETE via the GitHub
  API is the only path. Run it from cron or as a periodic
  `fleet-runner` schedule once the policy is dialed in.
* Compose files on the dockerhost mutate on every deploy. They
  shouldn't be in version control (they aren't currently — the
  dockerhost-side `/opt/services/<slug>/docker-compose.yml` is a
  derived artifact). Worth re-confirming none are.
* Operators who hand-edit dockerhost compose will fight the rewrite.
  Convention: don't hand-edit; use `fleet-runner deploy` or
  `fleet-runner render-compose --push`.
* `:latest` continues to drift (it always points at the most recent
  build). Anything that pulls `:latest` outside fleet-runner control
  (e.g. local dev runs, ad-hoc `docker run`) gets a sliding target.
  Not new — same risk as today; the policy doesn't add it.

**Mitigations**

* Add `fleet-runner audit image-sha-drift` (parallel to the existing
  `audit compose-image-drift`) that flags running containers whose
  image sha doesn't match `origin/main`. Cheap CI check after the
  policy lands.
* Add a one-time backfill: for each currently-deployed service, push
  a `:<short_sha>` tag at the current `:latest` and rewrite the
  dockerhost compose to the sha pin. Idempotent; can run via
  `fleet-runner exec` across all 222 services in one pass.

## Migration path

This is a coordinated change to fleet-runner + every service's
compose pin. Roll in three phases:

### Phase 1 — code change, opt-in (no behavioral default change)

1. Implement the `:<short_sha>` build tag — always pushed.
2. Compose-rewrite logic still defaults to `:latest`.
3. Add a flag: `--pin-mode={latest|sha}` (default `latest` for now).
4. Roll out via `fleet-runner` v0.7.0; deploy on LXC 108.

### Phase 2 — backfill sha tags for the fleet

5. One-off script: `fleet-runner exec 'docker manifest create ghcr.io/baditaflorin/$REPO:$SHA ghcr.io/baditaflorin/$REPO:latest && docker manifest push …'` for each repo. (Or: trigger an empty rebuild via `--force-build`.)
6. Verify every repo has at least one `:<short_sha>` tag in GHCR.

### Phase 3 — flip the default + drift-detection

7. Flip `--pin-mode` default to `sha`.
8. Update `fleet-runner deploy`'s drift detection from version-compare
   to sha-compare.
9. Sweep the dockerhost compose files to `:<short_sha>` pins via
   `fleet-runner deploy --pin-mode=sha` for every service. Idempotent
   no-ops where compose is already sha-pinned.
10. Land `fleet-runner audit image-sha-drift` as a CI check.

Rollback for any phase: flip the flag back. Until phase 3, behaviour
is identical to today.

## Alternatives considered

**Detect drift by build-sha embedded in the binary, not by tag**

Considered: bake `git rev-parse HEAD` into the image as a `Version` /
`Commit` const, query via `/version` endpoint, compare to repo HEAD.
This is actually already half-implemented (every service has a
`Version` constant + `commit` field in `/health`). But:

* `/version` requires the container to be running and reachable. Drift
  detection on a crash-looping deploy can't query the binary.
* Network round-trips for what should be a static lookup.
* Tag-as-pin gives us the SAME signal *plus* the ability to roll
  forward/back without rebuilding.

Embedded build-sha stays useful as a runtime confirmation but doesn't
replace tag-based pinning.

**Force semver bump on every functional commit**

Considered: a pre-commit hook bumps `version:` automatically when
non-doc files change. Rejected:

* Noisy commit history — every feature touches version.
* Multi-commit branches accumulate version churn; the final merge
  has 5+ version bumps mid-branch that need to collapse.
* Doesn't solve rapid-iteration cycles (refactor PRs that change a
  lot of code without changing the contract).

Semver should communicate intent to humans, not coerce CI.

**Per-environment overlay (Kustomize-style)**

Considered: keep one base compose + environment-specific overlays
that pin the image. Rejected:

* fleet-runner already does a less-formal version of this with
  `docker-compose.override.yml` (per-service mesh wiring). Layering
  another overlay for image pinning would multiply file count
  without proportional gain.
* Adds a dependency on either Kustomize or hand-rolled merge logic.

The sha-pin lives cleanly in the existing compose-rewrite step.

**Don't pin — embrace `:latest` and roll the dice**

Considered briefly. This IS the status quo. It's how we got the
2026-05-17 incident.

## References

* `go_fleet_runner/deploy_helpers.go::rebuildAndRoll` — current build/push step
* `go_fleet_runner/deploy_remote_compose.go::pinRemoteComposeImage` — compose pin rewrite
* `go_fleet_runner/audit_compose_image_drift.go` — companion audit (already exists)
* `go_fleet_runner/bump_version.go` — current semver bump verb
* ADR-0023 — pipeline gaps phase 1
* ADR-0027 — fleet-auth canonical flow (sibling decision; same dockerhost compose rewrite path)
* 2026-05-17 incident — `fleet-runner deploy` reported "no drift" four times in a row for migrated services that needed a rebuild
* GitOps reference: Flux / Argo both pin by image digest (sha256) for the same reason; we use git-sha as a proxy for build-content sha because the build is deterministic from the git tree
