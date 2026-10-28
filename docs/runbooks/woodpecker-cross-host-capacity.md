# Woodpecker cross-host execution capacity

This runbook reproduces the deployed execution-plane design without storing
private addresses or credentials in Git. Each repository keeps one
authoritative Woodpecker control plane. Physical capacity is pooled by
registering stable agents from any approved site with that control plane; do
not move a repository between control planes just to choose a worker host.

## Deployed topology

As of 2026-10-14 `ci.0exec.com` has these schedulable agents
(`fleet-runner woodpecker queue --plane 0exec` shows online and schedulable
counts and capacity; the controller journal names the agents):

- `0mcp-docker-0exec-ag-from-72-r-20260929t184118` on VM 130 (one workflow
  slot and the VM 130 metrics group)
- `0own-build-agent` on 0own VM 610 (`0own-build-worker`, which also hosts a
  Forgejo Woodpecker server and agent, a Buildx builder and a mail server)
- `infer-builder-ci`
- a diagnostic registration on CT 108

`pve01-fleet-agent` (registration 100, capacity 2) was offline with a stale
heartbeat and a stopped builder VM on 2026-10-14. Registration 98 on CT 108 is
held by a manual `no_schedule`. `0docker-builder-agent-a`,
`0docker-builder-agent-b` and `0mcp-docker-exec-agent` survive only as stale
registrations.

The pve01 worker is a general fleet agent (`repo=*`, Docker backend); read its
`WOODPECKER_MAX_WORKFLOWS` from its Compose file once its VM is up. The 0own
VM 610 agent uses the general Docker backend with four workflow slots on its
10-vCPU, 32-GiB guest. It joins the
existing `ci.0exec.com` control plane; repositories do not need a new webhook
or a repo-specific builder configuration. Woodpecker assigns queued workflows
to matching agents with free workflow slots. Keep the concurrency cap at four
until host metrics and sustained resource behavior support a deliberate change.
`ci.0mcp.com` remains a separate authority for repositories intentionally
owned by the 0mcp platform (such as `mcp-site-service`). It is not a general
capacity selector. General `baditaflorin` repositories stay on their
configured authority, normally `ci.0exec.com`. The shared `ci.0exec` Docker
agents advertise `lv3=true`, so workflows requiring that capability are still
scheduled across the common pool. Keep the label in the workflow; adding the
capability to each eligible worker is the fleet-level change. The load
controller still drains pressured physical workers, and Woodpecker chooses an
eligible worker with a free slot. A worker label is a scheduling hint, not a
security boundary.

The VM 130 worker is shared infrastructure for two independent control
planes: this `ci.0exec.com` worker has one slot, while the separate
`ci.0mcp.com` worker has two. The latter's capacity is not visible to the
0exec scheduler; using it for general fleet CI requires connecting a worker to
the 0exec authority and verifying the host-level concurrency budget. Their
queues do not coordinate, so host metrics and the load controllers remain the
safety bound. The `ci.0exec` worker must be
grouped with VM 130 metrics at `127.0.0.1:19400`; the VM 120 runtime endpoint
at `127.0.0.1:19200` describes a different host. If the VM 130 metrics tunnel
fails, the `ci.0exec` agent is drained until metrics recover.

Worker attribution follows the running container's host, not names embedded
in old agent registrations. Registration 98 has a historical `pve01` name but
runs on CT 108; it belongs under `0docker`. As of 2026-10-14 it is held out of
scheduling by a manual `no_schedule` flag, which the controller does not manage
and does not tie to RAID state.

Only repositories with a Woodpecker workflow file can run a pipeline. A
workflow pinned to a site or specialized label can only use agents that match
that label. General workflows use `repo=*` Docker agents for automatic
cross-host scheduling. `site` and `lv3=true` are capability constraints within
one authority, not control-plane routing switches. Keep a repository on its
configured authority; provision the required capability across that shared
pool rather than moving the repository to a different CI server.

This pool executes repository CI workflows. Production image publication and
`fleet-runner deploy` still follow the fleet deployment contract and use the
canonical deployment builder until that separate path is explicitly migrated.

