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
| `gateway_drift` *(warn)* | A record not in the expected gateway set for `runtime` (compose → 176.9.123.221, coolify → 65.108.75.123) |
| `tls_untrusted` | TLS chain does not validate against the system trust store (no `-k`) |
| `cert_host_mismatch` | leaf cert SAN does not cover this hostname |
| `cert_registry_mismatch` | SAN does not match registry `cert_domain` (`wildcard.0crawl.com` ⇒ SAN must include `*.0crawl.com`) |
| `cert_expiring` *(warn)* | cert `notAfter` < now + `--expiry-days` (default 14) |
| `offsite_redirect` | health check ended on a different host than requested |
| `health_status` | health check returned `000` / `5xx` (real outage). Uses registry `health_url`, else `/health` |
| `health_forbidden` *(warn)* | health check returned `401`/`403` — edge up, endpoint gated (infra services) |
| `health_notfound` *(warn)* | health check returned `404` — wrong path / missing `health_url` in registry |
| `health_body` | `2xx` but body is not JSON `status:ok` / `service` / `version`, nor a plain `ok`/`healthy` |
| `fallback_vhost` | body matches a known default-vhost fingerprint (`<title>LV3`, `nginx.0mcp.com`, …) |
| `example_unreachable` *(warn)* | `GET <url><example_path>` ∉ {200,401,403} or landed off-host — usually a stale `example_path` in the registry |

Exit `0` = all probed pass · `1` = ≥1 hard fail · `2` = usage/fetch error.
Warn-severity codes do not affect exit code.

## Run

```bash
# whole fleet, human output (failures + summary only)
bin/fleet-edge-probe.py

# everything, incl. passing rows
bin/fleet-edge-probe.py --all

# one mesh / a few slugs / a quick sample
bin/fleet-edge-probe.py --runtime coolify
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
POSTs only **newly-broken** and **recovered** services, so a persistent
failure pages once, not every 10 minutes.

Ship `/var/log/fleet-edge-probe.jsonl` into OpenObserve (stream
`fleet_edge_probe`) for history / dashboards, same as `docker_logs`.

## Topology assumptions (edit `GATEWAY_IPS` in the script when this changes)

- `compose`  services → gateway `176.9.123.221` (0docker webgateway, `wildcard.*` certs, `fleet-runner nginx-render`)
- `coolify`  services → gateway `65.108.75.123` (0mcp edge `nginx-lv3` → Coolify Traefik `10.20.10.71:443`)
- `external` / `network_exposure: internal` → skipped

## Deployed (2026-09-09) — two vantages, one per fleet

Each fleet's edge is only truly *external* from the **other** fleet's network
(a box inside a fleet hairpin-NATs to its own edge IP → `ECONNREFUSED`). So
the probe runs on one box per fleet, each filtered to the services the *other*
fleet hosts:

| vantage host | `EDGE_PROBE_RUNTIME` | covers | schedule | alert path |
|---|---|---|---|---|
| `monitoring-lv3` (0mcp) | `compose` | 0docker-hosted services (→ `176.9.123.221`) | `*:02/15` | node_exporter textfile → Prometheus job `fleet-edge-probe` → rules in `/etc/prometheus/rules/fleet-edge-probe.yml` → Alertmanager `ntfy-critical` + `mattermost-critical` |
| Builder LXC 108 (0docker) | `coolify` | 0mcp-hosted services (→ `65.108.75.123`) | `*:09/15` | textfile (`/var/lib/prometheus-node-exporter/textfile/`) **+** `fleet-edge-probe-ship-oo` → OpenObserve stream `fleet_edge_probe` on LXC 106 |

Artifacts in `bin/fleet-edge-probe.deploy/`:

- `fleet-edge-probe.service` / `.timer` — systemd oneshot + timer. Config via
  `/etc/default/fleet-edge-probe` (`EDGE_PROBE_SRC_URL`, `EDGE_PROBE_PROM`,
  `EDGE_PROBE_VANTAGE`, `EDGE_PROBE_RUNTIME`, `EDGE_PROBE_NTFY`, and on the
  0docker box `EDGE_PROBE_OO_URL` / `EDGE_PROBE_OO_AUTH`).
- `prom-rule-fleet-edge-probe.yml` — 3 alert rules: `FleetServiceEdgeDown`
  (per-service hard fail, `for: 15m`, critical), `FleetEdgeManyDown` (≥5 at
  once → batch cert/DNS/vhost failure, critical), `FleetEdgeProbeStale`
  (no run in >1h, warning).
- `fleet-edge-probe-ship-oo` — pushes each run's `state.json` (summary +
  one row per failing service) into OpenObserve for the 0docker vantage,
  where there is no local Prometheus.

### OpenObserve alert (0docker vantage) — live

Created 2026-09-09 on the LXC 106 OpenObserve (`v0.14.7`) via
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
