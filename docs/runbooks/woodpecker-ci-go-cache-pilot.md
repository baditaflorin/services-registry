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

1. **Before numbers.** On the host, from an existing checkout of a representative
   Go repository and a second, different one:

   ```sh
   scripts/ci-cache/bench.sh --src ~/work/go_example_a --other-src ~/work/go_example_b
   ```

   Keep the output. `cold` is today's cost, `warm` a re-run of the same commit,
   `warm-other` a repository's first run once the fleet has built the shared
   dependencies, and `peak_mem_MiB` is the container's peak memory in each case.
2. **Check and prepare the host** (dry run first):

   ```sh
   scripts/ci-cache/install-host.sh              # checks, prints the plan
   sudo scripts/ci-cache/install-host.sh --apply # creates the directories
   ```
3. **Enable it** on that host only: add `WOODPECKER_CI_CACHE_ROOT` to the agent's
   `.env`, put the override file next to the base compose file and recreate just
   the agent (the exact commands are printed by the install script). Then prove it
   took effect: a step container started after the restart must list the binds:

   ```sh
   docker inspect "$(docker ps -q --filter label=com.woodpecker-ci.step | head -1)" \
     --format '{{range .HostConfig.Binds}}{{println .}}{{end}}'
   ```

   Record the cutover time (UTC).
4. **Keep it bounded.** Schedule daily:
   `scripts/ci-cache/prune.sh --root /var/cache/woodpecker-ci`. Go also trims its
   own build cache entries that were unused for 5 days; this is the hard cap.
5. **Measure after 3 days** (so enough runs land on the pilot):

   ```sh
   python3 bin/ci_cache_pilot_report.py --plane ci.0exec.com \
     --pilot-agent <agent name> --cutover 2026-10-20T09:00:00Z --repos-file go-sample-repos.txt
   ```

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
5. **Reversible.** Rolling back (below) was exercised once and the next pipelines
   ran normally.

## Memory and disk

The caches live on disk, not in RAM. The kernel's page cache may use free memory
to speed them up and gives it back under pressure. The memory risk in a Go CI step
is the compiler, and a warm cache makes that smaller, because less is compiled.
The existing `GOFLAGS=-p=2` and the agent's slot count stay as they are.

Cache sizes are PREDICTED to be a few GB per Go version; the caps in the env file
are the safety net and `prune.sh` enforces them without stopping running jobs.

## Rollback

Stop passing the override file and recreate the agent; delete the cache root when
convenient. The next pipelines are cold runs, as before.

## Risks and limits

- **One trust domain per host.** Anything that runs in a CI step on this host can
  write to the shared caches, and later steps of any repository on that host read
  them, so one compromised step could affect the others. Enable this only on hosts
  whose CI runs code from this organisation's own members and automation, never on
  a host that also builds third-party or fork pull requests. Deployed images are
  built on the separate builder pool, not from these caches; these caches only
  speed up and influence CI verification. The longer-term design removes the
  shared-write assumption (a remote cache that only trusted builds may write).
- **Host-side tooling treats the cache as untrusted input.** `prune.sh` runs as
  root over a tree that containers can write to, so it refuses symlinked cache
  directories, skips symlinks inside them, and only ever deletes inside the
  configured root.
- **`bench.sh` runs the repository's own tests** on the host: point it only at
  repositories you trust.
- **Per host, not fleet wide.** Five hosts means five caches, so a job landing on a
  cold host is cold. The step after this pilot, if it pays off, is Go's
  `GOCACHEPROG` (a shared cache server all hosts use), not more directories.
- **Assumes root steps and the default paths** of the official images. A step image
  that runs as another user or sets its own `GOCACHE` or `GOMODCACHE` simply
  misses the cache; it does not break.
- **Needs cgroup v2 for the peak memory column** of `bench.sh` on older kernels it
  prints `na`.

## What this does not fix

- **Queue bursts.** Median queue wait is 3 s but p90 is 537 s: a fleet-wide
  dependency bump queues hundreds of pipelines at once. Caching makes each faster
  but does not change the arrival burst. That needs rollout throttling or more
  slots, and is a separate change.
- **Docker image builds.** `docker buildx` cache mounts live on the build pool
  workers, which are chosen per build, so they are cold more often than they need
  to be. Also separate.
