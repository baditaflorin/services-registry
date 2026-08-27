# ADR-0035 — Self-hosted push-triggered CI (Woodpecker) on Builder LXC 108

* **Status**: Accepted (2026-08-26 — see "Rollout status" below)
* **Date**: 2026-08-06
* **Authors**: baditaflorin + claude-sonnet-5
* **Tags**: ci, infra, builder-lxc, ops

## Context

Per this repo's own `CLAUDE.md` ("Local workflow"): *"CI: there is
none. Husky pre-commit hooks + local `npm run smoke` (Node repos) or
`go test ./...` (Go repos) are the gate. Don't scaffold GitHub Actions
build workflows"* — a deliberate choice to avoid GitHub Actions
billing, not an oversight.

That leaves a real gap: the only automated build/test signal any repo
gets is (a) whatever pre-commit hook ran on the pusher's own machine,
and (b) the `go build`/`go test` pre-flight `fleet-runner deploy`
already runs against a fresh `origin/main` worktree on Builder LXC 108
— but only at **deploy time**, for the one repo being deployed, only
when a human or agent happens to trigger it. A push that bypasses or
lacks a working hook (different machine, hook not installed, `mesh-*`
repo, parallel session per the "pull often, push often" section above)
produces zero automated feedback until someone happens to redeploy
that exact repo — which for rarely-touched services can be weeks.

Builder LXC 108 already does the load-bearing part of this job ad hoc:
`fleet-runner deploy` / `deploy-all` builds with `docker buildx build
--platform linux/amd64 --push`, runs Go pre-flight, and rolls the
dockerhost. It's proven capacity for this exact workload. What's
missing is a **push-triggered** gate, decoupled from "did someone
remember to deploy this repo."

Investigated as part of this ADR (2026-08-06 session): Builder LXC 108
is 12 vCPU / 12GB RAM (not the assumed 4/8), and was at 95% disk
(3.3GB free of 59GB) — reclaimed to 18GB free via `docker image prune
-a` (dangling/unused image tags, ~0GB net — mostly deduped layers) and
`docker builder prune -a` (16.78GB reclaimed from stale BuildKit
cache). Confirmed live via SSH that the LXC was mid-deploy
(`fleet-runner deploy-all` batching ~80 repos) at the time of the
audit — the load average of ~24 was transient build load, not chronic
overload.

## Decision

Run **Woodpecker CI** (Apache-2.0, self-hosted, Drone-YAML-compatible,
single Go binary) server + agent(s) on Builder LXC 108, alongside the
existing `fleet-runner` binary and BuildKit setup. GitHub webhooks
trigger a pipeline on push; the pipeline runs the same gate `fleet-runner
deploy` already trusts (`go build ./... && go test ./...` in a fresh
worktree, or `npm run smoke` for Node/`mesh-*` repos) so push-time and
deploy-time verification stay in sync instead of drifting into two
separate definitions of "green."

Woodpecker was picked over GitHub Actions (would reintroduce the
billing this repo explicitly opts out of) and over standing up a new
host (Builder LXC 108 already has spare CPU headroom, buildx, and
network access to the dockerhost fleet for a follow-on deploy trigger).

## Consequences

**Positive:**
- Closes the actual gap: broken code gets flagged on push, not
  whenever someone next happens to deploy that repo.
- Zero GitHub Actions spend — consistent with the standing "build
  locally only" constraint; cost is Builder LXC 108's existing,
  already-idle-most-of-the-time capacity.
- One dashboard for build status across the fleet instead of trusting
  scattered local hook logs per repo/per machine.
- Formalizes a job the LXC is already doing manually via
  `fleet-runner deploy`'s pre-flight step.

**Negative:**
- New persistent daemon (server + agent) to operate and patch on a
  host that already has real operational history (the 2026-05-29
  dockerhost fork-bomb incident that motivated `host-conventions.yaml`
  resource caps applies in spirit here too — bound the agent's own
  concurrent-pipeline count so a CI burst can't starve `fleet-runner`
  deploys running on the same LXC).
- Needs a GitHub webhook/App with access to whichever repos are
  onboarded — widens credential surface on a host reachable only via
  the `0docker.com` bastion.
- Disk is finite (18GB free post-cleanup on a 59GB volume already
  carrying BuildKit cache + workspace checkouts). CI pipelines add
  their own cache growth on top of `fleet-runner`'s. **Mitigation**:
  periodic `docker builder prune` (cron or pre-flight disk check),
  documented as a follow-up — no automated prune exists yet as of this
  ADR.
- CPU contention: Builder LXC 108 is 12 vCPU shared between
  `fleet-runner deploy-all` batches and CI pipelines. **Mitigation**:
  cap Woodpecker's concurrent pipeline count well below 12 so a deploy
  batch always has headroom; revisit if contention shows up in
  practice.

## Migration path

1. Install Woodpecker server + agent on Builder LXC 108 (not the
   dockerhost fleet — keeps CI load off production containers).
2. Pilot on a small repo subset (2-3 low-traffic `go_domain_*` repos)
   before fleet-wide GitHub App rollout, to validate the pipeline
   definition and resource caps under real load.
3. Pipeline definition (`.woodpecker.yml` per repo) mirrors the
   existing gate: `go build ./... && go test ./...` for
   `language: go` container repos, `npm run smoke` for Node /
   `mesh-*` repos — same commands the pre-commit hook and
   `fleet-runner deploy` pre-flight already run, so there is exactly
   one definition of "green," not two.
4. Once the pilot is stable, roll out fleet-wide via whatever
   templating mechanism `fleet-runner inject` already uses to
   propagate `CLAUDE.md` — one canonical `.woodpecker.yml` template,
   not 220 hand-written copies.
