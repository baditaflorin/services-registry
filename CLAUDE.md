# Fleet context — drop-in brief for AI agents

This file is the **canonical** cold-start brief for any AI agent
working inside a baditaflorin fleet service repo. It is maintained in
`services-registry/CLAUDE.md` (the registry is the catalog) and
propagated to every fleet repo via `fleet-runner inject`.

If you find a stale copy that differs from this one, the registry copy
wins — refresh and re-propagate, don't fork it.

Per-service specifics (port, mesh, slug, version, category) live in
the repo's own `service.yaml` + `deploy.yaml` + `README.md`. This file
is intentionally generic — it explains the *fleet*, not any one
service.

**Building a new service?** See
[`services-registry/SERVICE-TEMPLATE.md`](SERVICE-TEMPLATE.md) — the
canonical per-service scaffold (file-by-file templates for `main.go`,
`service.yaml`, `Dockerfile`, etc., plus a paste-ready cold-start
prompt you can feed Claude / ChatGPT / Gemini). Propagated to every
fleet repo next to this file.

## Fleet at a glance

~220 service repos under `github.com/baditaflorin/*`, organised into
three meshes. Each repo declares its mesh via a GitHub topic
(`mesh-0exec` / `mesh-0crawl` / `mesh-pages`) and its category via
`category-<x>`. The canonical catalog is
`services-registry/services.json`; the canonical conventions doc is
`services-registry/FLEET.md` — **read it first** for any fleet-wide
task.

| Mesh         | Domain pattern         | Auth                                       | Used for                              |
|--------------|------------------------|--------------------------------------------|---------------------------------------|
| `mesh-0exec` | `<slug>.0exec.com`     | `?api_key=…` or `X-API-Key` header         | proxy, search, ocr, security          |
| `mesh-0crawl`| `<slug>.0crawl.com`    | path token `/t/<token>/…`                  | domains, recon, web-analysis          |
| `mesh-pages` | static / *.github.io   | none                                       | dashboards, catalogs                  |

Look at `service.yaml` in this repo to see which mesh applies.

## TRL — technology readiness level

Every `services.json` entry may carry a `trl` field 1–9:

| TRL | Band         | Meaning                                                              |
|-----|--------------|----------------------------------------------------------------------|
| 1–3 | toy          | single regex / no tests. Don't depend on it.                         |
| 4–5 | developing   | curated lists, multi-step logic, partial tests.                      |
| 6–7 | real         | RFC-compliant parsing, evidence trails, real test coverage.          |
| 8–9 | production   | battle-tested, cross-checks, SLA-grade.                              |

`trl_ceiling` marks services that **structurally cannot** advance
further (e.g. needs a browser engine, needs paid threat intel).
`trl_assessed_at` older than ~90 days is stale — re-audit.

## Key sibling repos

| Repo                  | Role                                                                           | Visibility |
|-----------------------|--------------------------------------------------------------------------------|------------|
| `services-registry`   | canonical catalog (services.json + FLEET.md + this file)                       | PUBLIC     |
| `go-common`           | shared Go lib — SSRF-safe HTTP, jsbundle recovery, **apikey client**, ua, middleware | PUBLIC |
| `go-apikey-service`   | **the keystore** — issues/verifies/revokes API keys for `mesh-0exec`           | varies     |
| `go-catalog-service`  | renders services.json into `catalog.0exec.com`                                 | PRIVATE    |
| `go_fleet_runner`     | CLI to operate the fleet (`health`, `smoke`, `inject`, `push`, …)              | PRIVATE    |
| `0crawl-platform`     | nginx vhost templates (also embedded in fleet-runner)                          | PRIVATE    |
| `fleet-state`         | live operational state, runbooks, SSH topology                                 | PRIVATE    |

## Auth — how `mesh-0exec` actually authenticates (`go-apikey-service`)

**The keystore is the fleet's single point of compromise.** Treat it
like a CA root: every `0exec` service trusts whatever it says. If
this repo is on `mesh-0crawl` or `mesh-pages`, the keystore does not
apply — skip this section.

