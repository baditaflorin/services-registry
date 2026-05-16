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
| `go-fleet-<name>` (×24)             | **fleet primitives** — shared infra services consumed by other fleet services. Examples: `go-fleet-fingerprint-cache`, `go-fleet-body-redactor`, `go-fleet-resolver-quorum`, `go-fleet-payload-corpus`. Distinct from `go_<thing>` (offensive/recon) by the `go-fleet-` prefix. **Read [`docs/adr/`](docs/adr/) for each primitive's API + migration contract.** | PRIVATE |

## Fleet primitives (added 2026-05-16)

The fleet now has a dedicated **primitives tier** — 24 `go-fleet-<name>` services that exist to be *composed with* rather than re-implemented in each detection/recon service. Adding a new detection scanner should mean reaching for these first.

| Primitive | Port | ADR | What it solves (duplication-it-removes) |
|---|---|---|---|
| `go-fleet-fingerprint-cache` | 18153 | [0003](docs/adr/0003-fleet-fingerprint-cache.md) | WAF/soft-404/CDN classification — was duplicated in 5+ scanners |
| `go-fleet-body-redactor`     | 18154 | [0004](docs/adr/0004-fleet-body-redactor.md) | sensitive-header/body redaction — was duplicated in 4 evidence services |
| `go-fleet-resolver-quorum`   | 18155 | [0005](docs/adr/0005-resolver-quorum.md) | 2-of-3 multi-DNS-resolver consensus — was duplicated in takeover-checker + others |
| `go-fleet-payload-corpus`    | 18156 | [0006](docs/adr/0006-fleet-payload-corpus.md) | versioned attack-payload corpus (125 payloads, 12 classes) |
| `go-fleet-har-builder`       | 18157 | [0007](docs/adr/0007-canonical-har-emitter.md) | HAR 1.2 evidence format for external (Burp/ZAP) interop |
| `go-fleet-poc-curl`          | 18158 | [0008](docs/adr/0008-fleet-poc-curl.md) | bash -n-parse-gated PoC curl emitter (redacted by default) |
| `go-fleet-tech-inferrer`     | 18159 | [0009](docs/adr/0009-fleet-tech-inferrer-composes-signal-services.md) | composite tech-stack inference (83 signals × favicon-hash + headers + cookies) |
| `go-fleet-diff-engine`       | 18160 | [0010](docs/adr/0010-structured-diff-engine.md) | structured diff (http_response/json/html/asset_set/text) |
| `go-fleet-call-tracer`       | 18161 | [0011](docs/adr/0011-fleet-call-tracer.md) | per-request call trace collector (Speedscope flamegraph export) |
| `go-fleet-engagement-timeline` | 18162 | [0012](docs/adr/0012-engagement-timeline-aggregator.md) | per-program event timeline (aggregates 6 sibling services) |
| `go-fleet-backoff-coordinator` | 18163 | [0013](docs/adr/0013-response-driven-backoff-primitive.md) | response-driven backoff (429-Retry-After / 5xx / circuit-break) |
| `go-fleet-budget-tracker`    | 18164 | [0014](docs/adr/0014-per-program-scan-budget-cap.md) | atomic per-program scan-cost cap (race-free check-and-insert) |
| `go-fleet-selftest-aggregator` | 18165 | [0015](docs/adr/0015-fleet-selftest-aggregator.md) | hourly poll of every fleet service's `/selftest` |
| `go-fleet-schema-validator`  | 18166 | [0016](docs/adr/0016-fleet-schema-validator.md) | JSON Schema catalog + validator (8 baseline schemas seeded) |
| `go-fleet-vendor-disclosure-tracker` | 18167 | [0017](docs/adr/0017-vendor-disclosure-history-tracker.md) | vendor disclosure history (PII-redacted) — pairs with leak-bounty-policy |
| `go-fleet-sandbox-targets`   | 18168 | [0018](docs/adr/0018-fleet-sandbox-targets.md) | **INTERNAL-ONLY** deliberately-vulnerable apps for scanner integration tests |
| `go-fleet-priority-queue`    | 18169 | [0019](docs/adr/0019-fleet-priority-queue.md) | composite-scored findings ranker (severity × payoff × age × dedup) |
| `go-fleet-webhook-verifier`  | 18170 | [0020](docs/adr/0020-fleet-webhook-verifier.md) | inbound webhook signature verifier (6 platforms, constant-time compare) |
| `go-fleet-target-reputation` | 18171 | [0021](docs/adr/0021-fleet-target-reputation.md) | target reputation lookup (5 sources, local-flag short-circuit) |
| `go-fleet-content-normalizer` | 18172 | [0022](docs/adr/0022-fleet-content-normalizer.md) | MIME/charset/gzip/brotli normalizer (5MB cap, gzip-bomb defense) |

