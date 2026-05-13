# Service template — drop-in brief for AI agents building a new fleet service

This file is the **canonical** "how to build a new Go service that the
fleet will pick up" guide. Maintained in
`services-registry/SERVICE-TEMPLATE.md` and propagated to every fleet
repo via `fleet-runner inject` alongside `CLAUDE.md`.

Hand this file (plus `CLAUDE.md` and `FLEET.md`) to Claude / ChatGPT /
Gemini when you want them to scaffold a new service. The cold-start
prompt at the top of this file is paste-ready.

> If you find a stale copy that differs from the one in
> `services-registry/`, the registry copy wins — refresh and
> re-propagate, don't fork it.

---

## Cold-start prompt (paste this to your AI of choice)

```
You are scaffolding a new Go service for the baditaflorin fleet.

Read these three files in order before producing any code:
  1. services-registry/CLAUDE.md         — fleet context
  2. services-registry/FLEET.md          — conventions, IaC, lessons
  3. services-registry/SERVICE-TEMPLATE.md (this file) — per-service scaffold

Then produce a complete repo for a service that does: <DESCRIBE THE SERVICE>.

Constraints:
- Mesh: <0exec | 0crawl | pages>
- Category: <proxy|search|ocr|geo|nlp|content|domains|security|recon|
             infrastructure|web_analysis|visualization|dashboard>
- Repo name: <go_something>     (kebab on disk via slug rules — see FLEET.md)
- Port: ASK ME to run `fleet-runner allocate-port --count 1`; do not pick one.
- TRL target on first ship: 4 ("developing"). Be honest in trl_evidence.
- Use go-common/{config,server,safehttp,ua,middleware,apikey}.
- No GitHub Actions build workflow. Husky + local `go test ./...` is the CI.
- No secrets in any file. Default tokens must be intentionally public.
- Image tag: ghcr.io/baditaflorin/<id>:<version>, no `v` prefix.

Emit these files exactly (skeletons are in SERVICE-TEMPLATE.md):
  main.go  handler.go  handler_test.go
  service.yaml  deploy.yaml  docker-compose.yml  Dockerfile
  go.mod  README.md  CLAUDE.md (copied from services-registry/)

Then list the registration steps the human needs to run.
```

---

## 1. Pre-flight decisions

Make these four calls **before** writing any code; they determine every
template below.

| Decision      | How to pick                                                                 |
|---------------|-----------------------------------------------------------------------------|
| **Mesh**      | Public demo / auth-free dashboard → `mesh-pages`. Path-token recon / domain analysis → `mesh-0crawl`. API-key gated tool → `mesh-0exec`. |
| **Category**  | Must be one of the enum in `schema/v1.json` (`proxy`, `search`, `ocr`, `geo`, `nlp`, `content`, `domains`, `security`, `recon`, `infrastructure`, `web_analysis`, `visualization`, `registry`, `dashboard`). Don't invent. |
| **Slug**      | Derived from repo name by the rules in `FLEET.md` §Slug rules. Don't pick by hand — let `bin/generate.py` derive it and verify the result. |
| **Host port** | `fleet-runner allocate-port --count 1` (reserved range 18100–18999). Never squat. |

The mesh determines auth, routing, and what `service.yaml.api.endpoint`
looks like:

| Mesh         | `api.endpoint`     | Auth header / param                                |
|--------------|--------------------|----------------------------------------------------|
| `mesh-0exec` | `/` (or `/v1/...`) | `?api_key=…` or `X-API-Key`                        |
| `mesh-0crawl`| `/t/{token}/`      | path token; default `default_token` for public demo |
| `mesh-pages` | `/`                | none                                               |

---

## 2. Required files

Filenames are exact. `<id>` = repo name (e.g. `go_jwt_pentest`); `<port>`
= the allocated host port; `<ver>` = `0.1.0` for first ship.

### `main.go`

```go
package main

import (
    "github.com/baditaflorin/go-common/config"
    "github.com/baditaflorin/go-common/server"
)

// Set at build time; keep in sync with service.yaml.version and git tag.
const version = "0.1.0"

func main() {
    cfg := config.Load("<id>", version)

    // mesh-0exec: gateway sets X-Auth-User; middleware trusts that header
    // and falls back to the keystore for direct hits.
    srv := server.New(cfg, server.WithKeystoreAuth("default_token"))

    // mesh-0crawl: path-token; route both shapes that nginx may forward.
    // srv := server.New(cfg)
    // srv.Mux.HandleFunc("/t/", Handler)
    // srv.Mux.HandleFunc("/<id>", Handler)

    srv.Mux.HandleFunc("/", Handler)
    srv.Start()
}
```

`server.New` already wires `/health`, `/version`, `/metrics`, RequestID,
Logging, Metrics middleware. Don't re-implement them.

### `handler.go`