Request flow when a caller hits `https://<slug>.0exec.com/...?api_key=<k>`:

1. **nginx vhost** runs an `auth_request` to its `_verify_key` location.
2. **Static fallback** — if `<k>` matches the universal demo key
   hardcoded into the vhost, accept immediately. Survives keystore
   outages for the public demo path.
3. Otherwise nginx POSTs `X-Verify-Key: <k>` to the keystore's `/verify`.
4. Keystore checks SQLite → returns 200 + `X-Auth-User` / `X-Auth-Scope`,
   or 401.
5. On 200, nginx forwards the original request to the service
   container, with `X-Auth-*` headers populated.

**Services do not call the keystore themselves** — nginx already gated
the request. Trust the gateway-injected `X-Auth-*` headers. If you
genuinely need verification inside a service (admin tooling, internal
RPC), use the canonical clients — never handroll HTTP calls:

```go
// Middleware (preferred — gateway header fast-path + keystore fallback + Cache + fail-closed 503):
import "github.com/baditaflorin/go-common/middleware"   // ≥ v0.7.0
// Direct client (only for non-HTTP-handler code):
import "github.com/baditaflorin/go-common/apikey"
c := apikey.New() // reads APIKEY_SERVICE_URL + APIKEY_SERVICE_ADMIN_TOKEN
verifier := apikey.NewCache(c) // 15-min positive cache, no negative cache
result, err := verifier.Verify(ctx, userKey)
```

Keystore outage behaviour (designed-in graceful degradation):
- **Static fallback** in nginx keeps the public demo key working.
- **`apikey.Cache`** in each service keeps recently-verified callers
  working ~15 min.
- **Snapshot data** in `fleet-state/state/snapshot.json` flags the
  keystore as BROKEN once `/health` fails — that's the alert.
- **Recovery procedure**: private `fleet-state/RUNBOOK.md` under
  "keystore outage".

The admin token (`X-Admin-Token` on `/issue`, `/revoke`, `/list`,
`/purge`) is stored as `ADMIN_TOKEN` on the keystore container and
read by clients from `APIKEY_SERVICE_ADMIN_TOKEN`. Rotation playbook:
private `fleet-state/OPS.md`.

## Auth — how `mesh-0crawl` authenticates

`/t/<token>/...` path tokens. Token validation is per-service, not
centralised. Check the repo's handler code — typically a constant
`default_token` plus a list of legitimate tokens loaded from env.

## `go-common` packages — use these, don't reinvent

| Package      | Import path                                       | Purpose                                                 |
|--------------|---------------------------------------------------|---------------------------------------------------------|
| safehttp     | `github.com/baditaflorin/go-common/safehttp`      | SSRF-safe HTTP client, DNS-rebind guard                 |
| ua           | `github.com/baditaflorin/go-common/ua`            | Standard User-Agent builder                             |
| jsbundle     | `github.com/baditaflorin/go-common/jsbundle`      | source-map recovery for scanning JS bundles             |
| apikey       | `github.com/baditaflorin/go-common/apikey`        | keystore client (`Verify`, `Cache`, admin endpoints)    |
| middleware   | `github.com/baditaflorin/go-common/middleware`    | `TokenAuthKeystore` HTTP middleware (≥ v0.7.0)          |

```go
import (
    "github.com/baditaflorin/go-common/safehttp"
    "github.com/baditaflorin/go-common/ua"
)
client := safehttp.NewClient(
    safehttp.WithTimeout(10*time.Second),
    safehttp.WithUserAgent(ua.Build(ServiceID, Version)),
)
// Errors: safehttp.ErrBlocked, safehttp.ErrInvalidScheme, safehttp.ErrMissingHost
u, err := safehttp.NormalizeURL(rawInput)
```

## Service conventions (required for fleet-runner compatibility)

- **Port**: from `PORT` env; fallback to a build-time constant; must
  match `service.yaml`, compose, and `deploy.yaml`.