**Conventions for primitives**:
- Mesh `0exec`, kind `container`, language `go`, runtime `compose`.
- TRL 6 at ship, ceiling 7 or 8 (see per-ADR `Decision` section).
- Every primitive ships with `/selftest` + `INTEGRATIONS.md` (consumer-side guidance).
- Composition pattern: caller reads `<PRIMITIVE>_URL` env var, fails open with `degraded: ["<primitive>-down"]` in response on outage.
- When adding a 21st primitive: write the ADR FIRST (`docs/adr/00XX-<slug>.md`), get sign-off, then build. See ADR-0001 and ADR-0002 for the process.

For decisions older than the primitives tier (BEGIN IMMEDIATE pattern, /metrics middleware short-circuit, compose-tag pinning, etc.), grep this file's "Lessons & gotchas" section + commit history. Future decisions go in ADRs.

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

## Renames — `renames.json`

When a service's canonical id (and therefore its URL) has to change —
operator preference, vendor mark, a typo we slept on, a `go-` prefix added
for consistency — write a row to [`renames.json`](renames.json) instead of
just editing `slug.json` silently. Schema:
[`schema/renames.v1.json`](schema/renames.v1.json).

Why bother:

- **No surprise 404s.** The new entry in `services.json` auto-carries
  `aliases` (old slugs) and `alias_urls` (old hostnames). Any consumer that
  searched `services.ids.json` for the old name finds it via `aliases` and
  resolves to the new id.
- **Graceful URL deprecation.** `fleet-runner nginx-render` reads the log
  and emits a 301-redirect vhost from each `alias_url` → `url` for the
  lifetime of the rename (`status: redirect`, `retire_at: <date>`). Bookmarks
  keep working until the retirement date passes.
- **Caller-aware retirement.** `fleet-runner audit-callers --include-aliases`
  surfaces any service still hitting an `alias_url` during the redirect
  window — so by `retire_at` you've patched every internal caller off the
  old URL, and the 301 vhost can be torn down safely.
- **Audit trail.** `reason` + `renamed_by` + `renamed_at` survive in git
  history, so six months from now nobody has to reconstruct *why* a service
  is at `go-foo.0exec.com` when the GH repo is `go_foo`.

Workflow:

```
1. Decide new id. Update slug.json:
     "go_oauth_mapper": "go-oauth-mapper"
2. Append to renames.json: { from_id, to_id, from_url, to_url,
     renamed_at, retire_at (default +30d), reason, status: "redirect" }.
3. Run bin/generate.py — services.json now carries `aliases`/`alias_urls`
     on the new entry.
4. Commit + push registry. Live state is still on the old URL — nothing
     broke yet.
5. fleet-runner deploy <new-id> --bootstrap   (mints new DNS + cert,
     creates new container/dir, leaves old serving until step 6).
6. fleet-runner nginx-render                  (rewrites old vhost as a
     301 redirect to the new URL; new vhost serves the live container).
7. After retire_at: fleet-runner audit renames-active flags entries
     whose retire_at is past. Confirm no callers via audit-callers,
     then PR to renames.json: status: "retired" (the 301 vhost is
     re-rendered to a 410 Gone, then dropped on the next nginx-render).
```