```go
package main

import (
    "encoding/json"
    "net/http"

    "github.com/baditaflorin/go-common/safehttp"
    "github.com/baditaflorin/go-common/ua"
)

var httpClient = safehttp.NewClient(
    safehttp.WithUserAgent(ua.Build("<id>", version)),
)

type response struct {
    Input  string `json:"input"`
    Result any    `json:"result"`
}

func Handler(w http.ResponseWriter, r *http.Request) {
    input := r.URL.Query().Get("q")
    if input == "" {
        http.Error(w, `{"error":"missing q"}`, http.StatusBadRequest)
        return
    }
    // ... your logic; use httpClient for any outbound fetch (SSRF-safe).
    w.Header().Set("Content-Type", "application/json")
    _ = json.NewEncoder(w).Encode(response{Input: input, Result: nil})
}
```

### `handler_test.go`

```go
package main

import (
    "net/http"
    "net/http/httptest"
    "testing"
)

func TestHandler_missingInput(t *testing.T) {
    req := httptest.NewRequest(http.MethodGet, "/", nil)
    w := httptest.NewRecorder()
    Handler(w, req)
    if w.Code != http.StatusBadRequest {
        t.Fatalf("want 400, got %d", w.Code)
    }
}
```

Add real test cases as logic grows. `fleet-runner build-test` runs
`go test ./...` across every repo — keep this green.

### `service.yaml`

Single source of truth for the per-service registry entry. Used by
`bin/generate.py`, the catalog UI, and `fleet-runner` deploy/audit.

```yaml
id: <id>
name: <Human Display Name>
category: <enum value from schema/v1.json>
version: "0.1.0"
port: <host_port>           # must match docker-compose, Dockerfile, deploy.yaml
description: One-line summary of what this service does.
api:
  endpoint: /                # or /t/{token}/ for mesh-0crawl
  method: GET
  params:
    - name: q
      type: string
      required: true
      description: Input to process.
health:
  endpoint: /health
  expected_status: 200
test:
  url: /?q=example           # something that produces a real 200
  expected_status: 200
deploy:
  path: /opt/services/<id>   # /opt/security/<id> or /home/ubuntu_vm/pentest/<id> for those buckets
requires:
  - docker
build:
  platform: linux/amd64
  dockerfile: Dockerfile
registry:
  host: ghcr.io
  org: baditaflorin
  image: <id>
docker:
  network: default           # or `pentest_network` / a mesh-specific net
  restart: unless-stopped
  port: <host_port>
server:
  host: <dockerhost>            # real ssh target in private fleet-state/OPS.md   # dockerhost; see private fleet-state/OPS.md
  deploy_path: /opt/services/<id>
nginx:
  subdomain: <slug>.0exec.com   # or .0crawl.com
```

### `deploy.yaml`

Some services keep this collapsed into `service.yaml`. If you split it,
keep just the deploy-time bits:

```yaml
deploy:
  path: /opt/services/<id>
server:
  host: <dockerhost>            # real ssh target in private fleet-state/OPS.md
  deploy_path: /opt/services/<id>
nginx:
  subdomain: <slug>.0exec.com
```

### `docker-compose.yml`

```yaml
services:
  app:
    image: ghcr.io/baditaflorin/<id>:latest
    container_name: <id>-app-1
    restart: always
    ports:
      - "<host_port>:<host_port>"
    environment:
      - PORT=<host_port>
    networks:
      - default
```

### `Dockerfile`

Canonical multi-stage build. Match `golang` minor to whatever the rest
of the fleet is on (`1.24`/`1.25` are both in use; pick the same one as
neighbouring services).

```Dockerfile
FROM golang:1.25-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/<id> .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget tini \
 && addgroup -S app && adduser -S -G app app
WORKDIR /app
COPY --from=builder /out/<id> /app/<id>
COPY --from=builder /app/service.yaml /app/service.yaml
USER app
EXPOSE <host_port>
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1:${PORT:-<host_port>}/health >/dev/null || exit 1
ENTRYPOINT ["/sbin/tini","--"]
CMD ["/app/<id>"]
```

### `go.mod`

```
module github.com/baditaflorin/<id>

go 1.24

require github.com/baditaflorin/go-common v0.7.0
```

Use the latest tagged `go-common` (≥ v0.7.0 is required for the
keystore middleware). After scaffolding, run `go mod tidy` once.

### `README.md`

Aim for one screenful. Required sections:

```markdown
# <Human Display Name>

One-paragraph description of what the service does.

## Usage

curl 'https://<slug>.0exec.com/?q=example&api_key=<KEY>'
# or for mesh-0crawl:
curl 'https://<slug>.0crawl.com/t/default_token/?q=example'

Response: JSON, fields documented below.

## Development

go test ./...
docker compose up --build

## Endpoints

- GET /            — main entrypoint (params: q)
- GET /health      — liveness; {"status":"healthy","service":"<id>","version":"<ver>"}
- GET /version     — current version
- GET /metrics     — request counters
```

### `CLAUDE.md`

Copy the canonical file from `services-registry/CLAUDE.md`. Do **not**
edit per-service — `fleet-runner inject` re-syncs it from the registry.

---

## 3. Required endpoints

