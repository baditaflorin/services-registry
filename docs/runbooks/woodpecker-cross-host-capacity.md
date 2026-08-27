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

`ci.0mcp.com` also has `0exec-builder-mcp-agent` as remote capacity. The
control-plane hostname never determines the physical execution host; use
`bin/ci_execution_report.py` for deterministic attribution.

## Install a cross-host agent

1. Copy `templates/woodpecker-cross-host-agent.compose.yml` to a root-owned
   directory on the execution host.
2. Copy `templates/woodpecker-agent-secret.env.example` to
   `.agent-secret.env`, populate it from the private secret store and set mode
   `0600`.
3. Put the non-secret values below in the Compose `.env` file or process
   environment:

   ```text
   WOODPECKER_SERVER=<private-proxy-address>:9000
   WOODPECKER_AGENT_NAME=<stable-physical-agent-name>
   WOODPECKER_AGENT_SITE=<physical-site-label>
   WOODPECKER_MAX_WORKFLOWS=1
   ```

4. Validate with `docker compose config --quiet`, start the agent and confirm
   its stable name in `GET /api/agents`.

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

Start with `--once` and without `--apply`. After validating every metrics URL
and agent name, use the supervised service. The deployed policy samples every
30 seconds, drains after ten overloaded samples, restores after twenty healthy
samples, and keeps at least one agent schedulable.

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