Status semantics in `renames.json`:

| `status`   | What nginx-render emits for `from_url`                          |
|------------|-----------------------------------------------------------------|
| `redirect` | 301 → `to_url`, preserving path + query. Cert auto-renewed.      |
| `retired`  | 410 Gone + `Deprecation` header. Cert still served (until DNS removed). |
| `blocked`  | 410 Gone + no redirect (use this when the old hostname is now hostile, e.g. compromised). |

Chained renames (A → B → C) get **two** rows, not one flattened entry —
generate.py walks the chain so the C entry carries `aliases: [A, B]` and
`alias_urls: [A_url, B_url]`. Don't fold the chain manually; the per-step
record carries `renamed_at` / `reason` for each hop.

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

### 6. Active-scanner egress must route through the residential proxy

**Hetzner is a bullseye for DC-abuse complaints when scanners hit
bug-bounty targets.** German hosting providers escalate quickly: a
single complaint can pull the dockerhost IP. We use Webshare
rotating residential proxies for every scanner that actively probes
external (non-fleet) targets. The pattern:

1. Webshare creds live in `/opt/_shared/proxy.env` on the dockerhost
   (chmod 600, root-owned). Single source of truth — never duplicate
   into per-service `.env` files in service repos.
2. Each active-scanner repo's `docker-compose.yml` declares (in the
   `environment:` block):
   ```yaml
   - HTTP_PROXY=${EXTERNAL_PROXY_URL:-}
   - HTTPS_PROXY=${EXTERNAL_PROXY_URL:-}
   - NO_PROXY=${NO_PROXY:-localhost,127.0.0.1,.0exec.com,.0crawl.com}
   ```
   The `:-` default = direct egress, so an unconfigured workstation
   doesn't accidentally route through a missing proxy.
3. On the dockerhost, the per-service compose dir gets an `.env`
   that's a copy of `/opt/_shared/proxy.env` (or a bootstrap script
   that merges it). docker-compose reads `.env` adjacent to the
   compose file by default, populating `${EXTERNAL_PROXY_URL}`.
4. **Go services use safehttp ≥ go-common v0.14.3.** That release
   added `Proxy: http.ProxyFromEnvironment` to the safehttp
   transport — pre-v0.14.3 the env vars were silently bypassed.

**Verification recipe:**

```
# inside the container
env | grep -E '^HTTPS_PROXY=' # must show http://...:...@p.webshare.io:80

# end-to-end through the gateway
curl -s 'https://<scanner>.0exec.com/probe?url=https://httpbin.org/ip&api_key=default_token'
# → "origin": some Webshare residential IP, NOT 176.x.x.x (Hetzner)
```

**Which services need this:** any service that actively probes
*external bug-bounty targets* (httpx, takeover-checker, screenshot,
http-replay, exploit-verifier, nuclei wrappers, etc.). Services that
only hit *public APIs* (subfinder calling crt.sh / hackertarget /
anubis / wayback) DO NOT need the proxy — those endpoints are
designed to be hit and don't generate abuse complaints. Services
that only do *internal* fleet calls (orchestrators, queue workers,
findings-store, etc.) also don't need it.

**Lesson:** when an entire fleet egresses from one DC IP, the
egress-routing decision is a fleet-level invariant, not a per-call
choice. Wire it in compose + go-common defaults, not in each
scanner's HTTP-client construction code.

### 7. Stale Docker embedded-DNS forwarders after a dockerd restart

When `dockerd` restarts on the dockerhost while user-defined-network
(compose-created) containers are still running, those containers keep
the embedded DNS forwarder upstream config they cached at *their own*
container-start time. Docker never re-reads `/etc/resolv.conf` for
existing containers. If the host's resolv.conf changed in the meantime
(e.g. systemd-resolved was replaced by dnsmasq, or the daemon
transiently saw an empty resolv.conf during the restart), the
container's `/etc/resolv.conf` ends up with one of:

```
# ExtServers: [invalid IP invalid IP invalid IP]
# ExtServers: [host(127.0.0.53)]   # systemd-resolved no longer up
```

Result: every external DNS query inside that container `SERVFAIL`s
(`server misbehaving`). The dockerd `dns:` setting is *not* re-read —
the embedded forwarder has its own snapshot.

**Fleet symptom (observed 2026-05-15):** every recon scanner
(subfinder, takeover-checker, httpx, cert-transparency) returned
empty/zero hits for ~24 hours. Subfinder showed `count: 0` per
source, takeover-checker returned `severity: none` for 129/129 hosts.
At first glance both responses *looked* clean — that's the
false-negative class.

**Detection:**

```
fleet-runner audit dns-forwarders-stale --workdir /root/workspace
```

The check is anchored on `services-registry`; `pass=1 fail=0
skip=N` means the fleet is healthy. A `fail` row lists up to 5
broken container names plus the canonical fix command.

**Recovery:**

```
fleet-runner dns-heal           # dry-run report
fleet-runner dns-heal --apply   # restart every broken container (~10s each)
```

`dns-heal` SSHes to the dockerhost, parses each running container's
ExtServers annotation, and `docker restart -t 5`s any whose upstream
list doesn't match the dockerd `dns:` config (the fleet's internal
DNS triple — full topology in private `fleet-state/OPS.md`). Restart
forces Docker to re-read the host's current resolv.conf and rewrite
the forwarder upstream.

**Defense in depth — service-level detection.** Every recon scanner
that depends on outbound DNS now exposes `GET /selftest`, which
resolves a known control hostname (`example.com`) and returns `503`
when the local resolver is broken. External monitors can hit
`/selftest` to detect the false-negative-producing condition without
needing to interpret a "0 hits" run as either "clean negative" or
"resolver dead".

Affected scanners with `/selftest` shipped 2026-05-16: `go-pentest-
takeover-checker` (v0.2+), `go-pentest-subfinder` (v0.2+). Pattern
to propagate when adding a new outbound-DNS service: implement
`/selftest` against `example.com`, and emit a distinct `severity` /
top-level flag (`error`, `all_errored: true`, etc.) for resolver
failures so callers cannot silently absorb them as "no findings".

**Lesson:** when an external dependency can break silently in a way
that mimics a clean negative response, the contract has to surface
the difference. The fix is in three layers: (a) per-service `/selftest`
control probes; (b) per-service response shape distinguishes "no
signal" from "couldn't probe"; (c) fleet-wide `audit dns-forwarders-
stale` + `dns-heal --apply` so an operator catches the underlying
condition before scans rely on it.

### 8. Capability gaps belong in a findings JSON, not chat history

When a session-running agent notices a fleet service is missing a
needed capability, the discovery is worthless if it lives only in
the chat transcript. Pattern:

  1. Append a `fleet_gap` record to a findings JSON (any path; the
     tools take a path argument).
  2. If the fix is mechanical and you can write the
     `patch_unified_diff`, set `auto_apply: true` and let
     `bin/autofix.py --apply` ship it (clone → apply → tests →
     push → deploy → /selftest → rollback on fail).
  3. If the fix is design-needed (no patch, or feature-sized), let
     `bin/disclose.py --apply` file the issue on the right repo —
     the next agent picks it up cold.

Both tools live in
[`baditaflorin/go-pentest-leak-bounty-policy/bin/`](https://github.com/baditaflorin/go-pentest-leak-bounty-policy/tree/main/bin)
and run on any workstation with `gh` authed. Full recipe in the
canonical CLAUDE.md propagated to every fleet repo
("Recipe — Closing a capability gap").

**Lesson:** undisclosed gaps re-rediscover themselves a month later
in a different session. The cost of the autofix/disclose loop is
~30 lines per gap; the cost of re-discovery is hours of confused
spelunking. Always file before moving on.

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
