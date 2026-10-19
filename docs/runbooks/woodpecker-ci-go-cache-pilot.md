# Shared Go and npm caches on one Woodpecker agent (pilot)

Every fleet Go repository runs the canonical template
(`templates/woodpecker-go-ci.yml`): a fresh `golang:<ver>` container, then
`go build ./...` and `go test ./...`. Nothing survives between runs, so each run
downloads every module and recompiles every dependency, and `go test` can never
reuse a result. This pilot makes one agent host keep three caches on disk and
mount them into every step it starts. No repository changes.

## Why this should help (evidence, not promises)

OBSERVED 2026-10-17, read from the services registry and GitHub commit statuses:

- 374 Go container repositories. In a random sample of 11 `go.mod` files, 94% of
  the `module@version` entries appear in at least 3 repositories, 40 of 61
  distinct module versions appear in all 11, and only 1 of 60 modules differs
  by version (go-common v0.102.56 vs .57). One repository's compile work is
  therefore mostly another repository's cache hit.
- 65 successful workflow runs across 12 of those repositories: median 37 s, mean
  53 s, p90 85 s. Queue wait is a median 3 s but p90 537 s (bursts, for example a
  fleet-wide dependency bump). So caching shortens the 53 s; it does not fix
  bursts. See "What this does not fix".
PREDICTED, not yet measured: how much of the 53 s is download and compile. The
benchmark and the report below exist to turn that into a number before anything
is widened.

## What "agent level" means

Woodpecker has one server (`ci.0exec.com`) that receives GitHub webhooks and
schedules work, and agents: a `woodpeckerci/woodpecker-agent` container on each
build host. The agent starts a fresh Docker container for every pipeline step on
its host's Docker daemon (through `docker.sock`). The agent's own environment is
where host-wide behaviour is set, and one setting there reaches every repository
that runs on it, with no change to any of them.

The setting is `WOODPECKER_BACKEND_DOCKER_VOLUMES`: a comma separated list of
`host:container` mounts added to every step container. Verified in the Woodpecker
source (`pipeline/backend/docker/docker.go`, `StartStep`:
`hostConfig.Binds = DeduplicateStrings(append(hostConfig.Binds, e.config.volumes...))`):
it applies to every step, with no per-repository trust check (that check only
looks at a `volumes:` key written in a workflow file). Confirm it on the live
host with step 3 below, because the agent version on the hosts is older than the
source read.

## The change

| Piece | Where | Effect |
|---|---|---|
| `templates/woodpecker-agent-ci-cache.override.yml` | compose override for ONE host | mounts `go-build`, `go-mod`, `npm` into every step |
| `templates/woodpecker-agent-ci-cache.env.example` | the agent's `.env` | cache root and caps |
| `scripts/ci-cache/install-host.sh` | pilot host | checks Docker and free disk, creates the directories, prints the next commands |
| `scripts/ci-cache/apply-agent.sh` | pilot host | switches the agent (or rolls it back) only in a real idle window, health-checked |
| `scripts/ci-cache/prune.sh` | pilot host, daily | keeps each cache under its cap |
| `scripts/ci-cache/bench.sh` | pilot host | cold, warm and warm-from-another-repo timings plus peak memory, no API needed |
| `bin/ci_cache_pilot_report.py` | anywhere with a Woodpecker token | before/after against the other agents |

The base agent template is deliberately untouched, so no other host changes. The
per-repository template is untouched too: the mounts sit at the default paths of
the official `golang` and `node` images running as root
(`/root/.cache/go-build`, `/go/pkg/mod`, `/root/.npm`), which is how the fleet's
workflows already run them.

## Procedure (one host)

Pick the host with the most free disk and enough memory for its slot count. The
runbook `woodpecker-cross-host-capacity.md` lists the schedulable agents and their
slots; do not change `WOODPECKER_MAX_WORKFLOWS` as part of this pilot.

**Recreating the agent kills every step it is running** (they exit 137 and the
workflows are reported as canceled), and a busy host is rarely idle. That is why
the switch is a held, waited, health-checked step and not a plain `docker compose
up -d`. Hosts also differ from the registry template: use the host's real compose
file, service and project names (`docker ps` shows the project in the container
name; `docker inspect` shows it as the label `com.docker.compose.project`).

1. **Before numbers.** On the host, from an existing checkout of a representative
   Go repository and a second, different one:

   ```sh
   scripts/ci-cache/bench.sh --src ~/work/go_example_a --other-src ~/work/go_example_b
   ```

   Keep the output. `cold` is today's cost, `warm` a re-run of the same commit,
   `warm-other` a repository's first run once the fleet has built the shared
   dependencies, and `peak_mem_MiB` is the container's peak memory in each case.
