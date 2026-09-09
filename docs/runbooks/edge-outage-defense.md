# Defense against silent edge / deploy outages

The 2026-09 cookie-checker incident (a removed gateway vhost that 308-bounced
to a valid-cert 200 elsewhere) and the python-proxy incident (a broken image
crash-looping for 3 weeks) shared one root shape: **a deploy half-landed and
nothing noticed.** These are the layers now in place.

| Layer | Tool | Catches | Where |
|---|---|---|---|
| 1. External edge probe | `bin/fleet-edge-probe.py` (`fleet-edge-probe.timer`, 15 min) | wrong/expired cert, cert SAN ≠ `cert_domain`, cross-domain redirect, default-vhost fallback body, gateway-IP drift | `monitoring-lv3` (compose) + Builder LXC 108 (coolify) → Prometheus/Alertmanager + OpenObserve `fleet_edge_probe` |
| 2. Dockerhost container audit | `bin/fleet-container-audit.py` (`fleet-container-audit.timer`, 10 min) | `no_container`, `not_running`, `restarting`, `restart_storm`, `unhealthy`, `port_foreign`, `image_tag_latest`, `image_version_mismatch` | each dockerhost → OpenObserve `fleet_container_audit`, alert `fleet_container_bad` |
| 3. CI image boot-smoke | `templates/woodpecker-boot-smoke.yml` (opt-in per repo) | Dockerfile-level breakage the checkout can't see — missing `COPY`, bad `ENTRYPOINT`, dropped runtime dep | Woodpecker `.woodpecker.yml`, PR-time |
| 4. Pre-deploy edge gate | `go-fleet-preflight` `edge` check (≥ 0.3.6) | the migration class — vhost removed on a `runtime` flip and never recreated; probes `/health` **without following redirects** | `POST /preflight/<repo>` before every deploy |

### Deploy discipline (#4)

Deploy **only** via `fleet-runner deploy` — its pipeline has the drift check,
fresh-worktree pre-flight, digest assertion, health-wait, and the smoke gate +
auto-rollback. Both 2026-09 failures were hand `docker compose up`. Layer 2's
`image_tag_latest` / `image_version_mismatch` findings surface a hand-deploy
after the fact; there is no hard block on the manual path yet.

**Known `fleet-runner deploy` gap:** it re-renders the nginx vhost post-deploy
but NOT `docker-compose.override.yml` (the canonical port binding from
`render-compose`). After a registry port change, follow the deploy with
`fleet-runner render-compose --filter <slug> --push --restart`. (fleet-cert-watch
18320→18316, 2026-09-09.)

### Wiring the preflight gate into deploy (#6, remaining)

`go-fleet-preflight` ≥ 0.3.6 has the `edge` check, but `fleet-runner deploy`
does not yet call preflight automatically. Until it does, run
`curl -s https://go-fleet-preflight.0exec.com/preflight/<repo>` (or the MCP
tool) before a migration deploy and require `ok: true`.