5. Flip this ADR's `Status` to `Accepted` in the same PR that lands
   the working pilot (per ADR-0001's workflow). **Not what actually
   happened** — the pilot scaled to fleet-wide over the following
   three weeks without this step ever landing; `Status` sat at
   `Proposed` long after rollout was materially complete. Flipped
   2026-08-26, alongside the rollout-status audit below, once a
   session doing an unrelated task noticed the drift. Lesson for
   future ADRs: step 5 needs a concrete trigger (e.g. "when N% of
   eligible repos are onboarded"), not "in the same PR" for a rollout
   that's intentionally incremental across many small PRs, none of
   which is "the" pilot-landing PR.
6. Deferred, not in scope of the pilot: wiring a green pipeline to
   auto-trigger `fleet-runner deploy` — for now, CI reports status only;
   deploy stays a separate, explicit `fleet-runner deploy` invocation.

## Rollout status (as of 2026-08-26)

No separate rollout ledger exists (per this repo's `CLAUDE.md`: "tracked
via `git log` on this file / repo PRs") — this section is a point-in-time
snapshot, not a maintained tracker; don't treat a stale copy of these
numbers as current.

- **334 of 369 locally-checked-out, registered `kind=container` repos**
  had `.woodpecker.yml` as of this audit (90.5%). Of the 35 without it,
  22 lack `go.mod` (composite-pattern/non-Go services — correctly out of
  scope per this ADR's step 3) and 13 were eligible but missing.
- All 13 are now accounted for: 2 (`domain-abuse-contact-rollup`,
  `domain-trust-composite-grader`) were onboarded independently,
  concurrently, by a separate session between the initial count and
  this fix; the other 11 got `.woodpecker.yml` added same-day as part
  of this audit.
- **Adding `.woodpecker.yml` alone does not activate CI.** Woodpecker's
  GitHub webhook carries a repo-specific signed token
  (`repo-forge-remote-id` embedded in the JWT); this can only be minted
  by Woodpecker's own API, authenticated as the OAuth-connected GitHub
  user. **RESOLVED 2026-08-27**: the user logged into `ci.0exec.com`
  once (the one human step that can't be automated away — OAuth
  requires it by design) and generated a personal API token via
  Settings → CLI, which was then used to activate all 11 repos
  unattended via `POST /api/repos?forge_remote_id=<github-repo-id>`
  (Woodpecker's real activation endpoint — note this is NOT
  `/api/repos/{owner}/{name}`, which silently 200s with the SPA's HTML
  shell instead of erroring, an easy trap). Verified independently via
  the `gh api .../hooks` method below for all 11.
  - **A red herring worth recording**: before finding the right
    endpoint, `GET /api/user/repos?all=true` (meant to list every
    forge-visible repo) did not include these 11 repos, or several
    known-public ones like `services-registry`/`go-common` either —
    this looked exactly like a GitHub App with a curated,
    selectively-granted repository list (a real and common pattern),
    and momentarily was reported to the user as the blocker. It
    wasn't — the user's own "Installed GitHub Apps" page had no
    Woodpecker entry at all, and once the correct `forge_remote_id`
    activation call was used directly, every one of the 11 activated
    immediately with no GitHub-side permission grant needed. That
    listing endpoint's incompleteness (confirmed separately to also be
    a pagination artifact, capped well under the fleet's real repo
    count, not a `perPage`-respecting cap) is a real, still-open
    Woodpecker rough edge — just not this one.
- Verification method: `gh api repos/baditaflorin/<repo>/hooks` — an
  activated repo shows a `web` hook with `config.url` starting
  `https://ci.0exec.com/api/hook?access_token=...`; an inactive one
  returns `[]`. No Woodpecker credential needed for this read.

## Alternatives considered

1. **GitHub Actions** — rejected outright; reintroduces the exact
   billing this repo's `CLAUDE.md` already opted out of ("Don't
   scaffold GitHub Actions build workflows").
2. **Drone CI** — Woodpecker is the maintained open-source continuation
   after Drone's license change; picking Drone today means picking an
   unmaintained base.
3. **Jenkins** — heavier ops footprint (JVM, plugin sprawl) than this
   fleet's single-Go-binary operational style; no clear advantage over
   Woodpecker for this scope.
4. **Status quo (pre-commit + deploy-time pre-flight only)** — proven
   insufficient: it depends on the pusher's machine having a working
   hook and on someone eventually redeploying the affected repo. Does
   not catch the exact concurrent-session / stale-workspace failure
   modes this repo's "pull often, push often" section documents.
5. **Provision a new dedicated CI host** — rejected; Builder LXC 108
   already has spare CPU (12 vCPU, mostly idle outside deploy bursts),
   buildx, and network access to the dockerhost — a new host adds
   operational surface without solving a capacity problem that doesn't
   exist.

## References

- `services-registry/CLAUDE.md` — "Local workflow" (CI stance) and
  "Infrastructure topology" (Builder LXC 108 role).
- [ADR-0040](0040-single-ci-authority-multi-agent-routing.md) — keeps one
  webhook/control plane per repository while pooling execution capacity across
  multiple agent hosts.
- `services-registry/host-conventions.yaml` — the 2026-05-29
  fork-bomb incident and resource-cap precedent this ADR's mitigation
  section follows.
- [ADR-0001](0001-adr-process.md) — ADR process this document follows.
- [ADR-0028](0028-image-tagging-and-version-bump-policy.md) — image
  tagging policy the CI pipeline should not duplicate or diverge from.
- 2026-08-06 session: live Builder LXC 108 audit (specs, disk cleanup,
  in-flight `deploy-all` batch confirmation).
