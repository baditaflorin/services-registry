# Fleet Architecture — baditaflorin

> **Status:** Living document. Auto-generated sections (service counts, TRL) drift after ~30 days;
> the structural diagrams and security rationale are intended to be stable.
> Last audited: 2026-05-19.

---

## Contents

1. [What is the fleet?](#1-what-is-the-fleet)
2. [High-level map](#2-high-level-map)
3. [The four buckets](#3-the-four-buckets)
   - [Bucket A — Go Pentest Suite](#bucket-a--go-pentest-suite)
   - [Bucket B — go-common (shared library)](#bucket-b--go-common-shared-library)
   - [Bucket C — Mesh Applications](#bucket-c--mesh-applications)
   - [Bucket D — Everything else](#bucket-d--everything-else)
4. [Infrastructure layer](#4-infrastructure-layer)
5. [Public vs private — security rationale](#5-public-vs-private--security-rationale)
6. [Fleet primitives](#6-fleet-primitives)
7. [Request lifecycle](#7-request-lifecycle)
8. [Technology readiness levels](#8-technology-readiness-levels)

---

## 1. What is the fleet?

The fleet is ~420 microservices owned and operated by one engineer under the
`github.com/baditaflorin/` org. It covers three domains:

| Domain | Description |
|--------|-------------|
| **Security research** | A suite of 130+ reconnaissance, analysis, and verification tools powering a pentest pipeline |
| **Domain intelligence** | 100+ tools for passive web analysis (SEO, accessibility, tech stack, corporate identity) |
| **Mesh collaboration** | 100+ local-first peer-to-peer browser apps (games, productivity, anonymous feedback) |

Everything runs on a single dockerhost VM behind a gateway, with all secrets and
deploy automation on a private Builder LXC. The registry and most service repos are
**public on GitHub** — why this is not a security risk is explained in §5.

**By the numbers (May 2026):**

| Metric | Value |
|--------|-------|
| Total services registered | ~423 |
| Container services (deployed) | ~162 |
| Static / GitHub Pages sites | ~63 |
| Mesh collaboration apps | 100+ |
| Go service repos | 154 |
| Shared by go-common | 200+ consumers |
| Fleet primitives (go-fleet-*) | 20 |

---

## 2. High-level map

```
╔══════════════════════════════════════════════════════════════════════╗
║                        PUBLIC INTERNET                               ║
║                                                                      ║
║  API client                  Browser user               GitHub Pages ║
║  (Bearer / X-API-Key)        (demo token)               visitor      ║
╚════════════════╤═════════════════════╤══════════════════╤═══════════╝
                 │                     │                  │
                 ▼                     ▼                  ▼
    ┌────────────────────────┐   ╔══════════════╗   ┌──────────────┐
    │   Nginx Webgateway     │   ║  mesh-pages  ║   │ GitHub Pages │
    │   (176.9.123.221)      │   ║  (static)    ║   │ (*.github.io)│
    │   TLS termination      │   ╚══════════════╝   └──────────────┘
    │   auth_request ──────────────────┐
    │   rate-limit demo key  │         │
    └──────┬─────────────────┘         │
           │                           ▼
           │               ┌───────────────────────┐
           │               │   go-apikey-service    │
           │               │   (keystore / SQLite)  │
           │               │   /verify → 200 + User │
           │               │   /issue  /revoke      │
           │               └───────────────────────┘
           │
     ┌─────┴───────────────────────────────────────────┐
     │                                                   │
     ▼                                                   ▼
┌─────────────────────────────┐       ┌─────────────────────────────┐
│        mesh-0crawl          │       │        mesh-0exec            │
│   <slug>.0crawl.com         │       │   <slug>.0exec.com           │
│                             │       │                              │
│  ┌─────────────────────┐    │       │  ┌──────────────────────┐   │
│  │  Security Tools     │    │       │  │  Fleet Management    │   │
│  │  ─────────────────  │    │       │  │  ──────────────────  │   │
│  │  xss-scanner        │    │       │  │  fleet-orchestrator  │   │
│  │  cors-scanner       │    │       │  │  fleet-visualization │   │
│  │  sql-injection      │    │       │  │  fleet-preflight     │   │
│  │  subdomain-finder   │    │       │  │  fleet-secrets       │   │
│  │  port-scanner       │    │       │  │  fleet-dns-sync      │   │
│  │  takeover-checker   │    │       │  └──────────────────────┘   │
│  │  cookie-analyzer    │    │       │                              │
│  │  waf-detect         │    │       │  ┌──────────────────────┐   │
│  │  …130+ more         │    │       │  │  Backend Services    │   │
│  └─────────────────────┘    │       │  │  ──────────────────  │   │
│                             │       │  │  js-proxy, html-proxy│   │
│  ┌─────────────────────┐    │       │  │  ocr-pdf, nlp-extract│   │
│  │  Domain Intel Tools │    │       │  │  geocoding           │   │
│  │  ─────────────────  │    │       │  │  url-categorizer-api │   │
│  │  founding-year      │    │       │  └──────────────────────┘   │
│  │  company-size       │    │       │                              │
│  │  email-extractor    │    │       │  ┌──────────────────────┐   │
│  │  tech-stack         │    │       │  │  Fleet Primitives    │   │
│  │  seo-metrics        │    │       │  │  (go-fleet-*)        │   │
│  │  cdn-detector       │    │       │  │  ──────────────────  │   │
│  │  …100+ more         │    │       │  │  call-tracer         │   │
│  └─────────────────────┘    │       │  │  backoff-coordinator │   │
└─────────────────────────────┘       │  │  resolver-quorum     │   │
                                      │  │  …20 primitives      │   │
                                      │  └──────────────────────┘   │
                                      └─────────────────────────────┘
                                                   │
                              ┌────────────────────┘
                              ▼
         ┌────────────────────────────────────────────────┐
         │             SHARED LIBRARY LAYER                │
         │           github.com/baditaflorin/go-common     │
         │                                                  │
         │  safehttp  ·  ua  ·  apikey  ·  middleware      │
         │  jsbundle  ·  selftest  ·  policyeval            │
         │  circuitbreaker  ·  backoffcoord  ·  fleetfetch  │
         │                                                  │
         │  Imported by ~200 Go service repos               │
         └────────────────────────────────────────────────┘
                              │
         ┌────────────────────┘
         ▼
┌─────────────────────────────────────────────────────────┐
│                    PRIVATE INFRA                         │
│                                                          │
│   Builder LXC 108        Dockerhost VM                   │
│   ─────────────────      ───────────────                 │
│   fleet-runner CLI        /opt/services/<repo>/          │
│   /root/workspace/        docker compose                 │
│   git worktrees           container processes            │
│                                                          │
│   Webgateway (nginx)     fleet-state (runbooks)          │
│   ─────────────────      ──────────────────────          │
│   /etc/nginx/sites-      credentials, SSH topology       │
│   enabled/*.conf         OPS.md (NEVER public)           │
└─────────────────────────────────────────────────────────┘
```

---

## 3. The four buckets

### Bucket A — Go Pentest Suite

The pentest suite is the primary *reason* the fleet exists. It is a composition of
specialized Go microservices, each doing exactly one recon or verification task,
wired together by a higher-level orchestrator.

```
                    ┌─────────────────────────────────┐
                    │   go-pentest-bounty-pilot        │
                    │   (orchestrator / entry point)   │
                    │                                  │
                    │  scope: [ "target.com", ... ]    │
                    │  budget_usd: 50                  │
                    │  phases: [ recon, scan, verify ] │
                    └──────────────┬──────────────────┘
                                   │  calls via fleet API keys
          ┌───────────────┬────────┴──────────┬───────────────┐
          ▼               ▼                   ▼               ▼
  ┌──────────────┐ ┌──────────────┐  ┌──────────────┐ ┌──────────────┐
  │  RECON       │ │  SCAN        │  │  VERIFY      │ │  EVIDENCE    │
  │  ──────────  │ │  ──────────  │  │  ──────────  │ │  ──────────  │
  │  subdomain-  │ │  xss-scanner │  │  exploit-    │ │  har-builder │
  │  finder      │ │  cors-scanner│  │  verifier    │ │  poc-curl    │
  │              │ │  sqli-tester │  │              │ │              │
  │  port-scanner│ │  csrf-detector│ │  takeover-   │ │  engagement- │
  │              │ │  waf-detect  │  │  checker     │ │  timeline    │
  │  dns-record  │ │  cookie-pwn  │  │              │ │              │
  │              │ │  tls-checker │  │  bounty-     │ │  vendor-     │
  │  cdn-detector│ │  header-check│  │  pilot final │ │  disclosure- │
  │              │ │  …           │  │  validation  │ │  tracker     │
  └──────────────┘ └──────────────┘  └──────────────┘ └──────────────┘
          │               │                   │               │
          └───────────────┴────────┬──────────┴───────────────┘
                                   ▼
                    ┌─────────────────────────────────┐
                    │   SHARED PRIMITIVES TIER         │
                    │   (go-fleet-* services)          │
                    │                                  │
                    │  resolver-quorum (2-of-3 DNS)    │
                    │  fingerprint-cache               │
                    │  payload-corpus (attack payloads)│
                    │  diff-engine                     │
                    │  call-tracer (flamegraph)         │
                    │  backoff-coordinator             │
                    │  budget-tracker                  │
                    │  selftest-aggregator             │
                    └─────────────────────────────────┘
```

**Key pentest repos (all public):**

| Repo | Mesh | Category | TRL |
|------|------|----------|-----|
| go-pentest-bounty-pilot | 0crawl | orchestration | 5–6 |
| go-pentest-waf-detect | 0crawl | fingerprinting | 6 |
| go-pentest-cookie-pwn | 0crawl | session security | 6 |
| go-pentest-takeover-checker | 0crawl | subdomain takeover | 7 |
| go-pentest-subfinder | 0crawl | recon | 6 |
| go-pentest-exploit-verifier | 0crawl | verification | 5 |
| go-pentest-nuclei | 0exec | scan engine | 5 |
| go-pentest-scope-guard | 0exec | scope enforcement | 7 |
| go-pentest-findings-store | 0exec | result storage | 6 |
| go-pentest-report-templater | 0exec | output | 5 |

---

### Bucket B — go-common (shared library)

`go-common` is the single dependency every Go service in the fleet imports.
It prevents N services from each reinventing safe HTTP, auth, logging, etc.

```
     github.com/baditaflorin/go-common
     ─────────────────────────────────
     │
     ├── safehttp/          SSRF-safe HTTP client
     │   └── NormalizeURL, ErrBlocked, ErrInvalidScheme
     │       WithTraceCollector, WithBackoffCoordinator
     │       (NOTE: only for public internet; use plain http.Client for intra-mesh)
     │
     ├── ua/                User-Agent builder
     │   └── ua.Build(ServiceID, Version)
     │
     ├── apikey/            Keystore client
     │   └── apikey.New()  (reads APIKEY_SERVICE_URL + ADMIN_TOKEN)
     │       apikey.NewCache()  (15-min positive cache)
     │       apikey.MustResolveCritical()  (fail-fast for outbound auth)
     │
     ├── middleware/        HTTP middleware
     │   └── TokenAuthKeystore  (gateway header fast-path + keystore fallback)
     │
     ├── jsbundle/          JS source-map recovery
     │
     ├── selftest/          /selftest suite (consumed by selftest-aggregator)
     │
     ├── policyeval/        In-Go rule DSL: (fact, []Rule) → decision
     │
     ├── circuitbreaker/    Per-upstream circuit breaker
     │
     ├── backoffcoord/      Response-driven backoff (talks to go-fleet-backoff-coordinator)
     │
     ├── depcheck/          Dependency healthcheck on startup
     │
     └── fleetfetch/        Thin wrapper for intra-mesh HTTP calls (plain net/http)

     Consumed by: ~200 Go container services
     Version policy: semantic; fleet-runner update-dep bumps all consumers atomically
```

**Dependency flow:**

```
                  go-common (public library)
                          │
          ┌───────────────┼────────────────┐
          │               │                │
   mesh-0crawl       mesh-0exec        go-fleet-*
   services          services          primitives
   (130+)            (29+)             (20)
          │               │                │
          └───────────────┼────────────────┘
                          │
                 all call go-common
                 at compile time
                 (not at runtime)
```

**Why go-common is safe to be public:**
- Contains no secrets, no hardcoded keys, no infrastructure addresses
- Only documents *patterns* (how to call the keystore), never *credentials*
- `apikey.New()` reads keys from env at runtime — nothing to steal from the source

---

### Bucket C — Mesh Applications

The "mesh-*" repos are 100+ local-first peer-to-peer browser applications.
They are entirely separate from the security fleet — they share only the
GitHub org and the `github.io` pages domain.

```
     mesh-* apps
     ────────────
     │
     ├── Collaboration (no central server)
     │   mesh-anonymous-qa     anonymous live Q&A
     │   mesh-applause         real-time applause meter
     │   mesh-fist-of-five     consensus voting
     │   mesh-pair-rotation    pairing roulette
     │   mesh-pomodoro-room    group timer
     │
     ├── Games (P2P WebRTC)
     │   mesh-mafia            social deduction
     │   mesh-pictionary       collaborative drawing
     │   mesh-hot-potato       timed passing game
     │   mesh-laser-tag        AR/physical game
     │
     ├── Productivity (local-first)
     │   mesh-clipboard-bridge cross-device clipboard
     │   mesh-link-share       local link sharing
     │   mesh-doorbell         presence beacon
     │
     └── Hardware (local network only)
         mesh-firefly-walk     synchronized LED
         mesh-lightning-flash  camera flash sync
         mesh-metronome        networked beat

     Runtime: GitHub Pages (kind: static)
     Auth: none — client-side only
     Data: in-browser, never leaves device or LAN
```

These apps are grouped in the fleet registry under `mesh: mesh-pages` and
`kind: static`. They are audited separately with `fleet-runner pages-audit`
and never appear in health checks or deploy pipelines for containers.

---

### Bucket D — Everything else

Repos that are in the org but don't cleanly fit the three buckets above:

```
  go-url-categorizer-api   ← this repo
  ─────────────────────────
  High-performance URL categorization with:
    LLM-backed category inference (OpenAI / Ollama)
    Vector search (pgvector / SQLite-vec)
    Prometheus metrics
    Built-in developer portal
  Mesh: mesh-0exec | Category: domains | TRL: 6

  ─────────────────────────────────────────────────

  Infrastructure / tooling repos (PUBLIC)
  ─────────────────────────
  services-registry        Catalog JSON + FLEET.md
  go-auth-middleware       Reusable auth middleware
  go-config-module         Config management
  go-configutil            Config utilities
  go-commit-messages-extractor  Git tooling
  l                        Structured logging library

  ─────────────────────────────────────────────────

  Domain intelligence suite (PUBLIC, 0crawl)
  ─────────────────────────
  go_robots_analyzer       Fetch + parse robots.txt
  go_captcha_detector      CAPTCHA detection
  go_tos_finder            Legal docs locator
  go_url_shortener         URL shortener
  go_dependency_counter    Ext dependency counter
  go_website_carbon        Carbon footprint
  go_substack_scraper      Substack metadata
  go_video_content         Video content analyzer
  go_infrastructure_fetch_cache  Shared HTTP cache (Redis+zstd)
```

---

## 4. Infrastructure layer

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                     DEPLOY PIPELINE                             │
  │                                                                 │
  │   Developer / AI agent (local or worktree)                      │
  │   │                                                             │
  │   ├─ git commit && git push  (origin/main)                      │
  │   │                                                             │
  │   └─ fleet-runner deploy <repo>  (on Builder LXC 108)          │
  │         │                                                       │
  │         ├─1. DNS / vhost / cert shape checks                    │
  │         ├─2. Drift detection  (service.yaml vs live /version)   │
  │         ├─3. Pre-flight: go build + go test in fresh worktree   │
  │         ├─4. docker buildx build --platform linux/amd64 --push  │
  │         ├─5. Pull + digest assertion (new ≠ old)                │
  │         ├─6. docker compose up -d  (dockerhost)                 │
  │         ├─7. Health-wait (poll State.Health.Status ≤ 90s)       │
  │         ├─8. Smoke gate: /health + /selftest + /version verify  │
  │         └─9. Auto-rollback on smoke fail (previous digest)      │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │                   INFRASTRUCTURE SERVICES                       │
  │                                                                 │
  │   go-fleet-secrets (18140)                                      │
  │   └─ Encrypted vault for tokens (Hetzner, GitHub PAT, SMTP)    │
  │      secrets never in env on dockerhost, never in repos         │
  │                                                                 │
  │   go-fleet-dns-sync (18141)                                     │
  │   └─ services-registry → Hetzner Cloud DNS reconciler           │
  │      30-min ticker; zone 0exec.com (id 1285812)                │
  │      gateway IP: 176.9.123.221                                  │
  │                                                                 │
  │   go-fleet-preflight (18142)                                    │
  │   └─ Pre-deploy checklist: registry + DNS + port + secrets      │
  │      200 = green; 424 = checklist of what's red                 │
  │                                                                 │
  │   go-apikey-service (keystore)                                  │
  │   └─ Issues / verifies / revokes API keys                       │
  │      SQLite backend, WAL mode                                   │
  │      All 0exec + 0crawl services trust it                       │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │                   PHYSICAL TOPOLOGY                             │
  │                                                                 │
  │   0docker.com (Proxmox host / bastion)                          │
  │   │                                                             │
  │   ├── LXC 108 (Builder)                                         │
  │   │   └─ /root/workspace/<repo>/   (canonical upstream trackers)│
  │   │      /root/wt/<repo>-<purpose>/  (AI agent worktrees)       │
  │   │      /usr/local/bin/fleet-runner                            │
  │   │                                                             │
  │   └── VM 10.10.10.20 (Dockerhost)                               │
  │       └─ /opt/services/<repo>/     (running containers)         │
  │          /opt/security/<repo>/                                  │
  │                                                                 │
  │   10.10.10.10 (Webgateway)                                      │
  │   └─ /etc/nginx/sites-enabled/*.{http,https}.conf               │
  │      (regular files, NOT symlinks)                              │
  └─────────────────────────────────────────────────────────────────┘
```

---

## 5. Public vs private — security rationale

### Summary table

| Repo / category | Visibility | Why safe to be public |
|-----------------|------------|----------------------|
| `services-registry` | **PUBLIC** | Catalog metadata only. No secrets, no credentials. Auth is obtained out-of-band. Knowing service names/ports doesn't bypass the keystore. |
| `go-common` | **PUBLIC** | Library source code. `apikey.New()` reads keys from env at runtime — nothing to steal from the code. No hardcoded endpoints. |
| All `go-pentest-*` | **PUBLIC** | Tool implementations. Open-source security tooling. No credentials embedded. Auth happens at the gateway, not in service code. |
| All `mesh-*` | **PUBLIC** | Client-side only. Data stays in-browser or on LAN. No backend stores user data. |
| All `go_*` domain tools | **PUBLIC** | Passive analysis tools operating on public data. No credentials needed to run scans on public URLs. |
| `go-url-categorizer-api` | **PUBLIC** | Application code. Secrets (LLM API keys, DB URLs) come from env, never from source. |
| `services-registry` slices | **PUBLIC** | Derived from services.json. Demo token is intentionally public and rate-limited. |
| `go-apikey-service` | **varies** | Implementation of the keystore. Kept private to reduce attack-surface knowledge, not because it holds secrets. SQLite DB is on the dockerhost — not in the repo. |
| `go_fleet_runner` | **PRIVATE** | Contains SSH target references and operational procedures. No credentials, but knowing the topology reduces friction for an attacker. |
| `0crawl-platform` | **PRIVATE** | nginx template details. Exposing precise vhost shapes aids fingerprinting. |
| `fleet-state` | **PRIVATE** | Actual credentials, SSH keys, IPs, runbooks. This is the one repo that must never be public. |
| `go-catalog-service` | **PRIVATE** | Internal catalog renderer. No credentials, but kept private for operational hygiene. |

### Why "public service code ≠ security risk"

The fleet's security model is built on these properties:

```
  WHAT IS PUBLIC               WHAT THIS EXPOSES
  ─────────────────────────    ────────────────────────────────────────
  Service source code          How the service works (open-source)
  services-registry catalog    Which services exist + their ports
  API shape (endpoints, auth   How to call a service (docs)
    header names)

  WHAT STAYS PRIVATE           WHY IT MATTERS
  ─────────────────────────    ────────────────────────────────────────
  API keys (keystore)          Can't authenticate without a valid key
  SSH topology (fleet-state)   Can't reach the dockerhost
  nginx vhost configs          Can't forge gateway behavior
  Container runtime state      Can't inspect or inject into processes

  LAYERED DEFENSES
  ──────────────────────────────────────────────────────────────────────
  1. All 0exec + 0crawl endpoints require a valid API key (keystore-gated)
  2. Demo token is rate-limited to 1 req/s / ~60 req/h per IP
  3. Dockerhost is not reachable from the internet (jump via bastion only)
  4. Service code never sees raw keys — nginx injects X-Auth-User after verify
  5. go-common/safehttp blocks SSRF — services can't be used to probe internals
  6. go-pentest-scope-guard enforces target scope — tools can't be turned
     against the fleet itself
```

The philosophy is **defense in depth, not security by obscurity**. Making
the service code and catalog public means the attack surface is well-defined
and auditable, while the actual secrets and network topology stay private.

---

## 6. Fleet primitives

These 20 `go-fleet-*` services form a shared infrastructure tier.
All pentest and domain tools call them rather than reimplementing.

```
  go-fleet-* primitives (all on mesh-0exec, ports 18153–18172)
  ────────────────────────────────────────────────────────────────

  Observation & tracing
  ├── go-fleet-call-tracer        (18161)  per-request trace + flamegraph
  ├── go-fleet-engagement-timeline (18162) event timeline aggregator
  └── go-fleet-selftest-aggregator (18165) hourly /selftest polls all services

  Reliability
  ├── go-fleet-backoff-coordinator (18163) response-driven backoff rules
  └── go-fleet-budget-tracker     (18164) per-program scan-cost cap (USD)

  DNS / Network
  └── go-fleet-resolver-quorum    (18155) 2-of-3 multi-DNS consensus

  Security data
  ├── go-fleet-fingerprint-cache  (18153) WAF/CDN classification cache
  ├── go-fleet-payload-corpus     (18156) versioned attack-payload corpus
  └── go-fleet-sandbox-targets    (18168) deliberately vulnerable test apps

  Evidence / Output
  ├── go-fleet-har-builder        (18157) HAR 1.2 evidence format
  ├── go-fleet-poc-curl           (18158) PoC curl command generator
  ├── go-fleet-diff-engine        (18160) structured diff engine
  └── go-fleet-vendor-disclosure-tracker (18167) PII-redacted vendor history

  Normalization & Validation
  ├── go-fleet-body-redactor      (18154) sensitive header/body redaction
  ├── go-fleet-tech-inferrer      (18159) composite tech-stack (83 signals)
  ├── go-fleet-content-normalizer (18172) MIME/charset/gzip/brotli normalizer
  └── go-fleet-schema-validator   (18166) JSON Schema catalog

  Coordination
  ├── go-fleet-priority-queue     (18169) findings ranker
  └── go-fleet-webhook-verifier   (18170) webhook sig verify (6 platforms)

  Reputation
  └── go-fleet-target-reputation  (18171) target reputation (5 sources)
```

---

## 7. Request lifecycle

### Authenticated API call (happy path)

```
  Client                 Nginx gateway              go-apikey-service
     │                        │                            │
     │  GET /analyze           │                            │
     │  X-API-Key: sk_xxx      │                            │
     ├───────────────────────►│                            │
     │                        │  POST /verify              │
     │                        │  X-Verify-Key: sk_xxx      │
     │                        ├───────────────────────────►│
     │                        │                            │ lookup SQLite
     │                        │  200 OK                    │
     │                        │  X-Auth-User: alice        │
     │                        │◄───────────────────────────┤
     │                        │                            │
     │                  forward request                     │
     │                  + X-Auth-User: alice               │
     │                  + X-API-Key: sk_xxx                │
     │                        │                            │
     │                        ▼                            │
     │                  Service container                   │
     │                  (sees X-Auth-User, trusts gateway)  │
     │                        │  calls go-fleet-* primitives
     │                        │  calls go-common/safehttp
     │                        │  calls peer services
     │                        │                            │
     │◄───────────────────────┤  200 {"result": ...}       │
     │                        │                            │
```

### Demo token path (rate-limited fallback)

```
  Browser                Nginx gateway
     │                        │
     │  GET /analyze           │
     │  ?api_key=demo_default  │
     │ ───────────────────────►│
     │                        │ match $default_token
     │                        │ skip keystore call
     │                        │ set X-Auth-User: demo
     │                        │ rate-limit 1 req/s / 60 req/h
     │                        │
     │                  forward to service
     │ ◄───────────────────────│  200 {"result": ...}
```

### Service-to-service (intra-mesh)

```
  pentest-bounty-pilot              xss-scanner
          │                              │
          │  plain net/http (NOT safehttp)
          │  http://xss-scanner:8080/scan
          ├─────────────────────────────►│
          │                              │ does its work
          │◄─────────────────────────────┤  200 {"findings": [...]}
          │
          │  on error / timeout:
          │  degraded = append(degraded, "xss-scanner-down")
          │  surface degraded[] in response JSON
```

---

## 8. Technology readiness levels

TRL 1–9 captures maturity per service. As of May 2026:

```
  TRL  Band         Count (approx)  What it means for consumers
  ───  ─────────    ──────────────  ──────────────────────────────────
   1   toy           ~10            Don't depend on it
   2   toy           ~15            Proof-of-concept only
   3   toy           ~20            Not tested in real engagements
   4   developing    ~40            Curated lists, partial tests
   5   developing    ~50            Multi-step logic, used in some ops
   6   real          ~30            RFC-compliant, evidence trails, CI
   7   real          ~15            Real coverage, used in prod scans
   8   production     ~5            Battle-tested, SLA-grade
   9   production     ~2            Cross-check verified, fully trusted

  TRL ceiling: services marked trl_ceiling cannot advance further without
  a browser engine (JS rendering) or paid threat-intel feeds.
  trl_assessed_at > 90 days old = stale, re-audit required.
```

### Most mature (TRL 7–8):
- `go-pentest-scope-guard` — scope enforcement, well-tested
- `go-pentest-takeover-checker` — proven in real engagements
- `go-fleet-resolver-quorum` — consensus DNS, used by all scanners
- `go-apikey-service` — keystore, fleet's single auth point

### Highest ceiling blocked:
- Any scanner requiring JS execution (needs headless browser)
- Threat-intel correlation (needs paid feeds)

---

*This document was generated from live registry data (services.minimal.json,
services.deploy.json, services.trl.json) and the CLAUDE.md fleet brief.
For operational details (SSH targets, credentials, runbooks) see the private
`fleet-state` repo.*
