# ADR-0040 — One CI authority per repository, many load-aware agents

* **Status**: Accepted and deployed
* **Date**: 2026-08-28
* **Authors**: baditaflorin + Codex
* **Tags**: ci, woodpecker, scheduling, observability, multi-host

## Context

The fleet now operates two independent Woodpecker servers. A repository was
activated on both instances, so every GitHub event produced two pipelines and
both servers wrote the same default GitHub status context. A later failure on
the duplicate instance overwrote the successful authoritative result.

Independent Woodpecker servers are separate control planes: each has its own
OAuth identity, webhook, database, repository IDs, pipeline history and
scheduler. Sending a webhook to whichever server currently has the most free
CPU would split durable state and make status reporting nondeterministic. It
also makes a transient resource probe part of the event-delivery correctness
path.

Woodpecker already separates the control plane (`woodpecker-server`) from the
execution plane (`woodpecker-agent`). Capacity therefore scales by connecting
more agents to one server, not by activating a repository on more servers.

## Decision

1. Every repository has exactly one authoritative Woodpecker hostname. The
   mapping lives in `ci-authorities.json`: `ci.0exec.com` is the owner default,
   with explicit per-repository exceptions such as `mcp-site-service` on
   `ci.0mcp.com`.
2. `bin/ci_authority_audit.py` reads GitHub's repository-hook API and fails
   unless exactly one known CI hook is active and its hostname matches the
   registry. It never prints full hook URLs because those URLs contain signed
   repository tokens.
3. Additional hosts join an existing control plane as Woodpecker agents over a
   private network. Agents have stable names, bounded
   `WOODPECKER_MAX_WORKFLOWS` values and labels for intentional placement.
4. Woodpecker's free workflow slots remain the primary scheduler. CPU, memory
   and disk pressure are an admission-control layer, not a replacement
   scheduler.
5. `bin/woodpecker_load_controller.py` scrapes node-exporter and uses the
   Woodpecker agent API to set `no_schedule`. It is dry-run by default, requires
   sustained threshold breaches before draining, requires a longer healthy
   window before restoring, drains the most pressured candidates first, always
   leaves a configurable minimum capacity, and only restores agents it drained
   itself.
6. Missing metrics never cause an automatic drain. Telemetry loss is reported
   but scheduling state is left unchanged.
7. Control-plane API tokens, agent tokens, metrics addresses and private routes
   remain in the private secret/operations layer. The public repository contains
   only generic templates and public CI hostnames.

## Default pressure policy

The initial controller interval is 30 seconds:

| Signal | Drain after 10 samples (5 min) | Restore after 20 samples (10 min) |
|---|---:|---:|
| CPU utilization | `>= 85%` | `<= 60%` |
| Memory available | `<= 15%` | `>= 30%` |
| Root filesystem available | `<= 10%` | `>= 15%` |

The asymmetric thresholds and sample windows prevent a host near a boundary
from repeatedly joining and leaving the scheduler. Running workflows are never
killed; `no_schedule` only controls admission of new work.

## Deployment verification

On 2026-08-28, `ci.0mcp.com` registered the remote
`0exec-builder-mcp-agent` alongside its two existing agents. A real rerun of
the `mcp-site-service` test pipeline completed successfully on that remote
agent. Reciprocally, `ci.0exec.com` registered
`0mcp-docker-exec-agent` alongside stable local agents
`0docker-builder-agent-a` and `0docker-builder-agent-b`. Pipeline 20 for
`services-registry` completed successfully on the remote 0mcp agent while both
local agents were drained for the placement test; the local agents were then
restored and the capacity controller resumed management of all three.

Both controller instances observed sustained CPU pressure across their pools,
kept the configured minimum of one agent eligible, and left running workflows
untouched. The authority audit independently confirmed that `mcp-site-service`
has exactly one active known CI webhook, on `ci.0mcp.com`.

## Operational checks

Audit one repository:

```bash
GH_TOKEN="$(gh auth token)" \
  python3 bin/ci_authority_audit.py \
    --repo baditaflorin/mcp-site-service
```

Audit every repository owned by the configured GitHub account:

```bash
GH_TOKEN="$(gh auth token)" \
  python3 bin/ci_authority_audit.py --all baditaflorin --json
```

Attribute the 100 most recent completed gates across both control planes to
their physical execution hosts:

```bash
python3 bin/ci_execution_report.py --limit 100
```

The reporter resolves pipeline workflows to Woodpecker agent IDs and then uses
the explicit host mapping in `ci-execution-report.json`; it does not infer the
execution server from the webhook/control-plane hostname. Historical agent IDs
that Woodpecker has deleted from its live agent API remain explicitly mapped in
that file so old gates do not silently become unattributed.

Run one load-controller observation without changing scheduling:

```bash
python3 bin/woodpecker_load_controller.py \
    --config /etc/woodpecker-load-controller/config.json \
    --state /var/lib/woodpecker-load-controller/state.json \
    --token-file /etc/woodpecker-load-controller/token \
    --once
```

Add `--apply` only on the continuously supervised deployment after its dry-run
output has been validated against the Woodpecker UI and node-exporter.

## Consequences

**Positive:**

- GitHub receives one deterministic status per workflow.
- Pipeline history and secrets remain attached to one control plane.
- Capacity can grow across physical servers without adding GitHub webhooks.
- Overloaded hosts stop accepting new work while healthy agents continue.
- A public, reviewable registry makes accidental duplicate activation
  detectable across the fleet.

**Negative:**

- The authoritative server remains a control-plane dependency; database backup
  and recovery are still required.
- Cross-host agents require a reliable private gRPC route and careful secret
  distribution.
- `no_schedule` is admission control, not CPU-aware bin packing. Static workflow
  capacity must still be sized conservatively.
- The controller needs an administrative Woodpecker API token. Compromise of
  that token can drain agents, so it must be stored in the private vault and
  scoped/rotated independently.

## Alternatives considered

1. **DNS or HTTP load-balancing across Woodpecker servers.** Rejected because
   independent databases and schedulers would split pipeline state.
2. **Multiple active repository webhooks with distinct status contexts.** Useful
   for intentional redundant validation, but doubles work and does not pool
   capacity. It is not load balancing.
3. **Route each webhook after querying host CPU/RAM.** Rejected because the
   decision is stale immediately and webhook delivery must not depend on a
   resource-monitoring control loop.
4. **Kubernetes scheduler.** A valid future execution backend, but unnecessary
   for two Docker builders and much larger operational surface than bounded
   Woodpecker agents.

## References

- [ADR-0035](0035-self-hosted-ci-on-builder-lxc.md) — original Woodpecker fleet rollout.
- `ci-authorities.json` — repository-to-control-plane policy.
- `templates/woodpecker-load-controller.example.json` — public-safe controller example.
- Woodpecker agent API: `GET/PATCH /api/agents/{id}` and `GET /api/queue/info`.
