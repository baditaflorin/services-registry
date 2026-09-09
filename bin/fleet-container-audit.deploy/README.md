# fleet-container-audit — deploy

Runs **on each dockerhost** (not an external vantage). Companion to
`fleet-edge-probe`: catches crash-loops, `Created`/`Exited` containers,
`unhealthy`, and host_port owned by a foreign process — before or without
an edge hit. Both 2026-09-09 outages (python-proxy crash-loop,
fleet-cert-watch port collision) would have tripped it immediately.

## Install (per dockerhost)

```bash
sudo install -m755 bin/fleet-container-audit.py /usr/local/bin/fleet-container-audit
sudo tee /etc/default/fleet-container-audit >/dev/null <<'ENV'
CA_OO_URL=http://<openobserve-host>:5080/api/default/fleet_container_audit/_json
CA_OO_AUTH=<user>:<pass>
CA_PROM=
ENV
sudo chmod 600 /etc/default/fleet-container-audit
sudo cp bin/fleet-container-audit.deploy/fleet-container-audit.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now fleet-container-audit.timer
```

Live on `ubuntuvm1` (0docker dockerhost) → OpenObserve stream
`fleet_container_audit` on LXC 106, alert `fleet_container_bad` → `fleet_email`
(test-fired 2026-09-09). Repeat on the prod dockerhost (10.10.10.30).

## Findings

| check | meaning |
|---|---|
| `no_container` | registered service, no matching container, nothing on its port |
| `not_running` | container exists, `State != running` (Created / Exited / Paused / Dead) |
| `restarting` | container is in a restart loop right now |
| `restart_storm` | `RestartCount >= 20` (crash-looped a lot) |
| `unhealthy` | docker HEALTHCHECK == unhealthy |
| `port_foreign` | the registered host_port is published by a **different** container, or (no container matched) held by a non-docker listener that isn't a registered `$external` |
| `image_tag_latest` *(warn)* | container runs `:latest` — ADR-0028 wants `:<short-sha>` in prod (drift-prone, a hand `docker compose up`) |
| `image_version_mismatch` *(warn)* | container runs `:<semver>` ≠ the registry `version` — running stale code, deploy never happened |

`fail` findings count toward `bad` (the OO alert threshold); `warn` findings
(`image_*`) surface on the dashboard but don't page.

Host-networked containers (Prometheus, Alertmanager) publish no docker
port mapping; the port check only trusts the docker-proxy mapping, so
they don't false-positive.