As of 2026-10-14 `ci.0mcp.com` has no usable `0exec-builder-mcp-agent`
capacity: its online agents are its own VM 130 build worker and its staging
runtime worker; `fleet-runner woodpecker queue --plane 0mcp` shows the live
pool. The
control-plane hostname never determines the physical execution host; use
`bin/ci_execution_report.py` for deterministic attribution.

## Install a cross-host agent

1. Copy `templates/woodpecker-cross-host-agent.compose.yml` to a root-owned
   directory on the execution host.
2. Create one Woodpecker agent registration per worker. The API returns a
   random token; store it in Fleet Secrets with the exact consumer
   `woodpecker-agent-<registration-id>`, then write the token to a separate
   root-owned file with mode `0600`. Do not place the server's
   `WOODPECKER_GRPC_SECRET` on an agent.
3. Copy `templates/woodpecker-agent-token.path.env.example` to the Compose
   `.env` file and set the token file path plus the registration ID. The file
   contains no credential.
4. Put the remaining non-secret values below in the Compose `.env` file or
   process environment:

   ```text
   WOODPECKER_SERVER=<private-proxy-address>:9000
   WOODPECKER_AGENT_NAME=<stable-physical-agent-name>
   WOODPECKER_AGENT_SITE=<physical-site-label>
   WOODPECKER_MAX_WORKFLOWS=1
   ```

   For a general remote fleet worker, use a stable `WOODPECKER_AGENT_NAME` and
   a non-sensitive `WOODPECKER_AGENT_SITE` label. Keep `repo=*` and
   `backend=docker` in its labels. The shared fleet template also includes
   `lv3=true` so labeled workflows can route across all eligible agents on the
   `ci.0exec.com` pool. Use the worker only for repositories authorized there.

5. Validate with `docker compose config --quiet`, start the agent and confirm
   its stable name in `GET /api/agents`.

Run a rotation from the 0exec builder with its root-only Woodpecker API token
file and the Fleet Secrets/API-key admin credentials loaded into the process
environment. Replace one online worker at a time, using its exact agent ID,
Compose file, and service name:

```sh
sudo fleet-runner woodpecker agent rotate <registration-id> \
  --compose-file /opt/woodpecker/<compose-file> \
  --service <compose-service> --worker-local
```

For a worker on another host, use `--worker-ssh-target <saved-ssh-target>` and,
when needed, `--worker-ssh-bastion <saved-ssh-bastion>` instead of
`--worker-local`. The command records global queue counts and checks the
selected agent's tasks and heartbeat, creates a no-schedule replacement, writes
the random token to its registration-specific Vault consumer, pauses only the
selected agent, rechecks its assignments, installs the token through stdin,
verifies an identity-matched heartbeat, enables the replacement, and deletes
the old registration. Pending and running jobs on other agents continue. It
refuses a globally paused queue, assigned work on the selected agent, or an
offline worker. It changes only `ci.0exec.com`; do not use it for
`ci.0mcp.com`.

The `fleet-runner woodpecker agent retire` and `resume` verbs are disabled.
Finish or retire a cutover only through the exact durable reconcile journal.

If the replacement heartbeat was not verified, repair the worker's token file
or Compose connection first.

The worker receives only `WOODPECKER_AGENT_SECRET_FILE`; the control-plane JWT
signing secret remains on the server. The helper never copies secret-bearing
Compose content. If Compose validation or restart fails inside the helper, it
restores the original bytes from process memory. If the worker restarted but
its new heartbeat cannot be verified, the replacement registration and Vault
token remain unschedulable so recovery can continue forward without reviving a
shared token.

Do not use Compose replicas for controller-managed agents. Replica container
IDs are unstable, so declare explicit services or separate agent stacks with
stable `WOODPECKER_HOSTNAME` values.

## Private transport across overlapping subnets

When sites reuse private address ranges, expose only the Woodpecker gRPC port
on the control-plane site's Tailscale address. Render and install
`woodpecker-private-proxy@.socket.tmpl` and
`woodpecker-private-proxy@.service.tmpl` on that site's bastion. Instantiate
port 9000 so the agent receives gRPC without exposing it publicly; the
controller reads each worker's node-exporter through a loopback SSH tunnel to
that worker's port 9100.

If the agent VM is not itself a Tailscale node, render
`woodpecker-private-route.sh.tmpl` on its bastion and install it with
`woodpecker-private-route.service`. The rule must contain
one exact agent source address, one exact Tailscale destination and only ports
9000/9100. Port 9000 is gRPC; port 9100 is node-exporter for the controller.

