# fleet-edge-probe

Strict external synthetic prober for every fleet service's **public edge**
(DNS → TLS → redirects → body), driven by `services.json`. Built after the
2026-09-09 cookie-checker outage, where a whole batch of Coolify-hosted
services served the wrong TLS cert / redirected cross-domain to
`nginx.0mcp.com/health` (→ HTTP 200) and stayed undetected for days because
the existing checks follow redirects, only assert `200`, and
`fleet-runner smoke` can run with `-insecure`.

## What it asserts (per `kind: container` service with a public `https://` url)

| code | meaning |
|---|---|
| `dns_nxdomain` | hostname does not resolve |
| `gateway_drift` *(warn)* | A record outside the accepted public edge set for the service: the built-in per-runtime default (compose → 176.9.123.221, coolify → 65.108.75.123), extended by the [edges data file](#accepted-edges-and-vantages) and by `EDGE_PROBE_GATEWAY_EXTRA` |
| `tls_untrusted` | TLS chain does not validate against the system trust store (no `-k`) |
| `cert_host_mismatch` | leaf cert SAN does not cover this hostname |
| `cert_registry_mismatch` | SAN does not match registry `cert_domain` (`wildcard.0crawl.com` ⇒ SAN must include `*.0crawl.com`) |
| `cert_expiring` *(warn)* | cert `notAfter` < now + `--expiry-days` (default 14) |
| `offsite_redirect` | health check ended on a different host than requested |
| `health_status` | health check returned `000` / `5xx` (real outage). Uses registry `health_url`, else `/health` |
| `health_forbidden` *(warn)* | health check returned `401`/`403` — edge up, endpoint gated (infra services) |
| `health_notfound` *(warn)* | health check returned `404` — wrong path / missing `health_url` in registry |
| `health_body` | `2xx` but body is not JSON `status` ok/healthy/up/success (case-insensitive), `{"ok": true}`, a `service`/`version` key, nor a plain `ok`/`healthy` |
| `fallback_vhost` | body matches a known default-vhost fingerprint (`<title>LV3`, `nginx.0mcp.com`, …) |
| `example_unreachable` *(warn)* | `GET <url><example_path>` ∉ {200,401,403} or landed off-host — usually a stale `example_path` in the registry |

Services with registry `lifecycle.status: sunset` are **not probed**: they are listed as `SKIP`, counted in `skipped`, and exported as `fleet_edge_probe_service_skipped{lifecycle="sunset"} 1` plus `fleet_edge_probe_skipped_total` so they stay visible but never alert.

Exit `0` = all probed pass · `1` = ≥1 hard fail · `2` = usage/registry-fetch/edges-config error · `3` = **nothing eligible to probe** (see below).
Warn-severity codes do not affect exit code.

### Nothing eligible is a failure, never a quiet success

If the filters leave zero services to probe (for example `EDGE_PROBE_RUNTIME=coolify` after the last Coolify service moved to compose: from 2026-10-17 15:24Z the builder's unit probed nothing for 17+ hours and nothing alerted), the probe:

- exits **3** and says on stderr which filters ran and how many services each excluded
  (`of 533 registry entries, 0 matched …; excluded by kind != container: 139; by runtime external (never probed): 7; … by runtime not in filter: 387 …; requested runtimes in this registry: coolify=0; registry runtimes: compose=387, …`);
- writes `fleet_edge_probe_eligible_services{vantage=…} 0` (and `fleet_edge_probe_up 0`) to the `--prom` textfile **before** exiting. Every normal run writes the real count, so the metric always says how many services this vantage covers. The rule `FleetEdgeProbeNothingEligible` (in `fleet-edge-probe.deploy/prom-rule-fleet-edge-probe.yml`) pages on it;
- with `--state` and `--ntfy`, sends **one** ntfy alert when the run enters that state (the state file remembers `nothing_eligible`), nothing on the following runs, and one "probes N services again" notice when it recovers. Without `--state` there is no alert (same rule as the other transitions); the metric and the exit code still stand;
- with `--json`, prints the report with `"nothing_eligible": true` and the same breakdown under `detail`.

A selection that only matches `lifecycle.status: sunset` services counts as nothing eligible too (they are skipped, so nothing is probed). A runtime named in `--runtime` that the registry has no service for logs `warning: --runtime X matches 0 services` on every run, even when other runtimes still match.

## Run

```bash
# whole fleet, human output (failures + summary only)
bin/fleet-edge-probe.py

# everything, incl. passing rows
bin/fleet-edge-probe.py --all

# one runtime / a comma list / every probeable runtime (`all`, same as no filter)
bin/fleet-edge-probe.py --runtime compose
bin/fleet-edge-probe.py --runtime compose,coolify
bin/fleet-edge-probe.py --runtime all

# a few slugs / a quick sample
bin/fleet-edge-probe.py --only cookie-checker,asn-lookup
bin/fleet-edge-probe.py --sample 40

# machine output
bin/fleet-edge-probe.py --json > /var/lib/fleet-edge-probe/last.json
```

`--services` defaults to a local `services.json` next to the script, else
`raw.githubusercontent.com/.../main/services.json`.

## Schedule it (the point — external vantage, every few minutes)

Run from a host **outside both fleet LANs** so it exercises real public DNS +
TLS + edge (a small VPS in a third DC, or a GitHub Actions cron). Example
cron with alert-on-transition:

```cron
*/10 * * * *  /opt/services-registry/bin/fleet-edge-probe.py \
  --services /opt/services-registry/services.json \
  --state /var/lib/fleet-edge-probe/state.json \
  --ntfy https://ntfy.0mcp.com/fleet-edge \
  --json >> /var/log/fleet-edge-probe.jsonl 2>>/var/log/fleet-edge-probe.err
```

`--state` diffs against the previous run; `--ntfy` (or `EDGE_PROBE_NTFY`)
POSTs only **newly-broken** and **recovered** services (and the one-per-transition
nothing-eligible notices above), so a persistent failure pages once, not every 10 minutes.
The state file is written atomically.

Ship `/var/log/fleet-edge-probe.jsonl` into OpenObserve (stream
`fleet_edge_probe`) for history / dashboards, same as `docker_logs`.

## Accepted edges and vantages

A service is probed through its public hostname, and `gateway_drift` (a **warn**) says the
A record the probing host resolves is not an edge address we expect. "The" expected address
is wrong by design once services run in several places and the probe runs from several
vantages, so the accepted set is **layered**; every layer only adds addresses, and an A
record in the union passes:

| layer | where | what it holds |
|---|---|---|
| 1 built-in | `GATEWAY_IPS` in `fleet-edge-probe.py` | `compose` → the 0docker gateway (`176.9.123.221`, the nginx `wildcard.*` terminator), `coolify` → the 0mcp edge (`65.108.75.123`, `nginx-lv3` → Coolify Traefik) |
| 2 data file | `bin/fleet-edge-probe.edges.json` (`--edges PATH` or `EDGE_PROBE_EDGES`; the shipped file is picked up automatically when the script runs from a checkout) | further **public** edges, for every vantage: a second edge on the 0infer side, the 0mcp edge for compose, … |
| 3 per vantage | `EDGE_PROBE_GATEWAY_EXTRA` (or `--gateway-extra`), comma-separated IPv4 | the **internal split-horizon address** an internal vantage resolves the fleet hostnames to |

A service is checked only if its `runtime` (or `mesh`) has a base set in layers 1-2; layer 3 and `any` widen
that set but never switch the check on for a runtime nobody described (`external` stays unchecked).
An unparsable edges file, a typo'd key or an invalid address is **exit 2**, never a silent fallback to
the built-in default (a hidden typo would otherwise mean a missing edge). The shipped file is validated by the unit tests.

### `fleet-edge-probe.edges.json` schema (version 1)

```json
{
  "schema": 1,
  "description": "free text, ignored",
  "runtime": { "compose": ["203.0.113.7"] },
  "mesh":    { "0crawl":  ["203.0.113.8"] },
  "any":     ["203.0.113.9"]
}
```

- `schema` (required) must be `1`. Allowed keys are exactly `schema`, `description`, `runtime`, `mesh`, `any`.
- `runtime`: registry `runtime` → list of IPv4 addresses accepted for those services (adds to the built-in set for that runtime).
- `mesh`: registry `mesh` → list of IPv4 addresses accepted for services of that mesh (adds to, and for a runtime with no set, enables the check).
- `any`: IPv4 addresses accepted for every checked service (an edge that fronts everything).
- Only IPv4 (the probe compares A records), no CIDR. The shipped file records no extra edge yet: add one only once its address is known.

### Which vantage probes what

| vantage | what its resolver answers | `EDGE_PROBE_RUNTIME` | `EDGE_PROBE_GATEWAY_EXTRA` |
|---|---|---|---|
| **outside** both fleet LANs (a VPS in a third DC, `monitoring-lv3` on 0mcp) | the public A records | `all` (or the runtimes it can reach) | not set |
| **internal** to a fleet LAN (Builder LXC 108 on the 0docker LAN) | split-horizon: the fleet hostnames answer the internal gateway address, not the public one | `all` or `compose` | the internal gateway address (`10.10.10.10` on the 0docker LAN, see CLAUDE.md) |

An internal vantage proves the service answers through the gateway over the internal path (cert chain, SAN,
redirects, `/health` body); it does **not** prove public DNS or the public edge address, so keep at least one
outside vantage. Registry state on 2026-10-18 (`services.json`, 533 entries): 387 `kind: container` /
`runtime: compose`, 139 `static` / `github-pages` (not probed here, see `fleet-runner pages-audit`), 7
`container` / `external` (all `lan-internal` over `http://`, never probed), **0 `coolify`**: `all` and
`compose` are the same set today. Set `EDGE_PROBE_RUNTIME` to `all` (or leave it empty) so a runtime that
appears later is covered without editing every vantage.

`external` entries are never probed (third-party containers outside the fleet, no public edge); naming them in a
comma list is harmless and adds nothing. Entries whose `network_exposure` is `lan-internal` or `loopback` (the
schema spelling; the script used to compare against the unmatched `internal`) are skipped for the same reason.

## Deployed (2026-09-09) — two vantages, one per fleet

Each fleet's edge is only truly *external* from the **other** fleet's network
(a box inside a fleet hairpin-NATs to its own edge IP → `ECONNREFUSED`). So
the probe runs on one box per fleet, each filtered to the services the *other*
fleet hosts:

| vantage host | `EDGE_PROBE_RUNTIME` | covers | schedule | alert path |
|---|---|---|---|---|
| `monitoring-lv3` (0mcp) | `compose` | 0docker-hosted services (→ `176.9.123.221`) | `*:02/15` | node_exporter textfile → Prometheus job `fleet-edge-probe` → rules in `/etc/prometheus/rules/fleet-edge-probe.yml` → Alertmanager `ntfy-critical` + `mattermost-critical` |
| Builder LXC 108 (0docker) | `coolify` *(stale since 2026-10-17: the registry has no coolify service, so this probes nothing and exits 3; target config above)* | 0mcp-hosted services (→ `65.108.75.123`) | `*:09/15` | textfile (`/var/lib/prometheus-node-exporter/textfile/`) **+** `fleet-edge-probe-ship-oo` → OpenObserve stream `fleet_edge_probe` on 0own (`https://openobserve.0own.com`) |

Artifacts in `bin/fleet-edge-probe.deploy/`:

- `fleet-edge-probe.service` / `.timer` — systemd oneshot + timer. Config via
  `/etc/default/fleet-edge-probe` (`EDGE_PROBE_SRC_URL`, `EDGE_PROBE_PROM`,
  `EDGE_PROBE_VANTAGE`, `EDGE_PROBE_RUNTIME`, `EDGE_PROBE_NTFY`, and on the
  0docker box `EDGE_PROBE_OO_URL` / `EDGE_PROBE_OO_AUTH`).
  An optional `ExecStartPre` keeps the edges data file current like `services.json`:
  set `EDGE_PROBE_EDGES_URL` (raw GitHub URL of `bin/fleet-edge-probe.edges.json`) and
  `EDGE_PROBE_EDGES=/var/lib/fleet-edge-probe/edges.json`; unset, it does nothing.
- `prom-rule-fleet-edge-probe.yml` — 4 alert rules: `FleetServiceEdgeDown`
  (per-service hard fail, `for: 15m`, critical), `FleetEdgeManyDown` (≥5 at
  once → batch cert/DNS/vhost failure, critical), `FleetEdgeProbeStale`
  (no run in >1h, warning), `FleetEdgeProbeNothingEligible` (a vantage whose
  filters match zero services, `for: 10m`, critical).
- `fleet-edge-probe-ship-oo` — pushes each run's `state.json` (summary +
  one row per failing service) into OpenObserve for the 0docker vantage,
  where there is no local Prometheus.

### OpenObserve alert (0docker vantage) — live

Originally created 2026-09-09 on the former 0docker OpenObserve (`v0.14.7`) via
`POST /api/v2/default/alerts`:

- **name:** `fleet_edge_hard_fail`  (folder `default`, scheduled)
- **stream:** `fleet_edge_probe`
- **condition (SQL):** `SELECT vantage, hard_fail_total FROM fleet_edge_probe WHERE hard_fail_total > 0`
  — only `kind:"summary"` rows carry `hard_fail_total`, so this matches
  a run that found ≥1 hard failure without needing a string literal
  (OO normalises quotes out of stored SQL).
- **trigger:** period 20m, `>= 1` matching row, evaluated every 15m,
  `silence` 120m.
- **destination:** `fleet_email` → admin@0docker.com (template `fleet_email_default`).

The old CT 106 is absent. Recover this alert from its PBS snapshot and verify
it against the 0own instance before relying on it there.

Test-fired by ingesting a synthetic `{kind:"summary",vantage:"test-fire",hard_fail_total:7}`
row and briefly setting frequency=1m: OO logged `Alert conditions satisfied`
→ `Alert notification sent`, `last_satisfied_at` populated. Config then
restored to 15m/120m.

Drill-down when it fires:
`SELECT slug,host,fails,cert_err FROM fleet_edge_probe WHERE kind != 'summary' ORDER BY _timestamp DESC`

## Dashboard

`bin/fleet-edge-probe.deploy/grafana-dashboard.json` — provisioned on
`monitoring-lv3` by dropping it in `/etc/grafana/dashboards/` (the
`node-exporter` file provider there auto-loads it, 30s). uid `fleet-edge-probe`,
datasource uid `0mpc-prometheus`. Shows the **compose** vantage (this
Prometheus only has that one). For the coolify vantage see
`openobserve-dashboard-panels.md`.

See also [fleet-selftest-poll](fleet-selftest-poll.README.md): same plumbing for `/selftest` (service behaviour, not just the edge).
