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
   `scripts/ci-cache/prune.sh --root /var/cache/woodpecker-ci`, for example with the
   systemd timer in `templates/woodpecker-ci-cache-prune.{service,timer}` (install
   steps in the service file's header; 03:15 UTC, low priority). Go also trims its
   own build cache entries that were unused for 5 days; this is the hard cap.
7. **Measure after 3 days** (so enough runs land on the pilot):

   ```sh
   python3 bin/ci_cache_pilot_report.py --plane ci.0exec.com \
     --pilot-agent <agent name> --cutover <cutover UTC> --repos-file go-sample-repos.txt
   ```

   Without a Woodpecker API token, the same method runs through `fleet-runner`'s read-only
   verb for one repository, and adds a per-step table:

   ```sh
   python3 bin/ci_cache_pilot_fleet_runner_report.py --repo owner/name \
     --first <first pipeline number> --last <last> --pilot-agent-id <id> --cutover <cutover UTC>
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

## What production showed after the first days (2026-10-19)

Method: the token-free report above on one busy repository, 842 pipelines (numbers 8450 to
9570; numbers that answer HTTP 404 do not exist and are not read errors), 4,757 successful
workflow runs, cutover 2026-10-17T10:50:47Z, the pilot agent against the five other agents.
The pilot agent was idle for long stretches (no work on a Sunday and overnight, plus a short
Woodpecker server outage that made it restart), so only 140 workflow runs fall after the cutover.

| Measure | Result |
| --- | --- |
| difference-in-differences effect, all workflows (7 to 8 pairs) | **0.84 to 0.94**: 6 to 16 % faster than the other agents, against the 25 % bar |
| `go` workflow, pilot, after the cutover | median 464 s over 8 runs; the other agents 354 s (before the cutover: 413 s against 275 s) |
| `main-health-go` (build and vet only, the cleanest Go step) | 1 run on the pilot agent so far: 155 s; the others 128 s |

**Reading: inconclusive, and not a pass.** The cache effect cannot be seen because the pilot
host is slower than the other agents at everything, before the cutover as well as after, and
that dwarfs the compile time the cache removes. Median step times after the cutover, pilot
agent against the others: the repository clone 32 s against 11 s; `docs-refs` 29 s against 2 s;
`large-files` 16 s against 4 s; `changelog-format` 31 s against 6 s. These are steps that run for
a few seconds anywhere else, so the cost is per container, not per compile.

Cause found on the host (OBSERVED 2026-10-19): starting a container from a cached image took
6.5 to 10 s (well under a second is normal); the CPU was idle (load 0.75 on 10 cores) while
tasks were stalled on disk I/O 34 % of the time; a synchronous 4 KB write took **35 ms**,
against 1.2 ms and 0.5 ms on two other agents. The host's virtual disk is backed by spinning
disks without a fast log device, so every `fsync` of a container layer, a clone or a cache
file waits for them. Fixing that comes before judging this pilot: re-measure after it.
Synthetic runs on the same host (the "first run" and A/B sections above) are unaffected, they
time only the compile.

### Same day: why the pilot agent gets so little work, and the disk change

**The load controller holds the pilot agent most of the time (OBSERVED).** The load-aware
admission controller stops sending work to an agent whose host has had high I/O pressure for a
while, and resumes it only after about ten minutes in a row with low pressure. A held agent still
shows as online and healthy. From the controller's own journal over 26 hours, the pilot host was
**held 82 % of the time** (2,567 samples about 30 s apart), with a mean I/O pressure between 17 and
79 % an hour. Identical small probe workflows pinned by site label finished in about 6 s on three
other sites while the one pinned to the pilot host waited more than 50 minutes and ran nowhere.
That, more than anything about caching, explains the thin traffic behind the numbers above. An
image builder on the same host (about 750 MB written in 30 s while the agent was held) keeps the
pressure above the resume line.

**Change applied 2026-10-19 20:13:52 UTC:** synchronous-write handling of that VM's disk was set
to "disabled" on the hypervisor (a crash or power loss can lose the last ~5 s of writes; the VM
has a nightly backup; revert with `zfs inherit sync <the VM's disk dataset>`). Same tests before and
after, under similar load:

| Test on the pilot host | Before | After |
| --- | --- | --- |
| synchronous 4 KB write | 47 and 62 ms | 0.4 to 1.8 ms |
| `docker run alpine true`, image cached (median) | 1.9 s | 0.65 s |
| same moment, other agents, same test | 0.3 to 0.4 s | 0.3 to 0.4 s |

The gap to the other agents shrank from about 10-20 times to about 1.6 times. Occasional
multi-second stalls remain (the pool is shared and made of spinning disks); a fast log device in
that pool is the lasting fix.

**Does the hold shrink?** For the first hour after the change the pilot host was still held
(112 samples), which proves nothing. The evening results below are encouraging but short. To judge
the fix over a working day with the controller's own log, run on the controller host:

```sh
journalctl -u woodpecker-load-controller@ci.0exec --since -26h --no-pager -o cat \
  | python3 bin/woodpecker_controller_hold_report.py --node <pilot node> --split 2026-10-19T20:13:52Z
```

and then the token-free pilot report from step 7. If the hold stays near 80 %, the controller's
resume rule (many calm samples in a row) is the next thing to look at, not the cache.

**Measured the same evening, under load (2026-10-19, 2.5 to 3 hours after the change):**

- *Released:* the controller put the pilot agent back in service 68 minutes after the change and
  held it 0 % of the next hour (mean I/O pressure 9 %). The same quiet night hours before the
  change were 100 % held at 29 to 79 % pressure.
- *Under full load:* nine workflows on a scratch branch, six pinned to the pilot host (cold build and
  test of a small public module, four rounds each) and one per other site, with all six slots busy.
  About 45 controller samples: I/O pressure 3 to 82 %, CPU load per core up to 4.9, the breach counter
  peaked at 6 (it needs 10 in a row, and resets on any calmer sample): **never held, no stop proposed.**
- *Per job:* a single run took 69, 79 and 71 s on the other sites. On the pilot host, five at once took
  143 to 262 s each (aggregate throughput only about 1.5 jobs' worth, so still disk-bound under load);
  the run that happened to have the host to itself took 68, 68, 95 and 72 s, the same as elsewhere.
  Two of 24 rounds failed their tests under load (`go build` printed nothing, so it was `go test`;
  the probe discarded the test output, so the failing test is unknown, and 28 later runs with the
  output kept all passed). Two fake-clock tests in `go-common/chromeguard` took 12 s and 4 s in single
  runs under load against 0.1 s normally, which points at disk stalls; other tests in that package wait
  up to 5 s, so they are the suspects. Not proven.
- *Real pipelines after the change* (token-free report, only 9 pilot runs): one comparable workflow
  pair, `large-files`: pilot host 81 s to 32 s while the control agents went 19 s to 21 s (effect 0.36).
  That workflow does no Go work, so it measures the disk fix, not the cache. No Go workflow has two
  runs after the change on the pilot host yet.

Limits: one evening, one load shape, no image builds or nightly backup in the window. The working-day
check above is still the real test.

### The first two working days (2026-10-20, 2026-10-21): better, not fixed

Held share of the pilot host over 07:00-16:00 UTC, from the controller's own log (OBSERVED):

| Day | Held | Note |
| --- | --- | --- |
| 10-19, before the change | 67 % | I/O pressure 25 % |
| 10-20 | 13 % | one held stretch, 07:51-09:09 UTC |
| 10-21 | 42 % | 985 samples, mean I/O pressure 26.5 % |
| 10-22 | 18 % | 1,002 samples, mean 21.1 %; backup-server schedule changed at about 07:50 UTC (below), one old-schedule verify job still ran 07:00-08:00 |
| 10-23 | 0 % | 1,005 samples, mean 14.5 %, no backup-server task inside the window |

Two cautions on reading this table. First, the nightly backup of the pilot VM itself became a weekly
Sunday backup on 10-19, the same day as the disk change (the backup job's own comment says so), so
part of the drop from 67 % may come from that and the data cannot separate the two. Second, three
days are still too few: the check is a held share of 15 % or less on three working days in a row, and
the backup-server schedule change below only started on 10-22.

So the disk change helped, but one good day was not a trend. What still holds the agent is the
backup server that shares the same two spinning disks (a VM on the same hypervisor, datastore on the
same pool). In a 20 s sample at 07:05 UTC on 2026-10-22 it read 18.5 MB/s, more than all other VMs
together; the build VM wrote 2.7 MB/s. Controller samples from 10-21 00:58 to 10-22 07:07 UTC (3,198),
split by whether a backup-server task (backup, verify, garbage collection or reader) was running:

| | Samples | Held | Mean I/O pressure |
| --- | --- | --- | --- |
| backup server busy | 1,571 | 66 % | 36 % |
| backup server idle | 1,627 | 22 % | 18 % |
| 07-16 UTC, busy | 629 | 63 % | 31 % |
| 07-16 UTC, idle | 370 | 7 % | 21 % |

Long held stretches end where those jobs end (10-21 11:30-12:41 against a verify job that ran
07:00-12:40; 22:18-23:40 against garbage collection 22:00-23:33). Inside working hours the backup
server runs a daily pull-sync to another site at 08:00 UTC (six readers of 10-45 minutes) and weekly
verify slices of 4.5 to 5.7 hours starting 07:00 or 10:00 UTC on Tuesday to Friday; a large VM backup
from another site ran 04:31-11:20 UTC on 10-20. This is a correlation of two logs, not an experiment:
the control is the idle row, where the same host in the same hours was held 7 % of the time.

The lasting remedies are on the storage side (a log device or a special vdev in that pool, or
moving verify, sync and the biggest backups out of 07-16 UTC); neither is part of this change.

**Applied on 2026-10-22 at about 07:50 UTC (by the owner of the backup servers, not by this change):**
the five weekly verify slices moved to the weekend and the five daily pull-syncs moved from 08:00 to
17:00 UTC; both servers were read back afterwards and show the new times. Backup-server task minutes
inside 07-16 UTC went from 464 (10-21) to 60 (10-22) and 0 (10-23). Nightly backups run in local time,
so after the clock change on Sunday 10-25 they run one hour later in UTC (a VM backup from another site on that
Sunday ended at 07:06 UTC, six minutes into the window). A check of 10-26 and 10-27 with the same
target decides; until all three days are in, this is not done.

Judge any of them by the number, on the controller host:

```sh
journalctl -u woodpecker-load-controller@ci.0exec --since -30h --no-pager -o cat \
  | python3 bin/woodpecker_controller_hold_report.py --node <pilot node> --daily-window 07-16
```

Target: held share at or under 15 % on three working days in a row. The controller's journal keeps
only about 30 hours, so save each day's figure.

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