- **Health**: `GET /health` → `{"status":"ok","service":"<id>","version":"<ver>"}`.
- **Version**: `GET /version` → `{"version":"<ver>"}`.
- **Metrics**: `GET /metrics` (Prometheus).
- **Gateway health**: `GET /_gw_health` is added by the nginx
  template, not by the service — don't re-implement.
- **User-Agent**: `ua.Build(ServiceID, Version)`.
- **Docker image**: `ghcr.io/baditaflorin/<id>:<version>` (no `v`
  prefix on the tag).
- **Tagging**: `git tag <version>` (no `v` prefix), e.g. `1.2.3`.
- **service.yaml** must keep: `id`, `name`, `version`, `port`,
  `category`, `health` block, `test` block.

## fleet-runner

Binary at `/usr/local/bin/fleet-runner` on **Builder LXC 108**. From
any workspace dir on that LXC:

```
fleet-runner health [--insecure]             # /health on all live services
fleet-runner smoke  [--insecure]             # GET example_url on all services
fleet-runner build-test                      # go test ./... in every workspace
fleet-runner update-dep <mod@ver>            # bump dep across all repos
fleet-runner inject <src> <dest>             # copy a file into every repo
fleet-runner exec   "<cmd>"                  # shell command in every repo
fleet-runner push   "<msg>"                  # commit+push all dirty repos
fleet-runner nginx-render                    # regenerate vhosts from templates
fleet-runner new-service <name> <port> [cat] # scaffold new service
fleet-runner stats                           # audit log + token usage summary
```

All commands accept `--tokens-used N --model NAME` for LLM accounting.

## Infrastructure topology

| Target          | SSH                                                            |
|-----------------|----------------------------------------------------------------|
| Bastion         | `ssh root@0docker.com`                                         |
| Builder LXC 108 | `ssh root@0docker.com 'pct exec 108 -- bash -lc "<cmd>"'`      |
| Dockerhost VM   | `ssh -J root@0docker.com ubuntu_vm@10.10.10.20`                |
| Webgateway      | `ssh -J root@0docker.com florin@10.10.10.10`                   |

- **Builder LXC 108** is a Proxmox container on `0docker.com`. Hosts
  per-service build workspaces at `/root/workspace/go_*/` and the
  `fleet-runner` binary.
- **Dockerhost VM** runs the service containers. Compose dirs:
  `/opt/services/<repo>/`, `/opt/security/<repo>/`,
  `/home/ubuntu_vm/pentest/<repo>/`.
- **Webgateway** runs nginx (the public TLS terminator) and the
  keystore-aware `auth_request` flow.
- Build + push: `docker buildx build --platform linux/amd64 --provenance=false -t ghcr.io/baditaflorin/<id>:<ver> --push .`

Operational topology and credentials are in **private**
`fleet-state/OPS.md` — never commit SSH targets, IPs, or tokens to
service repos.

## Operations playbook — teach yourself to fish

**For any AI agent (Claude, Gemini, Haiku, GPT-anything) that lands in
this repo and is asked to bump versions, allocate ports, or deploy.
The fleet has canonical tooling — your job is to learn to invoke it.
This section gives you the exact commands plus the manual fallback
when the canonical tool isn't reachable.**

### How to invoke `fleet-runner` from anywhere

`fleet-runner` lives on **Builder LXC 108** at `/usr/local/bin/fleet-runner`.
The LXC is a Proxmox container on `0docker.com`. From any host with SSH
access to the bastion:

```bash
# One-off invocation (works from your laptop, a CI runner, anywhere):
ssh root@0docker.com "pct exec 108 -- /usr/local/bin/fleet-runner <subcommand> [args...]"

# Examples:
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner converge'
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner allocate-port --count 1'
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner audit --all'
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner bump-version go_<repo> patch --push'
```

If you don't have SSH access to `0docker.com`, **stop and ask the user
to run the command, copy-pasting the exact line above**. Do not
substitute a different command. If you can't involve the user
(autonomous run), drop down to the "manual fallback" recipe in each
section below — but mark in your output that you used the fallback so
the user can verify nothing drifted.

