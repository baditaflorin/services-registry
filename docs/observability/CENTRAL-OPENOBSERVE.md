# Central OpenObserve collection

OpenObserve is the central log store for the fleet. On enrolled Docker hosts,
collection is independent of application deploys: a `vector-log-shipper` reads
Docker's existing `json-file` logs and sends a copy to the central
`docker_logs` stream. Verify coverage per host; not every Docker host currently
has a shipper.

## Topology

```text
Enrolled Docker host (0docker or 0mcp)
  └─ Vector agent + local disk buffer
       └─ HTTPS ingest (gzip, batches, retry/backoff)
            └─ OpenObserve VM 630 on 0own (`https://openobserve.0own.com`)
                 └─ configured streams — logs, metrics, and traces
```

The 0mcp fleet uses the public TLS endpoint because its private `10.20.10.x`
network is independent of 0own's private network. The endpoint is the shared
0own instance. Historical data and dashboards from the former 0docker CT are
being recovered from its PBS snapshots; a healthy endpoint alone does not
confirm that historical data has been imported.

Docker and host logs use the OpenObserve JSON ingestion API at
`https://openobserve.0own.com`. Application traces use the separate OTLP
receiver at `https://otlp.0exec.com`; do not send OTLP traffic to the JSON log
endpoint.

On 2026-10-07 the active instance did not have a `default` stream, so do not
assume host/journald logs are available. Query `docker_logs` by exact host and
`container_name`; rows only cover hosts with an active shipper. As of
2026-10-08, nine active shippers were verified: three Docker hosts on 0docker
and all six planned Docker hosts on 0mcp. The GitHub email runner shipper is
active and sends to the current JSON endpoint above. A central query confirmed
`docker-runtime-lv3` had logs for 29 containers (1,273 events in the preceding
20 minutes). The 0docker Builder CT and the pve01 and 0own Buildx workers are
not enrolled. Treat missing rows as a collection gap, not a clean run. Check
the live stream inventory and per-host coverage before relying on OpenObserve
results.

## Collection contract

Every Docker log record should carry these stable fields:

- `fleet`: `0docker` or `0mcp`
- `host`: stable host name, not an ephemeral container ID
- `role`: host role such as `docker-runtime` or `docker-build`
- `container_name`, `container_id`, `image`, `stream`, `message`, `timestamp`
- Docker Compose labels when present (`com.docker.compose.project`,
  `com.docker.compose.service`, and related labels)

Do not put credentials, full environment files, or authorization headers into
application log messages. If a future redaction transform is introduced, it
must preserve the original event timestamp and the container identity.

## Reliability requirements

- The agent is `restart: always` and must be enabled on every enrolled Docker host.
- Use a disk buffer so a temporary OpenObserve outage does not drop logs.
- Use gzip and bounded batches to keep CPU and network overhead predictable.
- Keep Docker's `json-file` driver and rotation unchanged; Vector is a second
  read path, not a replacement for `docker logs`.
- Exclude only the shipper itself to avoid a self-ingestion loop.
- Validate the rendered Vector configuration before replacing a running agent.
- Query the central store with `bin/oo`; do not create ad-hoc query scripts.

## Adding a host

Install the pinned Vector compose shape under
`/opt/observability/vector-log-shipper/`, render `vector.toml` with the
OpenObserve credentials from the private fleet secret path, and start it with
`bin/observability-install-0mcp <host>`, which reads exactly one
`{"user":"...","password":"..."}` object from stdin. Do not put the
credentials in command arguments or environment variables. The installer
creates a root-only (`0600`) config, validates the candidate before replacing
the active config, and restores the previous config if the new container does
not start. Use a distinct stable `fleet` and `host` value. Verify the agent is
running and that `bin/oo hosts`/`bin/oo containers` show the new host before
declaring the rollout complete.

At approximately 200 hosts, keep the same agent contract but review
OpenObserve ingest rate, disk growth, retention, and the number of concurrent
HTTP connections. A relay tier is optional; it should only be added if direct
HTTPS egress or central endpoint load becomes a constraint.

## Metrics and traces

Telegraf metrics remain a separate metrics pipeline. Do not encode high-volume
metrics as log lines. Application traces and request correlation fields may be
added to `docker_logs` or a dedicated trace stream later, but the collector
must remain enabled continuously in production so live diagnostic agents can
reconstruct failures across deploys and container replacements.