2. **Prepare the host** (dry run first; it validates the service name against the
   compose file and writes the override for THAT service):

   ```sh
   scripts/ci-cache/install-host.sh --compose-dir /opt/woodpecker-agent \
     --compose-file compose.yml --service woodpecker-agent --project <project>
   sudo scripts/ci-cache/install-host.sh --compose-dir ... --apply   # same flags
   ```
3. **Hold the agent** in Woodpecker (Admin > Agents > the agent > "No schedule", an
   admin action) and wait until it has no running workflow
   (`fleet-runner woodpecker queue --plane 0exec`).
4. **Switch it:**

   ```sh
   scripts/ci-cache/apply-agent.sh --compose-dir /opt/woodpecker-agent \
     --compose-file compose.yml --service woodpecker-agent --project <project>
   ```

   It waits for a real idle window (no container with the label `wp_uuid`), changes
   nothing if none comes, recreates only the agent and rolls back by itself if the
   agent is not healthy. Then release the hold and note the cutover time it wrote
   (`.ci-cache-cutover`, UTC).
5. **Prove the mounts reached the steps.** A step container started after the
   switch must list the three binds:

   ```sh
   docker inspect "$(docker ps -q --filter label=wp_uuid | head -1)" \
     --format '{{range .HostConfig.Binds}}{{println .}}{{end}}'
   ```
6. **Keep it bounded.** Schedule daily:
   `scripts/ci-cache/prune.sh --root /var/cache/woodpecker-ci`. Go also trims its
   own build cache entries that were unused for 5 days; this is the hard cap.
7. **Measure after 3 days** (so enough runs land on the pilot):

   ```sh
   python3 bin/ci_cache_pilot_report.py --plane ci.0exec.com \
     --pilot-agent <agent name> --cutover <cutover UTC> --repos-file go-sample-repos.txt
   ```

## What the first run on a real host showed

OBSERVED on the pilot host, 2026-10-17 (one run each, host busy):

| Case (fleet Go service, `golang:1.25`, `go build ./... && go test ./...`) | Seconds | Peak memory |
|---|---|---|
| cold: empty caches, today's cost | 67 | 1221 MiB |
| warm: same commit again | 12 | 218 MiB |
| warm-other: first run after a different service warmed the caches | 26 | 432 MiB |

A warm cache cut a run by 61% to 82% and its peak memory by 65% to 82%. These are
single runs on a busy host; repeat before quoting them.

Second switch, with `apply-agent.sh`, OBSERVED the same day: it waited for a real
idle window (the whole CI plane happened to be quiet), recreated only the agent, and
cancelled no pipeline. Every one of the first 12 step containers started afterwards
(clone plugin, `golang`, `node`, `postgres` and `pgvector` service containers)
carried all three cache mounts, the caches reached about 2 GB in the first ten
minutes, host memory stayed flat, and no pipeline failed or was cancelled in the
following ten minutes. Steps started before the switch had none of the mounts,
which is the control. This verifies the mechanism; the speedup on real pipelines
is what `ci_cache_pilot_report.py` still has to show after a few days.

A same-host A/B on the real pipeline path (OBSERVED, three runs): a throwaway
workflow pinned to the pilot agent built and tested the public `go-common` with the
mounted caches bypassed, then with them on, in one pipeline.

| Run | cache bypassed | cache on, first | cache on, re-run | peak memory (bypassed / on / re-run) |
|---|---|---|---|---|
| 1 | 44 s | 18 s (-59%) | 1 s | 1331 / 475 / 132 MiB |
| 2 | 33 s | 3 s | 1 s | 1445 / 128 / 127 MiB |
| 3 | 31 s | 1 s | 1 s | 1553 / 143 / 141 MiB |

Only run 1 is a true "first run" (runs 2 and 3 re-ran the same commit, so their
cache-on rows are warm re-runs). Cache bypassed is today's cost: 31 to 44 s and about
1.3 to 1.6 GB. All steps built and tested successfully. This meets the "at least 25%
faster" and "no memory regression" criteria for this workload; it is one small public
module, so the percentages, not the seconds, are what carries over to other repos.

The same session corrected the procedure, which is why this runbook has the
hold-and-wait step: the first switch used a plain recreate and an idle check on the
wrong container label, and killed pipelines that were running (a long Go job and
two frontend workflows). Also, a pull request whose clone step failed in the same
minutes turned out to be an unrelated merge conflict, so check a failure's cause
before blaming the change.

## Acceptance criteria

All five must hold before the setting is applied to a second host:

