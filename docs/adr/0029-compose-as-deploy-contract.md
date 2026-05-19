# ADR-0029 — Compose-as-deploy-contract

* **Status**: Proposed
* **Date**: 2026-05-19
* **Authors**: claude-opus-4-7 (with florin)
* **Tags**: deploy, fleet-runner, compose, multi-image, dry

## Context

`fleet-runner deploy` today assumes a fixed shape: one repo = one
Dockerfile at the repo root = one image = one container. That
assumption is an accident of history (the first ~200 fleet services
happened to be single-container Go binaries), not a design principle.

It is already leaking:

* `services-registry/overrides.json` has an `$expand` directive
  (added 2026-05-18 for `go-fleet-metrics-hub`) that lets one repo
  emit N catalog entries when a compose project ships multiple
  independently-addressable services (different host_ports,
  different `*.<mesh>.com` hostnames). The registry knows about the
  shape; fleet-runner does not.
* `scaffold-compose` / `render-compose` / `audit compose-drift` exist
  to paper over the gap between "what fleet-runner builds" and "what
  actually runs on the dockerhost." They keep ratcheting more
  coverage of a contract that was never written down.
* On 2026-05-19, deploying `go-fleet-metrics-hub` after a registry
  + grafana dashboard refresh failed with
  `ERROR: failed to build: failed to solve: failed to read dockerfile:
  open Dockerfile: no such file or directory` — fleet-runner tried
  to build a single image at the repo root, but the repo is a
  compose project (discovery has its own Dockerfile under
  `./discovery/`, prometheus and grafana use upstream images). The
  workaround was a manual tar pipe from Builder LXC 108 to the
  dockerhost plus `docker compose up -d`.