Never commit the rendered templates: they contain private routing coordinates.

## Run one controller per control plane

Install the controller executable, copy
`templates/woodpecker-load-controller@.service`, and create one JSON config and
root-managed token file per control plane. Do not enable an instance yet: the
unit template starts the controller with `--apply`, so an enabled instance
changes agent `no_schedule` flags straight away. Dry-run first, as described
below.

Use configuration version 2 to group one physical host with all of its
Woodpecker agent names. Agent names identify membership in a host group; the
controller tracks and updates every matching live agent ID. Names must be
unique across host groups. This avoids treating duplicate agent registrations
as one worker or counting several containers on the same host as separate
capacity.

Configure the 0exec pool as four physical node groups: `0docker-builder`,
`0mcp-docker-build`, `pve01-amd64-builder`, and `0own-build-worker`. Keep their
private metrics URLs and verified agent aliases in the live config at
`/etc/woodpecker-load-controller/ci.0exec.json`; do not put rendered private
addresses or tokens in Git. The current controller service is the
`woodpecker-load-controller@ci.0exec` instance. Keep the separate
`ci.0mcp.com` controller and queue independent. It does not change scheduling
on `ci.0exec.com`; each control plane can only drain agents registered to it.

Start with `--once` and without `--apply`. Confirm every configured agent name
appears, each host's metrics URL succeeds, and the host-level observations match
the physical topology. For a host reachable only through Tailscale userspace
networking, configure the controller service with an HTTP proxy and an explicit
`NO_PROXY` list for its Woodpecker API and directly reachable metrics endpoints.
After validating the dry run and checking the schedulable-host minimum in each
config (a minimum of 0 means no minimum-capacity guard), enable the supervised,
apply-mode instance of each control plane:

```bash
systemctl enable --now woodpecker-load-controller@ci.0exec
systemctl enable --now woodpecker-load-controller@ci.0mcp
```

The default policy samples every 30 seconds, drains after
ten overloaded samples, restores after twenty healthy samples, and keeps at
least one physical host schedulable.

### Gate guest-backed workers on hypervisor storage

Guest filesystem metrics do not describe the hypervisor filesystem that stores
their virtual disks. For each guest whose writes can exhaust shared host
storage, add an optional backing_storage object to that v2 node entry or v1
agent entry. It requires a separate node-exporter metrics URL, the exact
filesystem_mountpoint and filesystem_device labels, disk_stop_pct,
disk_resume_pct, and a positive minimum_free_bytes floor. See
templates/woodpecker-load-controller.example.json for a placeholder example.
The selector accepts exactly one matching available/size metric pair; absent,
ambiguous, malformed, or mismatched telemetry is treated as unsafe.

The backing filesystem is critical when its available percentage is at or below
the stop threshold OR its free bytes are at or below the floor. Either condition
immediately drains the configured v2 node group or v1 agent-name group, even
when doing so leaves fewer agents schedulable than the configured minimum.
Missing guest or backing metrics also immediately drains that gated group.
These urgent actions are limited to the explicitly configured group; normal
guest CPU, memory, and disk drains retain the minimum-capacity guard. Nodes or
agents without this gate keep the existing guest-only behavior.

Recovery requires the guest CPU, memory, and filesystem metrics to meet their
resume thresholds and the backing filesystem to meet both its resume percentage
and absolute free-byte floor for the complete configured resume_samples
window. The controller restores only registration IDs it recorded as drained.
An ID already held with no_schedule by an operator remains held. For v1,
duplicate live registrations with one configured name are evaluated as a
single group, but only controller-managed IDs are restored. Drain only stops
new scheduling; it does not terminate workflows already assigned to those
agents.

Choose mount/device labels and thresholds from the hypervisor's actual node
exporter output and expected peak write bursts. Run a dry-run and verify the
exact labels and observed free bytes before enabling apply. During rollback,
keep the gated agent group unschedulable until backing storage meets both
recovery thresholds; do not let an older guest-only controller restore it
while host storage is still low.

## End-to-end verification