| Path           | Set by                            | Notes                                                             |
|----------------|-----------------------------------|-------------------------------------------------------------------|
| `GET /health`  | `go-common/server` automatically  | `{"status":"healthy","service":"<id>","version":"<ver>"}`         |
| `GET /version` | `go-common/server` automatically  | Plain version string                                              |
| `GET /metrics` | `go-common/server` automatically  | JSON request counters (Prometheus shape on roadmap)               |
| `GET /_gw_health` | nginx vhost template           | **Do not** implement; the gateway adds it                         |
| Your route(s)  | You                               | `/` for 0exec/pages; `/t/{token}/` plus `/<id>` for 0crawl        |

---

## 4. GitHub topics & registration

The catalog (`bin/generate.py`) discovers services by GitHub topic. A
repo without the right topics is **invisible** to the registry.

```bash
gh repo edit baditaflorin/<id> \
  --add-topic mesh-0exec \      # or mesh-0crawl / mesh-pages
  --add-topic category-security # any category-* matching schema/v1.json
```

Then, from the `services-registry` checkout:

```bash
python3 bin/generate.py                # rebuild services.json from topics + overrides
git add services.json services.summary.txt
git commit -m "registry: add <id>"
bin/notify-consumers.sh                # ping dashboards to refresh
```

If your service needs an `overrides.json` patch (custom description,
non-derived host_port, TRL fields), add it there — never write to
`services.json` directly.

---

## 5. Build, deploy, smoke

There is no GitHub Actions build. All builds happen on Builder LXC 108
via `fleet-runner`. From any machine with SSH access:

```bash
# On LXC 108, inside the repo workspace:
fleet-runner build-test                # go test ./...; gate before deploy
fleet-runner deploy <id>               # DNS → AMD64 build → vhost → cert → smoke
```

If you don't have LXC 108 access, **ask the user** to run those two
commands. Do **not** substitute `docker build && ssh && docker run` —
the canonical path also updates gateway vhosts, certs, and the
deployed-version metadata the catalog reads.

Image build by hand (only when fleet-runner is unavailable):

```bash
docker buildx build \
  --platform linux/amd64 --provenance=false \
  -t ghcr.io/baditaflorin/<id>:<ver> --push .
git tag <ver>          # no 'v' prefix
git push origin <ver>  # tags are NOT pushed by default
```

---

## 6. Self-check before declaring "done"

Run these on LXC 108 after deploy:

```bash
fleet-runner converge          # any drift signals?
fleet-runner audit --all       # any failed invariants?
fleet-runner state snapshot    # what's actually running
```

Plus, by eye:

- [ ] `service.yaml.port`, `docker-compose.yml` port, `Dockerfile` EXPOSE,
      and `services.json` `host_port` all match.
- [ ] Git tag and `service.yaml.version` and docker image tag all agree
      (no `v` prefix anywhere).
- [ ] `bin/generate.py` ran and `services.json` includes the new entry.
- [ ] `gh repo view baditaflorin/<id> --json topics` shows `mesh-*` + `category-*`.
- [ ] `curl https://<slug>.<mesh>.com/health` returns 200.
- [ ] `curl https://<slug>.<mesh>.com/<test.url>` returns 200 with real content.
- [ ] No secrets, no SSH targets, no real API keys checked in.

---

## 7. Anti-patterns (observed in prior AI sessions)

1. **Picking a port by hand to "avoid conflict."** Always
   `fleet-runner allocate-port`. Silent reassignment creates drift the
   registry can't fix.
2. **Editing only `service.yaml` when changing the port.** Five files
   carry the port; either update all or none.
3. **Adding a GitHub Actions build workflow.** The fleet's CI is local
   (Husky + `go test ./...`). Don't scaffold `.github/workflows/*`.
4. **Handrolling HTTP calls to the keystore.** Import
   `go-common/middleware` or `go-common/apikey` — never write
   `http.Post(".../verify", ...)` yourself.
5. **Inventing a category.** Categories are an enum in `schema/v1.json`.
   Anything else gets dropped by `bin/generate.py`.
6. **Re-implementing `/health`, `/version`, `/metrics`, `/_gw_health`.**
   `go-common/server` and the nginx template provide them. Adding a
   second handler at the same path is a bug.
7. **Pushing the docker image but not the git tag.** `git push` does
   not push tags by default — `git push origin <ver>` is required.
8. **Leaving `trl_evidence` blank or writing "production" on day one.**
   New services ship at TRL 4 ("developing") with a one-line honest
   evidence string. The catalog renders it; lying gets caught at
   re-audit.

---

## 8. Examples to imitate

| Mesh           | Repo                       | Why look at it                                          |
|----------------|----------------------------|---------------------------------------------------------|
| `mesh-0exec`   | `go-url-categorizer-api`   | Clean `safehttp` + JSON API                             |
| `mesh-0exec`   | `go_jwt_pentest`           | Path-token style with per-token handler                 |
| `mesh-0crawl`  | `go_apikey_scanner`        | Path-token recon service                                |
| `mesh-pages`   | `hub_scrapetheworld_org`   | Static dashboard consuming `services.json`             |

When in doubt, read these directly — sibling repos are right next to
yours under `/Users/live/Documents/Codex/2026-05-08/` on the local
workspace.