1. **Faster.** `effect` below 0.75 (at least 25% faster than the other agents over
   the same window) over at least 10 (repository, workflow) pairs.
2. **Not less reliable.** Failed-pipeline share on the pilot after the cutover is
   no higher than on the other agents (check in Woodpecker for the same window).
   A failure that mentions a cache, checksum or "file exists" error counts as a
   failed pilot immediately.
3. **Bounded disk.** The three directories together stay under the caps in the env
   file (default 17 GB): `du -sh /var/cache/woodpecker-ci/*` at the end of day 1,
   2 and 3.
4. **No memory regression.** The host's lowest available memory during the pilot is
   no lower than in the three days before (fleet metrics hub), and the `bench.sh`
   peak memory of `warm` is not above `cold`.
5. **Reversible and harmless to switch.** `apply-agent.sh --rollback` is exercised
   once, and neither the switch nor the rollback cancels a pipeline (both are done
   behind a hold).

## Memory and disk

The caches live on disk, not in RAM. The kernel's page cache may use free memory
to speed them up and gives it back under pressure. The memory risk in a Go CI step
is the compiler, and a warm cache makes that smaller, because less is compiled.
The existing `GOFLAGS=-p=2` and the agent's slot count stay as they are.

Cache sizes are PREDICTED to be a few GB per Go version; the caps in the env file
are the safety net and `prune.sh` enforces them without stopping running jobs.

## Rollback

Hold the agent, then `scripts/ci-cache/apply-agent.sh ... --rollback` (same flags);
delete the cache root when convenient. The next pipelines are cold runs, as before.

## Risks and limits

- **One trust domain per host.** Anything that runs in a CI step on this host can
  write to the shared caches, and later steps of any repository on that host read
  them, so one compromised step could affect the others. Enable this only on hosts
  whose CI runs code from this organisation's own members and automation, never on
  a host that also builds third-party or fork pull requests. Deployed images are
  built on the separate builder pool, not from these caches; these caches only
  speed up and influence CI verification. A remote cache that only trusted builds
  may write would remove the shared-write assumption, but a remote cache was
  measured and not pursued (see "Why not a shared cache server" below).
- **Host-side tooling treats the cache as untrusted input.** `prune.sh` runs as
  root over a tree that containers can write to, so it refuses symlinked cache
  directories, skips symlinks inside them, and only ever deletes inside the
  configured root.
- **`bench.sh` runs the repository's own tests** on the host: point it only at
  repositories you trust.
- **Per host, not fleet wide.** Five hosts means five caches, so a job landing on a
  cold host is cold. A shared cache server (Go's `GOCACHEPROG`) was tried to fix
  that and lost, see the next section; the answer is more hosts with a cache
  directory, sized to their disks, not a server.
- **Do not set `GOCACHEPROG` in a step on a host that mounts these directories.**
  It replaces `GOCACHE`, so that step stops using the mounted cache.
- **Assumes root steps and the default paths** of the official images. A step image
  that runs as another user or sets its own `GOCACHE` or `GOMODCACHE` simply
  misses the cache; it does not break.
- **Needs cgroup v2 for the peak memory column** of `bench.sh` on older kernels it
  prints `na`.

## Why not a shared cache server

Tried 2026-10-18 and 2026-10-19 and dropped. A `GOCACHEPROG` server that all agents share
(an HTTP service, fail-open client with timeouts and a circuit breaker) was run on the pilot
host and used from the agents on the other sites. Build plus test of go-common (65 s with no
cache), fresh container, same commit, `GOFLAGS=-p=2`:

| Where the cache lives | Re-run |
| --- | --- |
| mounted directories on the same host (this pilot) | **6 s** |
| the server, on the same host | 7 s; 52 s when its circuit breaker interrupted the first fill |
| the server, 17 ms away | 72 s (102 s with the slow-response guard off, every lookup a hit) |
| the server, 21 ms away | 68 s |
| the server, 38 ms away | 66 s |

Every cache lookup is one HTTP round trip and a build plus test makes about 5,800 of them
(effectively one at a time at `-p=2`), so the server only pays when it is next door, and then
a plain directory does the same without a breaker. Cold hosts stay cold; the remedy is a cache
directory on each agent that has the disk for one.

## What this does not fix

- **Queue bursts.** Median queue wait is 3 s but p90 is 537 s: a fleet-wide
  dependency bump queues hundreds of pipelines at once. Caching makes each faster
  but does not change the arrival burst. That needs rollout throttling or more
  slots, and is a separate change.
- **Docker image builds.** `docker buildx` cache mounts live on the build pool
  workers, which are chosen per build, so they are cold more often than they need
  to be. Also separate.
