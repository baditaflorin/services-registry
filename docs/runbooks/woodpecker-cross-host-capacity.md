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
An ID already held with no_schedule by an operator remains held. The
controller records an ID, and writes its state file, before it sends the
pause, so a pause whose response is lost (a read timeout after Woodpecker
applied it) is still restored, and a pause that never landed is forgotten on
the next cycle because the agent is still schedulable. Such a request logs
`agent_drain_unconfirmed` with the agent ID. For v1,
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
