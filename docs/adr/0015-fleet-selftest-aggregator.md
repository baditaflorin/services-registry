# ADR-0015 — Canonical `/selftest` aggregator

**Status:** accepted, 2026-05-16
**Owner:** baditaflorin
**Service:** `go-fleet-selftest-aggregator` (port 18165, mesh-0exec,
category `fleet-infra`)

## Context

After batches 1-3 of `/selftest` rollout (~50 fleet services), every
container service exposes a `GET /selftest` endpoint that exercises
its real dependencies — resolver, upstream APIs, embedded data,
keystore, etc. `/health` only proves the binary booted; `/selftest`
proves the patched code path actually works.

The motivating example lives in
[services-registry/FLEET.md gotcha #7](../../services-registry/FLEET.md)
("Stale Docker embedded-DNS forwarders after a dockerd restart") —
every recon scanner returned empty/zero hits for ~24 hours, and the
empty responses looked clean. After that incident, every outbound-DNS
service grew a `/selftest` that resolves a control hostname and returns
`503` when the local resolver is broken. External monitors can hit
`/selftest` to detect the false-negative-producing condition without
needing to interpret a "0 hits" run as either "clean negative" or
"resolver dead".

`fleet-runner smoke` already queries every service one-by-one and
prints pass/fail to the operator's terminal. The deploy pipeline's
smoke gate (step 8 of the deploy recipe in `services-registry/CLAUDE.md`)
hits `/selftest` against the single service being deployed. **What
neither tool does** is compose every result into a fleet-wide pass/fail
matrix that a dashboard, an on-call shift, or another agent can read
without spinning up its own N-of-N fan-out.

The data exists. The aggregator didn't.

## Decision

Build `go-fleet-selftest-aggregator` as the canonical aggregator:

- **Hourly background poll** (configurable via `POLL_INTERVAL`)
  against `services.urls.json`, with 8-way concurrency and a 10s
  per-probe deadline (`POLL_CONCURRENCY` / `POLL_TIMEOUT`). `kind:
  static` Pages entries are skipped — they have no `/selftest`.
- **Status taxonomy**:
  - `pass`        — HTTP 2xx
  - `skip`        — HTTP 404 (service hasn't implemented `/selftest`
    yet; matches the deploy smoke gate convention)
  - `fail`        — HTTP 5xx (canonical: 503 = "internal sources
    errored")
  - `unreachable` — network / timeout / TLS / non-HTTP error
- **SSR `/board`** — pure server-rendered HTML, no JS, grouped by
  registry `category` (domains, recon, fleet-infra, …). `html/template`
  auto-escapes every action, so a hostile upstream returning
  `<script>alert("xss")</script>` in its body can't break out of the
  cell. `TestBoard_HTMLEscapes` is the regression gate.
- **`/board.json`** — structured form for non-browser consumers
  (dashboard, oncall pager, agent triage). Same contract as the deploy
  smoke gate: `{generated_at, services: [{id, url, status,
  duration_ms, body_first_200?}], tally}`.
- **SQLite trend store** — `runs(id INTEGER PK, ts, service_id,
  status, duration_ms, body_first_200)` with `(service_id, ts)`
  index, 30-day retention pruned on each write. Pinned-connection
  `BEGIN IMMEDIATE` writes (same pattern as `go-fleet-dns-sync`'s
  reconcile actions) so the poll-write and `/history` read paths
  can't deadlock on lock upgrade.
- **`/history?service=ID&since=RFC3339`** — per-service timeline, up
  to 1000 rows / call.
- **`/selftest`** — synthesises an in-process 3-service registry
  (one pass, one fail, one unreachable), runs the poller, asserts
  the tally. Returns 200 on the expected mix, 503 otherwise. This is
  the same shape every other fleet `/selftest` uses, so the deploy
  smoke gate consumes it without special-casing.
- **safehttp for the polling client** — SSRF defense + canonical UA
  + honors compose-injected `HTTP_PROXY` env (relevant for any
  scanner that needs egress via the Webshare residential proxy).

The aggregator does **not** issue alerts. It surfaces state — a
dashboard or pager wraps `/board.json` if that's wanted. Keeping the
aggregator alert-free preserves a clean separation: one service
collects, others react.

## Consequences

**Positive**

- **Visibility.** `/selftest` coverage and breakage are visible at a
  glance instead of being scattered across N dashboards.
- **One-stop integration.** Consumers (dashboard, on-call, an agent
  triaging "is the fleet healthy?") fetch one URL — `GET /board.json`
  — instead of writing their own fan-out.
- **Pressure on laggards.** A `skip` count on the headline tally
  makes services that haven't implemented `/selftest` yet visibly
  laggard; the next agent touching one of those repos has obvious
  motivation to land the missing handler.
- **Trend data.** SQLite history lets an operator answer "when did
  `subfinder` start failing?" in one query instead of grepping
  `fleet-runner smoke` logs.
- **No new alert surface.** The aggregator surfaces state; alerting
  decisions stay in dashboards / pager configs / external SLO tools.

**Negative**

- **Hourly latency.** A service that breaks 5 minutes after a poll
  isn't visible on `/board` for up to 59 more minutes. Acceptable
  for a baseline coverage tool — operators with a tighter SLO target
  should still wire their own per-service alerting on top of the
  `/board.json` shape.
- **Storage growth.** Bounded by `RETENTION_DAYS * services * 24`
  rows ≈ 30 * 220 * 24 ≈ 160 k rows. Negligible at SQLite's scale.
- **Polling-volume floor.** 220 services every hour = 5280 probes/day
  flowing through the gateway as `?api_key=default_token` traffic.
  At the gateway's 1 req/s default-token rate limit per IP this fits,
  but worth noting for any future cadence tightening.

## Migration

- **Consumers** point at `GET /board.json` directly (dashboard, oncall
  pager, agent triage). No client library needed; the JSON shape is
  the API.
- **`fleet-runner smoke`** stays useful for the "run all probes
  *right now* and print to my terminal" path. The aggregator gives
  the "what was the state at the last hourly snapshot" view; they're
  complementary, not redundant.
- **Deploy smoke gate** (step 8 of the deploy recipe) is unchanged —
  it hits `/selftest` directly against the single service being
  deployed, bypassing the aggregator's hourly lag.

## Top 3 consumers

1. **`go-catalog-service`** (`catalog.0exec.com`) — surface a green/red
   pip per service in the catalog table, fed by `GET /board.json`. One
   fetch on render, no N-of-N fan-out.
2. **`hub_scrapetheworld_org`** — fleet dashboard adds a "selftest
   coverage" tile reading the same `/board.json`; on-call shift sees
   `pass / fail / skip / unreachable` totals at a glance.
3. **`bin/autofix.py`** / **`bin/disclose.py`** in
   `go-pentest-leak-bounty-policy` — when an agent files a fleet_gap,
   query `/history?service=X&since=...` to confirm the failure is
   sustained (not a one-tick flake) before opening a disclosure issue.