#### Even shorter: install the local shim

On a fresh workstation, once SSH keys to the bastion are set up
(target identities are in private `fleet-state/OPS.md`), install
`fleet-runner-shim` as `/usr/local/bin/fleet-runner` and every recipe
below works with the bare command (drop the
`ssh "$FLEET_BASTION" 'pct exec "$FLEET_LXC" -- "$FLEET_REMOTE_BIN"'` prefix).
One-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/baditaflorin/services-registry/main/bin/fleet-runner-shim \
  | sudo tee /usr/local/bin/fleet-runner >/dev/null \
  && sudo chmod +x /usr/local/bin/fleet-runner
fleet-runner --help            # smoke test — should print the remote binary's help
```

After install, the canonical examples shorten to e.g.
`fleet-runner converge`, `fleet-runner allocate-port --count 1`,
`fleet-runner deploy go_<repo>`. The shim is dumb — it just forwards
argv over SSH to LXC 108 — so output, exit codes, and prompts behave
exactly as on the LXC. Source: [`services-registry/bin/fleet-runner-shim`](bin/fleet-runner-shim).

### Recipe — Allocating a port for a new service (or resolving a conflict)

**Canonical (preferred):**

```bash
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner allocate-port --count 1'
# Output: a single integer like 18099 — that's your host_port

# Multiple at once:
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner allocate-port --count 3'
```

**Manual fallback (when canonical isn't reachable):**

1. Open `services-registry/services.json` and find the highest
   `host_port` currently in use in the reserved range (default
   `18100–18999`).
2. Pick the next integer above the max.
3. Add an entry to `services.json` with **both** `host_port` (e.g.
   `18099`) and `container_port` (what the service binds inside its
   docker container — usually `8xxx`).
4. Verify no clash: `grep -E '"(host\|container)_port":\s*<your-pick>' services-registry/services.json` should return only your line.

**When you hit "port X is already taken" — the case Gemini got wrong:**

The registry is the truth, not the running container. Find the
squatter:

```bash
# Anyone claiming this port in the registry?
python3 -c "import json; d=json.load(open('services-registry/services.json')); print([e['id'] for e in d if e.get('container_port')==8313 or e.get('host_port')==8313])"

# Services WITHOUT a registered host_port (likely silent squatters):
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner audit registry-host-port-set'
```

If the squatter has no registry entry, **add one for it** with
`allocate-port`. Your service keeps its original port. Only reallocate
your service's port if the squatter has a legitimate registered claim.

### Recipe — Bumping a service version (atomically across all files)

**Canonical:** `fleet-runner bump-version` updates `service.yaml`, any
`const Version = "..."` in `main.go`/`version.go`, creates the git
tag, and (with `--push`) pushes commit + tag together:

```bash
# Local bump (writes files, prints next steps for review)
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner bump-version go_<repo> patch'

# Atomic bump + commit + tag + push (one-shot)
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner bump-version go_<repo> patch --push'

# Variants:  minor  /  major  /  --set 2.0.0
```

After the bump lands, the **container is still running the OLD
version** until you deploy. Pair with `fleet-runner deploy <repo>`.

**Manual fallback:**

```bash
cd /path/to/<repo>
# 1. service.yaml (preserve quoting — quoted stays quoted)
sed -i.bak 's/^version: "1.2.3"/version: "1.2.4"/' service.yaml && rm service.yaml.bak

# 2. main.go / version.go const, if present
grep -l 'const Version' *.go
sed -i.bak 's/const Version = "1.2.3"/const Version = "1.2.4"/' main.go && rm main.go.bak