1. Confirm the queue has no unrelated active workflows.
2. Stop the relevant controller briefly.
3. Set `no_schedule=true` only on local agents.
4. Rerun a safe test-only pipeline and verify its workflow `agent_id` resolves
   to the remote agent.
5. Restore the local agents in a `finally`/trap path and restart the controller.
6. On Builder LXC 108, run
   `ci-execution-report --config /etc/ci-execution-report/config.json --limit 100 --include-active`.
   The report classifies pipelines by physical host. Unknown agent IDs remain
   `unattributed`; do not assume they ran on 0docker.

The 2026-08-28 deployment proof reran `services-registry` pipeline 20 on
`0mcp-docker-exec-agent`; it passed. Both local agents were restored afterward.

## Rollback

Stop the new controller instance, set the remote agent to `no_schedule=true`,
and stop its Compose stack. Keep the repository webhook and authoritative
control plane unchanged. Remove the private proxy/NAT service only after the
agent has disconnected and no workflow is running there.

## Graded admission: slots follow headroom (a node's optional `graded` block)

By default the controller is on/off per host: it drains every agent of a host that stays
over a stop threshold and restores them together. That wastes the host in between (it
sat at 12% CPU with work pending because its slots were fixed) and overloads it at the
edge. A node with a `graded` block instead gets a **share of its slots**, 0 to 1, from its
live headroom, and opens or closes ONE agent per step to match it.

```json
{
  "name": "infer-builder-ci",
  "agent_names": ["infer-builder-ci", "infer-builder-ci-slot-2", "infer-builder-ci-slot-3"],
  "metrics_url": "http://<node-exporter>:9100/metrics",
  "graded": {"min_open_slots": 1, "load_per_cpu_full": 0.5, "memory_full_pct": 50,
             "open_samples": 6, "close_samples": 2}
}
```

- Each signal is linear between its *full* level (all slots open) and the node's stop
  threshold (none): normalized load (or CPU %), available memory %, and I/O pressure when the
  node has `io_pressure_*` limits. The scarcest signal decides; an unknown signal never
  limits (same rule as the hard drain). Defaults for the full levels are the resume
  thresholds.
- Slots are the agents' `capacity` (an agent with capacity 3 is 3 slots). Use several
  one-slot registrations on a host to get fine steps. Agents are used in `agent_names` order:
  the first listed closes last. Agents without a fresh heartbeat are not counted.
- `close_samples` consecutive cycles over share close the last open agent;
  `open_samples` consecutive cycles under it (and no hard breach) reopen the first agent the
  controller closed. At least `min_open_slots` stay open. Only agent IDs the controller closed
  are reopened, so an operator's `no_schedule` hold survives.
- The hard path is unchanged and wins: a sustained breach (memory, disk, I/O stop, load stop)
  still drains the whole host, and the recovery then reopens it one agent at a time instead of
  all at once. Running workflows are never killed.
- A node without `graded` behaves exactly as before. Log events: `node_graded`,
  `agent_graded_closed`, `agent_graded_opened`.

### Deployed with graded admission (2026-10-26)

`ci.0exec.com` now runs the controller with `graded` blocks on two hosts; their extra
agents are one-slot registrations that start paused (`no_schedule`) and are handed to the
controller by listing their IDs in the node's `managed_agent_ids` in the controller state file
while the controller is stopped (a paused agent the controller did not close is treated as an
operator hold and is never reopened).

| Node | Agents (open order) | Slots | Graded limits |
|---|---|---:|---|
| `infer-builder-ci` | `infer-builder-ci` (1 since 2026-10-28, was 3), `-slot-2`, `-slot-3`, `-slot-4` (agent IDs 109-111) | 4 | min 2 open, load full 0.7/CPU, memory full 40%; 5 GiB step cap on all four agents since 2026-10-28 |
| `0mcp-docker-build` (VM 130) | the registered 0exec agent (1), `0mcp-docker-0exec-slot-2`, `-slot-3` (IDs 114, 115) | 3 | min 1 open, load full 0.7/CPU, memory full 55% |
| `0own-build-worker` (VM 610) | `0own-build-agent` (99, now 1 slot), `0own-build-agent-slot-2`, `-3`, `-4` (IDs 116-118) | 4 | min 1 open, load full 0.7/CPU, memory full 50%, I/O full 8%; node I/O stop 45% / resume 25%; 5 GiB step cap on all four agents since 2026-10-28 |

