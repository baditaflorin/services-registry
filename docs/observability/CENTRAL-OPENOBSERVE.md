# Central OpenObserve collection

OpenObserve is the central log store for the fleet. Collection is
continuous in production and is independent of application deploys: each
Docker host runs a `vector-log-shipper` agent that reads Docker's existing
`json-file` logs and sends a copy to the central `docker_logs` stream.

## Topology

```text
Docker host (0docker or 0mcp)
  └─ Vector Docker agent + local disk buffer
       └─ HTTPS ingest (gzip, batches, retry/backoff)
            └─ OpenObserve LXC 106 on the 0docker fleet
                 ├─ docker_logs  — container stdout/stderr
                 ├─ host_logs    — non-Docker journald
                 └─ metrics      — future metrics bridge

Non-Docker host (0mcp)
  └─ Vector native systemd service + local disk buffer
       └─ HTTPS ingest (gzip, batches, retry/backoff)
            └─ OpenObserve `host_logs` stream
```

The 0mcp fleet uses the public TLS endpoint because its private `10.10.10.x`
network is independent of the 0docker private network. The endpoint is still
the same OpenObserve instance and therefore keeps one searchable history.

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

Native host records use the same `fleet`, `host`, `role`, `collector`, and
`observability_schema` fields, plus `source_type=journald`; they are kept in
`host_logs` so system/service records remain distinguishable from container
stdout/stderr.

## Reliability requirements

- The agent is `restart: always` and must be enabled on every Docker host.
- Use a disk buffer so a temporary OpenObserve outage does not drop logs.
- Use gzip and bounded batches to keep CPU and network overhead predictable.
- Keep Docker's `json-file` driver and rotation unchanged; Vector is a second
  read path, not a replacement for `docker logs`.
- Exclude only the shipper itself to avoid a self-ingestion loop.
- Validate the rendered Vector configuration before replacing a running agent.
- Query the central store with `bin/oo`; do not create ad-hoc query scripts.

For non-Docker hosts, install the pinned native collector with
`bin/observability-install-host-logs`. It runs as a systemd service named
`vector-host-logs`, reads journald as root, and uses the same disk-buffer and
TLS contract as the Docker collector. The initial rollout can set
`VECTOR_HOST_LOGS_SKIP_HISTORY=1` to preserve the old journal cursor and start
at the current journal tail; the installer stores that old cursor under
`/var/backups/vector-host-logs/`.

## Adding a host

Install the pinned Vector compose shape under
`/opt/observability/vector-log-shipper/`, render `vector.toml` with the
OpenObserve credentials from the private fleet secret path, and start it with
`docker compose up -d`. Use a distinct stable `fleet` and `host` value. Verify
the agent is healthy and that `bin/oo hosts`/`bin/oo containers` show the new
host before declaring the rollout complete.

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
