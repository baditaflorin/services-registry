# fleet-selftest-poll

Polls `GET /selftest` of every fleet container service and exports the result as Prometheus metrics through the
node_exporter textfile collector. Same plumbing as [fleet-edge-probe](fleet-edge-probe.README.md): systemd timer ->
textfile -> Prometheus -> Alertmanager -> ntfy.

## Why it exists

Every service ships `/selftest` (ADR-0015) and `SelftestCheckFailing` (`fleet_selftest_check_pass == 0`) was written
for it, but no series ever existed, so the alert could not fire:

- go-common reports a selftest to Prometheus only when a service passes `selftest.WithObserver(...)`; none do;
- `go-fleet-selftest-aggregator` polls `/selftest` but exports no selftest series, and production runs it in the
  one-service graph-pilot mode (`POLL_SERVICE_ALLOWLIST=fleet-preflight`), so even its board covers one service.

## Where it probes

| service kind | route | why |
|---|---|---|
| compose (0docker) with a registry `host_port` | `http://<docker host>:<host_port>/selftest`, hosts tried in order (`10.10.10.20` dockerhost, `10.10.10.30` docker-prod) | the gateway puts an API-key `auth_request` in front of the service; `/selftest` itself is public on the container, so the LAN route needs no credential |
| coolify (0mcp) and anything without a LAN route | public edge `https://<host>/selftest` | open on the edge |

A compose service that answers on none of the docker hosts is `unreachable`: the container is down, or the registry
names a host/port it no longer uses. Retired (`lifecycle: sunset`), `external` and `lan-internal` services are skipped.

## Metrics

| metric | meaning |
|---|---|
| `fleet_selftest_check_pass{service,check}` | 1/0 per named check (`_overall` if the body has none) |
| `fleet_selftest_service_state{service,state}` | `pass` `fail` `unreachable` `skip` (404) `gated` (401/403) |
| `fleet_selftest_consecutive_failures{service}` | polls in a row that were `fail`/`unreachable` |
| `fleet_selftest_services{state}` | tally per state |
| `fleet_selftest_poll_*` | targets, last run timestamp, duration, up |

Alert rules live in go-fleet-metrics-hub `prometheus/rules/selftest-alerts.yml`. They wait for two failing polls in a
row (one hour), so a single flaky poll never pages; `skip` and `gated` never alert.

## Deploy (Builder CT108)

```sh
sh bin/fleet-selftest-poll.deploy/install.sh     # idempotent; config in /etc/default/fleet-selftest-poll
```

Run by hand: `fleet-selftest-poll --services /var/lib/fleet-selftest-poll/services.json --direct-hosts 10.10.10.20,10.10.10.30 --json`.
Tests: `python3 -m unittest bin/test_fleet_selftest_poll.py`.