The slot agents run from `/opt/woodpecker-agent-slots` (infer) and
`/opt/woodpecker-ci-0exec-slots` (VM 130), one compose project each, with their tokens in
`/opt/woodpecker-agent-tokens/agent-<id>.token` and in the vault as
`woodpecker_agent_<id>_token`. VM 130 slots keep the 5 GiB per-step memory cap. CT 108 runs no schedulable agent.

Traps met while rolling this out: the controller reaches the tailnet metrics only through the
`HTTP_PROXY` its unit sets, so a hand-run `--once` from a shell times out on `infer-builder-ci`;
a dry run (`--once` without `--apply`) never reaches the graded step, so it proves only that the config loads;
a `concurrency_limit` group on a workflow (the `domainscope-go-integration` group of
go-url-categorizer-api allows one at a time) leaves workflows pending for hours with free slots, which is the
workflow's own limit and not missing capacity.

0own was split on 2026-10-26 (agent 99 paused, drained of its 3 running jobs, recreated with
`WOODPECKER_MAX_WORKFLOWS=1`; its compose project is `own-woodpecker-agent`, so a bare
`docker compose -f ...` in `/opt/woodpecker-agent` starts a SECOND container with the same token
under project `woodpecker-agent`: always pass `-p own-woodpecker-agent`). Its two spinning disks run at 55-67% I/O
pressure from the guests alone (VMs 610, 660, 630), so with the 45% stop limit its slots stay closed
until the disks calm down; that is the intended behaviour, and it shows 0own is not a CI baseline host.

## Memory budget per builder (measured 2026-10-28)

Slots are not the only thing that limits a builder: its RAM is. Every CI builder VM has about 12 GiB
(`0mcp-builder` 11.9, `0own-build-worker` 11.4, `infer-builder-ci` 11.9, CT 108 11) with 20, 10, 30 and 8 cores, so
cores say nothing about how many builds fit.

**What a build costs.** From the build sampler over 24 h: the median build peaks at 0.4 to 0.6 GiB, the 95th percentile at
4.5 GiB, about 90 builds a day go above 4 GiB, about 15 above 5 GiB and 2 above 6 GiB, nearly all of them in
`go-url-categorizer-api` and `hub_scrapetheworld_org`. One `node` step grew to 10 GB (killed 34 times on infer and 13 on 0own in
24 h). A few heavy builds at once exhaust a VM, and a whole-VM OOM is far worse than the build it kills: load 265, ssh and
node_exporter time out, the controller's `fail_closed_on_metrics_error` drains the host, and innocent steps die with it.

**What happened on 2026-10-28.** The main agent of `infer-builder-ci` was raised from 3 to 7 workflows at 01:24Z (the backup
`compose.yaml.bak-pre-cap7-20261028T0124Z` on the VM), most likely to work off a backlog. Kernel OOM kills there went from 2 in
the six hours before to about 35 a day (37 whole-VM kills in 24 h; 0mcp-builder 24 whole-VM plus 31 kills of containers that hit
their own 5 GiB cap; 0own-build-worker 14; CT 108 none), up to 12 builds ran at once, and the queue grew from 46 to 154
pending: kills mean retries, retries mean a longer queue. A 7-slot agent is also a coarse unit for the graded controller:
`min_open_slots: 2` is met by opening the whole 7-slot agent, so seven builds arrive together. The slot agents crash-looped
(exit 137, about once a minute, 100+ restarts) and every crash left a workflow as `running` in the server database
(agent 109, capacity 1, showed 7; agent 101 had 50 rows 12 days old), so **the `running` count in the database is not load**:
count the step containers on the VM (`docker ps --filter name=wp_`).

**The rule.**

1. One-slot agents, and at most **4 builds at once per 12 GiB VM counting every agent on it**, whichever Woodpecker server it
   belongs to (the 0mcp VM also runs the ci.0mcp agent; the 0own VM also runs a Forgejo agent).
2. Every agent sets `WOODPECKER_BACKEND_DOCKER_LIMIT_MEM` and `WOODPECKER_BACKEND_DOCKER_LIMIT_MEM_SWAP` to `5368709120`
   (5 GiB, the cap VM 130 already had), so a runaway step dies alone and fast instead of thrashing the VM.
