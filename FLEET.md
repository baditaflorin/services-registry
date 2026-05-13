# FLEET — conventions for the baditaflorin fleet

This is the **public-safe** conventions doc for the service fleet behind
`catalog.0exec.com` and `services-dashboard.0crawl.com`. It documents
architecture, the IaC pipeline, slug rules, and gotchas — without naming
SSH targets, IP blocks, or credential values. Operational topology and
secrets are in the private `fleet-state/OPS.md`.

## Ecosystem at a glance

baditaflorin/* hosts ~220 service repos plus ~130 prototype/experiment
repos (the `implemment-*` namespace). The 222 currently in the registry
are the ones with `mesh-{0exec,0crawl,pages}` GitHub topics, and they
form three distinct meshes (table below). For each service the registry
captures: human-readable name, description, host_port, container_port,
auth model, TRL (technology readiness level), and TRL ceiling (if any).

| Repo                                | Role                                                            | Visibility |
|-------------------------------------|-----------------------------------------------------------------|------------|
| `services-registry`                 | canonical service catalog (this repo). Pure metadata, no live state. | PUBLIC |
| `go-common`                         | shared Go library (HTTP client w/ SSRF guard, auth middleware, jsbundle recovery, ua builder). Every Go service imports this. | PUBLIC |
| `go_fleet_runner`                   | CLI that operates the fleet: `health`, `smoke`, `state snapshot`, `whois`, `nginx-render`, `deploy`, `allocate-port`, `nginx-drift-audit`, `inject`, `push`. | PRIVATE |
| `0crawl-platform`                   | nginx vhost templates + bootstrap scripts. Templates embedded in fleet-runner too. | PRIVATE |
| `fleet-state`                       | live operational state: snapshots, session summaries, SSH topology, rotation playbooks. | PRIVATE |
| `go-catalog-service` (catalog.0exec.com) | renders services.json into a public catalog table. | PRIVATE |
| `hub_scrapetheworld_org` (hub.scrapetheworld.org) | dashboard/login UI consuming services.json. | PRIVATE |
| `go_<thing>` (×~220)                | one repo per service. Examples: `go_smuggling_probe`, `go_biz_classifier`, `go_jwt_pentest`. Each ships its own Dockerfile + docker-compose.yml. | varies |

## TRL — technology readiness level

Every entry in `services.json` may carry a `trl` field 1-9. Convention:

| TRL | Band         | Meaning                                                                       |
|-----|--------------|-------------------------------------------------------------------------------|
| 1-3 | **toy**      | single regex / no tests / one file. Don't depend on it.                       |
| 4-5 | **developing** | curated lists or gazetteers, multi-step logic, partial tests.                 |
| 6-7 | **real**     | RFC-compliant parsing, evidence trails, verdicts, real test coverage.         |
| 8-9 | **production** | battle-tested with cross-checks, comprehensive. SLA-grade.                    |

`trl_ceiling` flags services that **structurally cannot** advance further
with CPU-only smart logic — for example, `xss-scanner` needs DOM canary
injection (browser engine); `bucket-finder` needs paid threat intel feeds.
A low ceiling is a deprecation signal: filter `select(trl_ceiling != null
and trl_ceiling <= 5)` in services.json for candidates.

`trl_assessed_at` should be ≤90 days for the data to be trustworthy. Older
than that, treat as stale and re-audit. The catalog and hub UIs surface
TRL as a colored pill so you don't have to open the JSON to see it.

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

## Authentication: the keystore (`go-apikey-service`)

**This is the fleet's single point of compromise.** Treat it like a CA
root. Every 0exec service trusts whatever it says.

- **What it is**: a Go HTTP service (`baditaflorin/go-apikey-service`)
  backed by SQLite. Issues, verifies, revokes, lists keys. Runs on the
  dockerhost, internal-only (not internet-reachable).
- **How keys flow**:
  1. Caller hits `https://<slug>.0exec.com/...?api_key=<key>`
  2. nginx auth_request → `_verify_key` location
  3. Static fallback first: if key matches the universal demo key
     hardcoded in the vhost (e.g. `fb_05dea…`), accept immediately.
  4. Otherwise POST `X-Verify-Key=<key>` to `/verify` on the keystore.
  5. Keystore checks SQLite → returns 200 + `X-Auth-User`/`X-Auth-Scope`,
     or 401.
- **Why two layers**: the static fallback means a brief keystore
  outage doesn't kill the public demo path. Real per-user keys still
  flow through the dynamic check.

### Clients MUST use `go-common/apikey`, not handroll HTTP calls

Every service that needs to verify/issue/revoke keys imports the
canonical package:

```go
import "github.com/baditaflorin/go-common/apikey"

c := apikey.New() // wired from APIKEY_SERVICE_URL + APIKEY_SERVICE_ADMIN_TOKEN
// Verify path:
result, err := c.Verify(ctx, userKey)
// or with graceful degradation across keystore outages:
verifier := apikey.NewCache(c) // serves stale-but-valid for up to 15m
result, err = verifier.Verify(ctx, userKey)
```

The `Cache` layer is the **graceful-degradation** primitive: positive
results survive a short keystore outage; negative results (401) are
never cached so revocations take effect immediately on next call.

### What to do when the keystore is down

- **Static fallback** in nginx vhosts keeps the public demo key working.
- **Per-service caches** (via `apikey.Cache`) keep recently-verified
  callers working up to 15 min.
- **Snapshot data** in `fleet-state/state/snapshot.json` flags it as a
  BROKEN service entry once `/health` fails — that's your alert.
- **Procedure** to recover lives in private `fleet-state/RUNBOOK.md`
  under "keystore outage". Short version: SSH dockerhost, restart
  the `go-apikey-service` container, verify `/health` returns 200,
  drop the Cache TTL on caller services if they're stuck on stale.

### Admin token

The keystore admin endpoints (`/issue`, `/revoke`, `/list`, `/purge`)
require `X-Admin-Token: <token>`. Token is `ADMIN_TOKEN` env var on
the keystore container; clients read it from `APIKEY_SERVICE_ADMIN_TOKEN`.
**Token storage + rotation playbook**: private `fleet-state/OPS.md`.

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

### 5. Legacy Hetzner DNS Console API is sunset

`dns.hetzner.com/api/v1/` (Hetzner DNS *Console* API, `Auth-API-Token` header)
is **deprecated and unsupported**. Use only the Hetzner *Cloud* API at
`api.hetzner.cloud/v1/zones/{id}/rrsets` with `Authorization: Bearer <token>`.
On 2026-05-13 an agent shipped `deploy` against the legacy API and the
zone-list call returned an empty result silently — a frustrating debug.
**Fix**: `fleet-runner deploy` now uses only the Cloud API; the env var
name is `HETZNER_TOKEN` (not `HETZNER_DNS_API_TOKEN`). Lesson: when a
vendor sunsets an API path, the deprecated calls often degrade to
*empty-success* rather than 404 — fail-loud requires explicit version
checks at the boundary.

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
