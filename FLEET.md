# FLEET — conventions for the baditaflorin fleet

This is the **public-safe** conventions doc for the service fleet behind
`catalog.0exec.com` and `services-dashboard.0crawl.com`. It documents
architecture, the IaC pipeline, slug rules, and gotchas — without naming
SSH targets, IP blocks, or credential values. Operational topology and
secrets are in the private `fleet-state/OPS.md`.

## Architecture

Two meshes share a single registry but have different auth models:

| Mesh         | Domain pattern              | Auth                                       | Used for                                     |
|--------------|-----------------------------|--------------------------------------------|----------------------------------------------|
| `mesh-0exec` | `<slug>.0exec.com`          | api_key in `?api_key=` or `X-API-Key`      | proxy, search, ocr, security, infrastructure |
| `mesh-0crawl`| `<slug>.0crawl.com`         | path token `/t/<token>/...`                | domains, recon, web-analysis                 |
| `mesh-pages` | varies (homepage or *.github.io) | none                                  | static dashboards, catalogs                  |

Mesh is declared per repo via the GitHub topic `mesh-0exec` / `mesh-0crawl` /
`mesh-pages`. Category is declared via `category-<x>`. `bin/generate.py`
discovers repos by querying topics on `github.com/baditaflorin/*`.

## IaC pipeline

```
overrides.json (host_port, descriptions)         slug.json (slug map)
                |                                       |
                v                                       v
              bin/generate.py  --reads-->  services.json (canonical, committed)
                                                      |
              fleet-runner nginx-render <-------------+
                                                      |
                                       gateway sites-available/, sites-enabled/
```

1. Edit `overrides.json` (per-slug patches) or add topics to a repo.
2. `python3 bin/generate.py` rebuilds `services.json` from GitHub topics + overrides.
3. Commit `services.json` + `overrides.json` (+ `services.summary.txt`).
4. `bin/notify-consumers.sh` pings the dashboards to refresh.
5. `fleet-runner nginx-render --push --reload` renders + ships gateway vhosts.

For SSH hosts and credentials, see private `fleet-state/OPS.md`.

## Slug rules

Canonical slug = the first label of the service FQDN
(`<slug>.0crawl.com` or `<slug>.0exec.com`). Stable forever once live.

Derivation: kebab-case the repo name, then for `mesh-0crawl` strip a `go-`
prefix (so `go_outlink_graph` → `outlink-graph.0crawl.com`). For `mesh-0exec`
the `go-` stays.

Some legacy services shipped under a shorter name than the auto-derivation
would yield. Those overrides live in [`slug.json`](slug.json), shared by
`bin/generate.py` and `bin/backfill-host-ports.py` (no duplicate Python maps).

Never remove an entry from `slug.json` once a service is live — the catalog
URL stability depends on the slug.

## fleet-runner commands (reference)

The runner binary is private; this is a public-safe pointer list.

| Command                                 | Purpose                                                   |
|-----------------------------------------|-----------------------------------------------------------|
| `fleet-runner health`                   | `GET /health` across every live service                   |
| `fleet-runner smoke`                    | `GET <example_url>` across every service                  |
| `fleet-runner build-test`               | `go test ./...` across every workspace                    |
| `fleet-runner update-dep <mod@ver>`     | bulk dep bump across repos                                |
| `fleet-runner inject <src> <dest>`      | copy a file into every repo (this FLEET.md was injected)  |
| `fleet-runner exec "<cmd>"`             | run a shell command in every repo                         |
| `fleet-runner push "<msg>"`             | commit+push all dirty repos                               |
| `fleet-runner new-service <name> <port>`| scaffold a new service                                    |
| `fleet-runner allocate-port`            | next free port in 18100-18999                             |
| `fleet-runner nginx-drift-audit`        | read-only gateway-vs-registry comparison                  |
| `fleet-runner nginx-render`             | IaC render of gateway vhosts (dry-run by default)         |
| `fleet-runner snapshot`                 | writes `fleet-state/state/snapshot.json` per its schema   |

## Where things live

| File / path                              | Visibility | Purpose                                          |
|------------------------------------------|------------|--------------------------------------------------|
| `services-registry/services.json`        | PUBLIC     | canonical entries, committed                     |
| `services-registry/overrides.json`       | PUBLIC     | per-slug patches (no secrets ever)               |
| `services-registry/slug.json`            | PUBLIC     | slug overrides, single source of truth           |
| `services-registry/schema/v1.json`       | PUBLIC     | JSON Schema for an entry                         |
| `services-registry/FLEET.md` (this)      | PUBLIC     | conventions                                      |
| `fleet-state/OPS.md`                     | PRIVATE    | SSH topology, env vars, rotation playbooks       |
| `fleet-state/STATE_SCHEMA.md`            | PRIVATE    | snapshot.json schema for fleet-runner            |
| `fleet-state/RUNBOOK.md`                 | PRIVATE    | step-by-step ops procedures                      |
| `fleet-state/state/*.json`               | PRIVATE    | snapshots produced by `fleet-runner snapshot`    |
| `go_fleet_runner/templates/`             | PRIVATE    | nginx vhost templates                            |

## Lessons & gotchas

Captured from real incidents — keep these on hand when something breaks.

### 1. Audit-staleness (the "7 of 14 broken" miss)

`nginx-drift-audit` used to query only `docker ps` for live upstreams,
which missed services running as native binaries on the host. The audit
declared 14 services broken; 7 of them were actually serving fine via
non-docker processes. **Fix**: also probe `ss -tlnp` for native listeners
(see `bin/backfill-host-ports.py` `native_listeners()` and the matching
logic in `fleet-runner nginx-drift-audit`). Lesson: any health/audit tool
must cover **every shape an upstream can take**, not just the most common one.

### 2. Private-default for new repos

A new pentest service was scaffolded private by default; topics resolved
to `mesh-0crawl`, generator skipped it (`gh repo list` only returns repos
the caller can see at the right visibility). The service was live but
invisible to the catalog for two days. **Fix**: explicitly verify repo
visibility on registration. Lesson: silent skips in generators are worse
than loud failures — `bin/generate.py` now warns on unmatched topics, and
operators sanity-check the summary diff after each run.

### 3. sites-enabled canonicalization

`/etc/nginx/sites-enabled/` had a mix of real files and symlinks pointing
back to `sites-available/`. Editing the sites-enabled copy gave the
illusion of a change that nginx reload reverted. **Fix**: `nginx-render`
always writes to `sites-available/` and re-creates the symlink in
`sites-enabled/`. Lesson: when there are two paths to the same data, pick
one as canonical and document it (here: `sites-available/`).

### 4. Hetzner Cloud DNS API quirks

`hcloud dns record create` silently returns success with an HTTP 200 if
the zone exists and the record body is malformed but parseable. Two A
records went unobserved for a day until the dashboard surfaced 502. **Fix**:
the new-service runbook (private `RUNBOOK.md`) requires a follow-up
`dig <slug>.<domain> A` verification step. Lesson: trust dig over the API
response when the SLA depends on it.

## No secrets policy

Restating the [README](README.md): nothing sensitive belongs here.

- API key shapes and per-service secrets are issued out-of-band; the
  registry only describes *how* to send the key.
- SSH targets, private IPs (10.x), and bastion identities live only in
  the private `fleet-state/OPS.md`.
- Public demo tokens for the 0crawl mesh are allowed; they must not grant
  privileged access.

If you spot a real secret in this repo or `go-common`, treat it as a leak:
open an issue with the redacted reference, rotate the credential per
`fleet-state/OPS.md`, then submit a PR to scrub git history.