A one-line fix ("teach `deploy` about `$expand replace_parent:
true`") would split the deploy hot path into two near-identical
branches. Every subsequent flag (`--force-build`, smoke gating, sha
pin, rollback) would have to be implemented twice. That is the
technical-debt path.

## Decision

Every fleet deployable produces a `docker compose` project rooted in
its repo. Deploy is the same three-line pipeline regardless of how
many containers the compose project owns:

```
docker compose build
docker compose pull
docker compose up -d
```

Single-image services are a one-service compose (already auto-
scaffolded today via `fleet-runner scaffold-compose`). Multi-image
projects (`go-fleet-metrics-hub`, future coordinator pairs,
HA-keystore, …) ship their own hand-owned compose. **The two are not
two shapes; they are one shape with N = 1 or N > 1.**

### Pipeline contract

| Phase     | Single-image (N=1)                                    | Multi-image (N>1)                                                                |
|-----------|-------------------------------------------------------|----------------------------------------------------------------------------------|
| Build     | `docker compose build` builds the one service.        | Builds whichever services have a `build:` directive; skips upstream-image ones. |
| Image tag | One image gets `:<sha>` pinned in compose.            | N built images get `:<sha>` pinned; upstream-pinned images stay as declared.    |
| Roll      | `docker compose pull && up -d`.                       | Same line. Compose recreates only what changed.                                  |
| Smoke     | One slug → one `/health` probe.                       | N child slugs (from `$expand`) → N `/health` probes.                            |
| Rollback  | Capture pre-roll digest of the one image, retag, up. | Capture digests for every built service; restore all on smoke fail.              |

Every step is the same code path, just iterating over a list of
length 1 or length N. That is the DRY-ness test, and it passes.

### Separation of concerns

* **Registry** (`services.json` + `overrides.json` `$expand`) —
  authoritative for *what catalog entries exist*: slug, host_port,
  mesh, category, url. Already correct today; no schema changes
  from this ADR.
* **Repo** (`service.yaml` + `docker-compose.yml`) — authoritative
  for *runtime shape*: what builds, what mounts, what images.
  Single-image repos get a one-service compose auto-scaffolded;
  multi-image repos hand-own theirs.

Both layers stay decoupled. Registry tells you *what*; repo tells you
*how*. Pushing `$expand` into the repo, or compose-shape into the
registry, is the anti-pattern that creates the worst debt because it
conflates catalog with deploy mechanism.

### Addressing

`fleet-runner deploy <name>` accepts two addressing modes, both
calling the same underlying machinery:

* Repo name (`go-fleet-metrics-hub`) → operates on the whole compose
  project (every child smoke-gated, all built images digest-asserted).
* Child slug (`fleet-discovery`) → operates on just that one service
  inside the compose project (`docker compose up -d discovery`),
  smoke-gated against that one child's url.

## Consequences

### Positive

* One deploy code path. Future flags land once, not twice.
* Sha-pin invariant (ADR-0028) generalises naturally — pin every
  built image, leave upstream pins alone.
* Compose-project repos stop being "second class." No more
  manual-tarball workarounds.
* The same `Deployable` abstraction will let future runtimes
  (`runtime: k8s`, `runtime: nomad`, …) plug in cleanly when they
  arrive, by implementing the same five methods.

### Negative

* Internal refactor of `deploy.go` (~1000 LOC). Largest single
  change to fleet-runner since the deploy-pipeline-gaps work
  (ADR-0023).
* Two implementations of the `Deployable` interface need to stay
  consistent. Mitigated by a shared acceptance test suite that
  asserts byte-identical output for every existing single-image
  service.

### Mitigations

* **Phase 1 lands without changing single-image behavior.** The
  `Deployable` interface dispatches to today's `deployRun` function
  unchanged for single-image services. Only multi-image (i.e.
  `$expand replace_parent: true`) repos take the new path. Single-
  image services produce byte-identical output before and after.
* **Acceptance test**: golden-file capture of `fleet-runner deploy
  <slug> --dry-run` for ~10 representative single-image services
  before Phase 1, replayed after. Any diff fails CI.
* **Rollback plan**: if Phase 1 introduces a regression, the
  dispatch shim (a `switch` on
  `registry.expand.replace_parent`) is a 5-line revert.

## Migration path

### Phase 1 — Lift the assumption out of `deploy`

* Add `Deployable` interface to `go_fleet_runner`:
  ```go
  type Deployable interface {
      Build(ctx context.Context, opts BuildOpts) (BuildResult, error)
      Pull(ctx context.Context) error
      Roll(ctx context.Context) (RollResult, error)
      Smoke(ctx context.Context) error
      Rollback(ctx context.Context, prior RollResult) error
  }
  ```
* Two implementations:
  - `SingleImageDeployable` — wraps today's `deployRun` flow
    unchanged. Tests-as-spec: existing deploys produce byte-
    identical output.
  - `ComposeProjectDeployable` — new path, used only when
    `overrides.json $expand[replace_parent=true]` matches the repo.
* Dispatch in `cmdDeploy`:
  ```go
  d := selectDeployable(svc, registry)  // 5 lines
  d.Build(...); d.Pull(...); d.Roll(...); d.Smoke(...);
  ```

### Phase 2 — Generalise surrounding subcommands

* `bump-version` — for multi-image repos, write `service.yaml` once
  at repo root; children share the parent version unless `$expand`
  declares a `versions:` map (add lazily, only when a real
  divergence appears).
* `audit compose-drift` / `audit compose-image-drift` — already
  compose-aware; verify they cover the multi-image case.
* `image-tag policy` (ADR-0028) — extend the rewriter that pins
  `:<sha>` in compose files to handle multiple `build:` services.

### Phase 3 — Discoverability

* New `fleet-runner audit compose-shape` — reports which repos are
  single-image vs compose-project vs mismatched (e.g. has `$expand`
  but no multi-service compose).
* `SERVICE-TEMPLATE.md` gains a "compose-project variant" section
  describing the registered shape.

## Alternatives considered

### A — Special-case multi-image repos in `deployRun`

A `switch` early in `deployRun` on `$expand replace_parent`, routing
to a separate `deployComposeProject` function. Smaller diff but
clones the deploy pipeline; every future flag (smoke gating,
rollback, force-build, dry-run) has to be implemented twice. **Path
of accumulating debt.**

### B — Add `kind: compose-project` to `services.json`

Would let the registry declare runtime shape. Rejected: conflates
*what catalog entries exist* with *how they deploy*. The registry's
audience is the catalog UI, sibling discovery clients, the public
docs. The deploy mechanism is fleet-runner's audience. Different
concerns, different schemas.

### C — Move every service's Dockerfile to a `./service/` subfolder

Would force uniformity. Rejected: gratuitous churn across ~230
repos, no upside for single-image services, breaks every existing
deploy until each repo migrates.

### D — Adopt kubernetes / nomad instead of compose-projects

Out of scope. The fleet runs on a single dockerhost VM by design
(see fleet topology in `services-registry/CLAUDE.md`). The compose-
as-contract decision is orthogonal to a future runtime swap — the
`Deployable` interface would let kubernetes / nomad plug in as new
implementations without touching the dispatch layer.

## References

* ADR-0023 — deploy-pipeline-gaps (the last major deploy refactor)
* ADR-0028 — image-tagging-and-version-bump policy (the per-sha
  pin invariant this ADR generalises to multi-image)
* `services-registry/overrides.json` — `$expand` directive
* `go_fleet_runner/deploy.go` — the file Phase 1 refactors
* `go-fleet-metrics-hub` — first multi-image repo in the fleet;
  motivating incident
* 2026-05-19 deploy failure / manual-tarball workaround — see
  `fleet-state/RUNBOOK.md` "metrics-hub manual deploy" if it ends
  up captured there
