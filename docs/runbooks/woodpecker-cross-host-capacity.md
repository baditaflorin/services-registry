# Woodpecker cross-host execution capacity

This runbook reproduces the deployed execution-plane design without storing
private addresses or credentials in Git. Each repository keeps one
authoritative Woodpecker control plane. Physical capacity is pooled by
registering stable agents from both sites with that control plane.

## Deployed topology

`ci.0exec.com` schedules across these stable agent identities:

- `0docker-builder-agent-a`
- `0docker-builder-agent-b`
- `0mcp-docker-exec-agent`
- `pve01-fleet-agent` on a dedicated remote builder VM
- `0own-build-agent` on dedicated 0own VM 610

The pve01 worker is a general fleet agent (`repo=*`, Docker backend) with
`WOODPECKER_MAX_WORKFLOWS=1`. The 0own VM 610 agent also uses the general
Docker backend and currently has two workflow slots. Both join the existing
`ci.0exec.com` control plane; repositories do not need a new webhook or a
repo-specific builder configuration. Woodpecker assigns queued workflows to
matching agents with free workflow slots. The 0own slot count should stay at
two until its host metrics are included in the load controller.
`ci.0mcp.com` remains a separate authority for its `lv3=true` pipelines; do
not register those repositories on the 0exec pool.

This pool executes repository CI workflows. Production image publication and
`fleet-runner deploy` still follow the fleet deployment contract and use the
canonical deployment builder until that separate path is explicitly migrated.

`ci.0mcp.com` also has `0exec-builder-mcp-agent` as remote capacity. The
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
   `backend=docker` in its labels. Use the worker only for repositories
   authorized on `ci.0exec.com`.

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

If a successful worker cutover leaves the old registration unschedulable because
the API delete was unavailable, retry its cleanup with
`sudo fleet-runner woodpecker agent retire <registration-id>` after its
heartbeat is stale and it has no assigned tasks.

If the replacement heartbeat was not verified, repair the worker's token file
or Compose connection first. Once its identity-matched heartbeat is fresh, the
source worker is offline and idle, and the global queue is not paused, finish
the cutover with
`sudo fleet-runner woodpecker agent resume <replacement-registration-id>`.

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
ports 9000 and 9100 so the agent receives gRPC and the remote controller can
read node-exporter without exposing either port publicly.

If the agent VM is not itself a Tailscale node, render
`woodpecker-private-route.sh.tmpl` on its bastion and install it with
`woodpecker-private-route.service`. The rule must contain
one exact agent source address, one exact Tailscale destination and only ports
9000/9100. Port 9000 is gRPC; port 9100 is node-exporter for the controller.

Never commit the rendered templates: they contain private routing coordinates.

## Run one controller per control plane

Install the controller executable, copy
`templates/woodpecker-load-controller@.service`, and create one JSON config and
root-managed token file per control plane:

```bash
systemctl enable --now woodpecker-load-controller@ci.0exec
systemctl enable --now woodpecker-load-controller@ci.0mcp
```

Use configuration version 2 to group one physical host with all of its
Woodpecker agent names. Agent names identify membership in a host group; the
controller tracks and updates every matching live agent ID. Names must be
unique across host groups. This avoids treating duplicate agent registrations
as one worker or counting several containers on the same host as separate
capacity.

Start with `--once` and without `--apply`. Confirm every configured agent name
appears, each host's metrics URL succeeds, and the host-level observations match
the physical topology. For a host reachable only through Tailscale userspace
networking, configure the controller service with an HTTP proxy and an explicit
`NO_PROXY` list for its Woodpecker API and directly reachable metrics endpoints.
After validating the dry run and checking the schedulable-host minimum, use the
supervised service. The default policy samples every 30 seconds, drains after
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
6. Run `python3 bin/ci_execution_report.py --limit 100`.

The 2026-08-28 deployment proof reran `services-registry` pipeline 20 on
`0mcp-docker-exec-agent`; it passed. Both local agents were restored afterward.

## Rollback

Stop the new controller instance, set the remote agent to `no_schedule=true`,
and stop its Compose stack. Keep the repository webhook and authoritative
control plane unchanged. Remove the private proxy/NAT service only after the
agent has disconnected and no workflow is running there.