3. Do not raise `WOODPECKER_MAX_WORKFLOWS` to chew a backlog: capacity beyond the budget makes OOM kills, which make retries.
4. Watch the OOM panels on CI Operations (`node_vmstat_oom_kill{role="ci-builder"}`) and tell the two kinds apart in the VM's
   kernel log: `CONSTRAINT_NONE` is the VM running out of memory, `CONSTRAINT_MEMCG` is a capped container.

Why 4 and 5 GiB, as a model rather than a measurement (about one build in ten is heavy at about 5 GiB, the rest about
0.5 GiB, independent): the chance that two or more heavy builds run at once is 1% with 2 busy slots, 2.8% with 3, 5.2% with 4,
11% with 6 and 34% with 12, and two heavy builds plus the VM's own 1.5 to 2 GiB already fill 12 GiB. With 4 slots and the cap a
whole-VM OOM needs three heavy builds at once (0.4% when all four slots are busy); without the cap a single runaway is enough.

**Changing a builder's slots or cap (worked on infer: about 22 minutes from drain to reopen, almost all of it waiting for builds to finish).** Drain the open agents of that VM through
the API (`PATCH /api/agents/<id>` with `{"no_schedule": true}`, the controller's own token; the controller never reopens an
agent it did not close, so reopen them yourself afterwards); wait until the VM's step containers are down to its own leftovers;
back up the compose file (`compose.yaml.bak-pre-<change>-<UTC>`), edit it, and recreate with the project the container really
belongs to (read it from the container's `com.docker.compose.project*` labels: a bare `docker compose` in the wrong directory
starts a second container with the same token). Projects: infer `infer-builder-ci` (`/opt/woodpecker-agent`) and
`woodpecker-slots` (`/opt/woodpecker-agent-slots`); 0own `own-woodpecker-agent` (`compose.yml` and
`woodpecker-agent-ci-cache.override.yml`) and `woodpecker-0own-slots`; 0mcp `woodpecker-0exec-agent`
(`/opt/woodpecker-ci-0exec-agent`), `woodpecker-0exec-slots` (`/opt/woodpecker-ci-0exec-slots`) and the ci.0mcp agent
`woodpecker` (`/opt/woodpecker`). Check the environment of each agent, exactly one container per agent, restarts staying 0 and
the kernel's `oom_kill` counter (`/proc/vmstat`) not moving. Result on infer: the crash loop stopped (restarts 0/0/0/0 over four
minutes), load fell from 22 to 6, `MemAvailable` held at about 9 GiB, new step containers carry the 5 GiB limit, no OOM kill.
Then 0own, the cap only (every agent there was already one slot): 8 minutes, every step container carried the limit, available
memory dipped to 4.5 GiB under a heavy build without an OOM kill, restarts 0, `oom_kill` delta 0 over four minutes. That is a
short window: the OOM panels on CI Operations show whether it holds over a day.

**Not done yet.** The 0mcp VM (the only builder without this change) still runs five builds at once (3 ci.0exec slots plus the ci.0mcp agent with
`WOODPECKER_MAX_WORKFLOWS=2`, which has no cap): give that agent one slot and the cap. Do it through the platform, not by hand on
the VM: its compose (`/opt/woodpecker/docker-compose.yml`, project `woodpecker`) takes the setting from
`/run/0mpc-secrets/woodpecker/runtime.env`, which OpenBao renders from `woodpecker.env.ctmpl.j2` of the `woodpecker_runtime`
role in `proxmox_florin_server` (`WOODPECKER_MAX_WORKFLOWS={{ woodpecker_max_workflows }}`). The role default is 1 and the live
agent runs 2, so the value is already set somewhere outside the default, and a hand edit would be reverted by the next converge.
Add `WOODPECKER_BACKEND_DOCKER_LIMIT_MEM` and `WOODPECKER_BACKEND_DOCKER_LIMIT_MEM_SWAP` to that template and set the variable to 1. Routing the heavy repositories to
dedicated agents by label (a workflow that asks for `memory=heavy` runs only on agents that carry it, so one heavy build per VM)
needs a `labels:` entry in those two repositories' pipelines. More RAM per builder is the other lever; the 0infer host has
about 6 GiB left.