# 3. Commit, tag, push, push tag — ALL FOUR (Gemini forgot step 4)
git add -A && git commit -m "chore: bump version to 1.2.4"
git tag 1.2.4              # NO leading v
git push
git push origin 1.2.4      # tags don't ride `git push` by default
```

Tag *after* the commit, push *both*.

### Recipe — Deploying a service

**Canonical (only one right answer):**

```bash
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner deploy go_<repo>'
```

`fleet-runner deploy` is idempotent end-to-end: DNS A record (Hetzner),
image build on AMD64 host (no QEMU emulation), push to GHCR, deploy
via `docker compose` on the dockerhost, ensure nginx vhost + Let's
Encrypt cert exist, `/health` smoke check. It also writes the
deployed-version metadata the catalog UI reads.

**Manual fallback (when LXC 108 is unreachable):**

If you must deploy manually, do **all** of these in order — do not skip
any:

```bash
# 1. Build on an AMD64 host (NOT on an ARM Mac — binary won't run)
docker buildx build --platform linux/amd64 --provenance=false \
  -t ghcr.io/baditaflorin/go_<repo>:<version> --push .

# 2. Roll the container forward on the dockerhost
ssh -J root@0docker.com ubuntu_vm@10.10.10.20 '
  cd /opt/services/go_<repo>/src
  git pull origin main
  sudo docker compose pull && sudo docker compose up -d
'

# 3. Update the gateway-served deployment metadata (catalog UI reads it)
ssh -J root@0docker.com florin@10.10.10.10 '
  echo "{\"sha\":\"$(git rev-parse HEAD)\",\"version\":\"<version>\",\"deployed_at\":\"$(date -u +%FT%TZ)\"}" \
    | sudo tee /etc/nginx/deploy-meta/<slug>.0exec.com.json
  sudo nginx -s reload
'

# 4. Smoke test
curl -sSf https://<slug>.<mesh>.com/health
```

If step 3 or 4 fails, the deploy is incomplete even though the
container is running. Don't declare done until both succeed.

### Recipe — Self-check before declaring "done"

Three commands. Run all three. If anything in the category you touched
is flagged, fix it before stopping:

```bash
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner converge'
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner audit --all'
ssh root@0docker.com 'pct exec 108 -- /usr/local/bin/fleet-runner state snapshot'
```

### Anti-patterns — observed in prior agent sessions

1. **"Port 8313 is taken, I'll pick 8500 and edit `service.yaml`."** Use
   `fleet-runner allocate-port` and register the squatter. See "Allocating
   a port" above.

2. **"I bumped the version in `service.yaml` and pushed."** Did you tag
   git AND push the tag AND update the docker image tag? Use
   `fleet-runner bump-version --push`.

3. **"All repos are pushed to origin/main."** Pushing code ≠ deploying.
   The container is on the old image until `fleet-runner deploy`
   (or the manual fallback) runs.

4. **"I edited `service.yaml` port from 8313 to 8500 to avoid conflict."**
   Silent multi-file drift. `fleet-runner audit port-matches-registry`
   catches this. Don't.

5. **"I ran `git tag X.Y.Z`."** Did you `git push origin X.Y.Z`? Tags
   don't ride `git push` by default.

6. **"`fleet-runner` isn't working for me, I'll use a different deploy
   path."** Stop. Either report the exact command + error to the user,
   or use the manual fallback recipe above and **say so** in your
   summary so the user can verify the catalog-meta step landed.

## Fleet-wide changes — change `go-common`, not consumers

The cardinal rule when you'd otherwise touch every service: **modify
the library and bump the dep.** A `go-common` patch plus
`fleet-runner update-dep github.com/baditaflorin/go-common@vX.Y.Z`
beats 130 PRs.

## Local workflow

- Local workspace root: `/Users/live/Documents/Codex/2026-05-08/`.
  Sibling repos sit next to this one — read them directly when you
  need to understand a dependency.
- CI: there is none. Husky pre-commit hooks + local `npm run smoke`
  (Node repos) or `go test ./...` (Go repos) are the gate. Don't
  scaffold GitHub Actions build workflows.
- Supply chain: prefer npm packages ≥ 3 days old over `@latest` —
  accept known CVEs over zero-day supply-chain injection.
