# FLEET — conventions for the baditaflorin fleet

This is the **public-safe** conventions doc for the service fleet behind
`catalog.0exec.com` and `services-dashboard.0crawl.com`. It documents
architecture, the IaC pipeline, slug rules, and gotchas — without naming
SSH targets, IP blocks, or credential values. Operational topology and
secrets are in the private `fleet-state/OPS.md`.

## Ecosystem at a glance

baditaflorin/* hosts ~220 service repos plus ~130 prototype/experiment
repos (the `implemment-*` namespace). The 223 currently in the registry
are the ones with `mesh-{0exec,0crawl,pages}` GitHub topics. For each
service the registry captures three orthogonal classifying axes plus
the usual metadata (name, description, host_port, container_port,
auth model, TRL, ceiling):

| Axis       | Values                                  | Drives                                                       |
|------------|-----------------------------------------|--------------------------------------------------------------|
| `kind`     | `container`, `static`                   | which fleet-runner operations apply (deploy / health / bump only run on `container`) |
| `mesh`     | `0exec`, `0crawl`, `pages`              | which network + auth domain the service lives in             |
| `runtime`  | `compose`, `systemd`, `binary`, `k8s`, `github-pages`, `external` | which deploy mechanism applies (orthogonal to language) |
| `language` | `go`, `node`, `python`, `c`, `rust`, `html`, `wasm`, `other` | bulk-op filters (dep bumps, lockfile audits, etc.) |

Today's snapshot:

| `kind`      | count | `mesh` breakdown                | `language` breakdown                              |
|-------------|-------|---------------------------------|---------------------------------------------------|
| `container` | 160   | 0exec 29, 0crawl 131            | go 154, node 4, python 1, c 1                     |
| `static`    | 63    | pages 63                        | html 63                                           |

By `runtime`: 160 `compose`, 63 `github-pages`. The other runtime values are reserved for future expansion (a Go binary registered as `runtime: systemd` could deploy via `systemctl restart` instead of `docker compose`; nothing in the fleet uses them today).

**`kind` is the load-bearing field for tooling.** When you write a new
audit, a new bulk operation, or a new agent prompt, gate on `kind`,
not on `mesh`. New deployable shapes (serverless, cdn-only,
external-api) will be added as new `kind` values without touching
mesh semantics.

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

The three meshes share a single registry and (for the two container
meshes) a single auth backend — the keystore:

| Mesh         | `kind`     | Domain pattern              | Auth                                                          | Used for                                     |
|--------------|------------|-----------------------------|---------------------------------------------------------------|----------------------------------------------|
| `mesh-0exec` | container  | `<slug>.0exec.com`          | api_key in `?api_key=` or `X-API-Key` — keystore-gated         | proxy, search, ocr, security, infrastructure |
| `mesh-0crawl`| container  | `<slug>.0crawl.com`         | **api_key OR legacy `/t/<token>/…`** — both keystore-gated     | domains, recon, web-analysis                 |
| `mesh-pages` | static     | varies (homepage or *.github.io) | none                                                     | static dashboards, browser-only WASM apps    |

Both container meshes flow through the **same** `auth_request` →
`go-apikey-service` pipeline. One revocation in the keystore kills a
key on every container service across both domains. Per-service token
constants on 0crawl are dead code — kept around as harmless dead
constants until each repo is next touched.

Mesh is declared per repo via the GitHub topic `mesh-0exec` /
`mesh-0crawl` / `mesh-pages`; `kind` is derived from mesh by
`bin/generate.py` (`pages` → `static`, else `container`). Language
is derived from the explicit topic `lang-<x>` (preferred) or the
legacy tag-soup (`node`, `c`, …). Category is declared via
`category-<x>`.

## Authentication: the keystore (`go-apikey-service`)

**This is the fleet's single point of compromise.** Treat it like a
CA root. Every **container** service — both 0exec and 0crawl — trusts
whatever the keystore says.

- **What it is**: a Go HTTP service (`baditaflorin/go-apikey-service`)
  backed by SQLite. Issues, verifies, revokes, lists keys. Runs on the
  dockerhost, internal-only (not internet-reachable).
- **How keys flow**:
  1. Caller hits one of:
     - `https://<slug>.0exec.com/...?api_key=<key>`
     - `https://<slug>.0crawl.com/...?api_key=<key>` (new shape)
     - `https://<slug>.0crawl.com/t/<key>/...` (legacy shape, kept working)
  2. nginx extracts the candidate key from query / header / path
     and runs `auth_request` → `_verify_key` location.
  3. Static fallback first: if the candidate matches the universal
     demo key `$default_token` (sourced from `/etc/nginx/conf.d/_default_token.conf`
     on the gateway, NOT from any repo), accept immediately and set
     `X-Auth-User: demo`. The demo path is rate-limited to 1 req/s
     and ~60 req/h per IP at the gateway.
  4. Otherwise POST `X-Verify-Key=<key>` to `/verify` on the keystore.
  5. Keystore checks SQLite → returns 200 + `X-Auth-User`/`X-Auth-Scope`,
     or 401.
- **Why two layers**: the static fallback means a brief keystore
  outage doesn't kill the public demo path. Real per-user keys still
  flow through the dynamic check.
- **Rotating the default token without touching repos**: see the
  "Default-token rotation" section below.

### Default-token rotation

The universal demo key is **never** committed to a public repo. The
canonical store is `/etc/nginx/conf.d/_default_token.conf` on the
webgateway, included by every container-mesh vhost. To rotate:

```bash
fleet-runner rotate-default-token "<new-value>"   # gateway-only, instant
fleet-runner default-token                        # prints current value
```

The hub (`hub_scrapetheworld_org`) and catalog (`go-catalog-service`)
fetch the current value at boot from the **private**
`fleet-state/secrets/default_token.txt`; the rotation command updates
both atomically (gateway file + private secret). No public commit, no
per-repo edit, no service restart required.

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

## Fleet-wide changes — modify 130 repos at once

The fleet has ~160 container repos (Go-heavy) plus 63 static Pages.
The cardinal principle: **change the library or the gateway template,
not the consumers.** When you need to touch every service, the right
pattern is almost always a `go-common` change + a dep bump, or a
0exec / 0crawl nginx template change + `nginx-render --push --reload`
— not N PRs.

**Always scope bulk operations with filters.** A Go dep bump should
not even attempt to touch a Node, Python, C, or static service:

```bash
fleet-runner update-dep github.com/baditaflorin/go-common@v0.9.0 \
  --filter kind=container,language=go
```

The filter is enforced before any clone/edit happens, so non-Go
repos are never opened. Same pattern for `build-test`, `astedit`,
`exec`, `inject`, etc.

`fleet-runner` ships the bulk primitives:

| Command | What it does |
|---|---|
| `fleet-runner update-dep <mod@ver>` | `go get <mod@ver> && go mod tidy` in every repo |
| `fleet-runner inject <src> <dest>` | copy a file (e.g. `FLEET.md`) into every repo |
| `fleet-runner exec "<cmd>"` | run any shell command in every repo (sed, formatter, etc.) |
| `fleet-runner build-test` | `go test ./...` across every repo — regression gate |
| `fleet-runner push "<msg>"` | commit + push every dirty repo |

### Worked example: migrate every 0exec service to keystore auth

Today most services use `middleware.TokenAuth(staticList)`. To swap
that for keystore-backed validation across all 130 repos:

1. **One commit to `go-common`** — already done in v0.7.0:
   ```go
   // new in middleware/auth_keystore.go
   middleware.TokenAuthKeystore(middleware.KeystoreOpts{
       Verifier:    apikey.NewCache(apikey.New()),
       LocalTokens: []string{"default_token", "fb_…"},
   })
   ```
   This middleware trusts the gateway's `X-Auth-User` header, has a
   local fast-path for the static fallback key, calls the keystore
   for everything else with 15-min stale tolerance, and fails closed
   on keystore outage.

2. **One bulk dep bump** — bumps every fleet repo to `go-common@v0.7.0`:
   ```bash
   fleet-runner update-dep github.com/baditaflorin/go-common@v0.7.0
   fleet-runner build-test          # verify nothing broke
   fleet-runner push "deps: go-common v0.7.0 (keystore middleware available)"
   ```

3. **Per-service swap** is then a 3-line change in each service's
   `main.go` — but **most services don't need it** because the gateway
   sets `X-Auth-User` and the new middleware trusts that automatically.
   Services that still use the legacy `middleware.TokenAuth` keep
   working unchanged. Migrate the loud ones (high-traffic, security-
   sensitive) first; let the long tail drain organically when each
   repo next gets touched.

The net effect: a library-level change scales to 130 repos without
130 individual code reviews. `fleet-runner build-test` is the
regression gate before `push`. `fleet-runner state snapshot --push`
after deploys gives you fleet-wide health visibility.

### Anti-pattern to avoid

If you find yourself writing a sed-pipeline to mutate every service's
`main.go` directly, **stop**. The codepath you're trying to change
probably belongs in `go-common` (or `0crawl-platform`'s nginx templates).
Changing it there is one PR + one dep bump; doing it per-repo is 130
commits + 130 review cycles + 130 chances for drift.

## IaC pipeline

Two parallel render targets — gateway nginx vhosts AND dockerhost docker-compose files — both driven from this registry. Neither lives as hand-edited state on the target host.

```
overrides.json (per-slug + $rules)     slug.json (slug map)
       │                                       │
       └──────────┐                  ┌─────────┘
                  v                  v
              bin/generate.py  ─reads─>  services.json (canonical)
                  │                          │
                  │                          ├──> fleet-runner nginx-render --push --reload
                  │                          │       gateway: sites-available/ + sites-enabled/
                  │                          │
                  │                          ├──> fleet-runner render-compose --push --restart
host-conventions.yaml ─────────────────────> │       dockerhost: /opt/<area>/<service>/docker-compose.yml
       (extra_hosts, env defaults, restart,  │
        networks for every container)        │
                                             └──> fleet-runner inject  (CLAUDE.md to every repo)
```

1. Edit `overrides.json` (per-slug patches or bulk `$rules`) or add topics to a repo.
2. `python3 bin/generate.py` rebuilds `services.json` from GitHub topics + overrides.
3. Commit `services.json` + every `services.*.json` slice + `overrides.json` (+ `services.summary.txt`).
4. `bin/notify-consumers.sh` pings the dashboards to refresh.
5. `fleet-runner nginx-render --push --reload` renders + ships gateway vhosts.
6. `fleet-runner render-compose --push --restart` renders + ships per-service docker-compose.yml to every dockerhost. (Same diff/push/reload pattern as nginx-render; mesh changes that need every service's compose updated land here.)

### host-conventions.yaml — the compose-side analogue of nginx templates

[`host-conventions.yaml`](host-conventions.yaml) declares fleet-wide compose primitives that apply to every container service unless explicitly overridden. Today: `extra_hosts: host.docker.internal:host-gateway`, default `APIKEY_SERVICE_URL`, default `LOG_LEVEL`, default networks. Adding a new fleet-wide compose key (e.g. a log driver, a new env var every service reads) is ONE PR here + `fleet-runner render-compose --push --restart --all` instead of SSH-editing N dockerhost composes.

Precedence (declaration order):
1. `container_defaults` — every container
2. `mesh_defaults.<mesh>` — per-mesh layer
3. `overrides.json` `$rules` — match by `mesh`/`kind`/`ids` etc., patch any compose field
4. `overrides.json` per-slug entry — wins over everything else

Schema: [`schema/host-conventions.v1.json`](schema/host-conventions.v1.json). The renderer refuses unknown top-level keys, so adding a new primitive requires updating the schema in the same PR (loud-by-design).

`bin/generate.py` writes seven sibling projection files
(`services.ids.json`, `services.names.json`, `services.minimal.json`,
`services.urls.json`, `services.trl.json`, `services.ports.json`,
`services.deploy.json`) so token-constrained consumers (AI agents,
small dashboards) can fetch a 5-60 KB slice instead of the full
~280 KB blob. See `README.md` → "Sliced URLs". Slices are derived;
rebuild without re-querying GitHub via `python3 bin/generate.py --slices-only`.

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
