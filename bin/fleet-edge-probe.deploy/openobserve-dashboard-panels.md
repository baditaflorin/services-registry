# OpenObserve dashboard — coolify vantage (manual, 2 min in the OO UI)

The 0docker vantage ships to OpenObserve stream `fleet_edge_probe` (LXC 106).
OO's dashboard-panel JSON schema (v0.14.7) is version-specific and fiddly to
POST blind, so add these panels in the UI (Dashboards → New → Add panel →
"Custom SQL"). Same three views as the Grafana dashboard:

**Hard failures (latest)** — Metric panel
```sql
SELECT hard_fail_total AS y FROM fleet_edge_probe
WHERE kind = 'summary' ORDER BY _timestamp DESC LIMIT 1
```

**Hard failures over time** — Line panel
```sql
SELECT histogram(_timestamp, '15 minute') AS x, max(hard_fail_total) AS y
FROM fleet_edge_probe WHERE kind = 'summary' GROUP BY x ORDER BY x
```

**Failing / warning services** — Table panel
```sql
SELECT slug, host, runtime, fails, cert_err FROM fleet_edge_probe
WHERE kind = 'service' ORDER BY _timestamp DESC LIMIT 50
```

## Or: one Grafana dashboard for both vantages (cleaner, ~15 min)

The Grafana on `monitoring-lv3` can't reach LXC 108's node_exporter
(`10.10.10.108:9100`) directly. To unify:

1. On the 0docker webgateway (`florin@10.10.10.10`), add a location that
   `proxy_pass`es `http://10.10.10.108:9100/metrics` behind basic auth
   (monitoring-lv3 already reaches `10.10.10.10:443` for blackbox).
2. On `monitoring-lv3`, add a Prometheus scrape job for that URL with the
   basic-auth creds and `metric_relabel_configs` keeping `fleet_edge_probe_*`.
3. `grafana-dashboard.json`'s `$vantage` template var then lists both
   `monitoring-lv3-0mcp` (compose) and `builder-lxc108-0docker` (coolify)
   and every panel works for both. The OO `fleet_edge_hard_fail` alert can
   then be retired in favour of the Prometheus `FleetServiceEdgeDown` rule.
