# ADR-0014 — Per-program scan-cost budget cap

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-session-2026-05-16
* **Tags**: fleet-infra, cost-control, abuse-prevention, mesh-0exec

## Context

The fleet runs continuous-monitor programs that fan out scans across
hundreds of domains on a cron-driven loop. None of them have a
per-program ceiling: if a config bug widens the target list (a
wildcard scope, a stale dedupe key, a regression in the dedupe
clock-skew), the monitor can launch unbounded scans against fleet
upstreams and out-of-fleet targets indefinitely. The two failure
modes we've already paid for once each:

1. **Egress quota exhaustion** — Hetzner outbound bandwidth caps; a
   misconfigured continuous-monitor burns the month's quota in hours.
2. **Hetzner abuse complaints** — a runaway scan against a single
   downstream looks indistinguishable from a DoS to the receiver,
   who files an abuse ticket. Each one takes a human cycle to answer.

Today every program tracks "did I scan this target recently?" in its
own SQLite, but nothing aggregates a TOTAL per-program work budget.
The duplication problem is real but cosmetic. The runaway problem is
operational.

## Decision

Ship `go-fleet-budget-tracker` as a canonical fleet primitive on
mesh-0exec, port 18164, slug `fleet-budget-tracker`.

API shape:

| Method + path                | Caller                | Semantics                                                                |
|------------------------------|-----------------------|--------------------------------------------------------------------------|
| `POST /spend`                | any continuous program | Atomic check-and-insert. Returns `{accepted, remaining, cap_reset_at, reason?}`. On `accepted=false` the row is NOT written; the caller decides what to do (skip, queue, alert). |
| `GET  /budget/{program}`     | dashboards / ops      | `{caps, used (daily, weekly), by_scanner, reset_at}` snapshot.           |
| `POST /caps/{program}`       | admin only            | Upsert daily / weekly caps. `0` = unbounded for that window.             |
| `GET  /caps/{program}`       | any                   | Read current caps.                                                       |

Storage: SQLite on a single dockerhost node, WAL journal mode. The
canonical race-safety primitive is `BEGIN IMMEDIATE` on a pinned
`*sql.Conn`: the check-and-insert in `Spend()` runs inside one
transaction so two callers racing to push the same program over cap
serialize. Exactly the cap-worth of units lands; every other caller
gets `accepted=false, reason="over-cap"` and writes nothing. The
50-goroutine race test (`TestSpend_ConcurrentRace`) is the canonical
regression: cap=10, fifty goroutines each push 1 unit, must see
exactly 10 accepted, 40 rejected, and `used.daily == 10` (not 50).

Units are opaque — the tracker does not interpret "1 unit". Programs
that want byte-accounting set `units = kilobytes`; programs that want
request-counting set `units = 1` per request. The cap and the spend
must agree on the unit; the tracker just sums and compares.

## Consequences

**Positive**

- A misconfigured continuous-monitor stops at its program cap instead
  of burning the fleet's egress quota.
- One canonical place to see "how much has this program spent today /
  this week"; ad-hoc grafana queries against per-program SQLites
  retired.
- `/selftest` exercises set-cap → accept → reject → no-insert against
  an in-memory store, so the deploy pipeline's smoke gate catches a
  regression in the atomicity primitive before it reaches prod.

**Negative**

- One more hop in every continuous-monitor's inner loop. Mitigated
  by the loop being I/O-bound on the actual scan, not the budget
  call; expected p50 latency for `/spend` is sub-millisecond on
  the SQLite hot path.
- Single-node SQLite. A multi-region fleet would need to redesign
  this on a consensus-backed kv. Today's fleet is single-region, so
  acceptable; TRL ceiling marked at 7 with this rationale.

**Mitigations**

- Caller fail-open (see migration path). A tracker outage degrades
  to "no cap enforcement", not "no scans run". The trade-off is
  intentional: a budget-tracker outage must not stall the fleet.
- Backups: SQLite file on a docker volume; standard fleet snapshot
  cadence applies. Loss of the spend log = loss of historical
  audit, not loss of correctness (caps re-apply from the next
  window boundary).

## Migration path (service ADRs)

Consumers read one env var:

```
BUDGET_TRACKER_URL=http://go-fleet-budget-tracker:18164
```

Recommended client shape (fail-open by design):

```go
url := os.Getenv("BUDGET_TRACKER_URL")
if url == "" {
    proceed = true // not configured ⇒ accept-without-tracking
} else {
    res, err := budget.Spend(ctx, program, scanner, units)
    if err != nil {
        proceed = true // tracker down ⇒ accept-without-tracking (degraded)
    } else {
        proceed = res.Accepted
    }
}
```

A tracker outage does NOT block scans. That is the explicit trade-off:
better to let a real program drift past cap during an outage than to
stall every continuous-monitor when the tracker hiccups.

Caps are set out-of-band by the operator via `POST /caps/{program}`
with the admin token (env `BUDGET_ADMIN_TOKEN` on the tracker
container). No code change is needed to add a new program — calling
`/spend` with a never-before-seen `program` is well-defined and just
runs unbounded until someone sets a cap.

## Alternatives considered

1. **Add per-program counters to every continuous-monitor.** Every
   service grows its own SQLite tally and its own cap config. Five
   copies of the same primitive, drift between them, no centralized
   visibility, no canonical race-test. Rejected for the same reason
   we centralize secrets and DNS sync: one canonical primitive >
   N hand-rolled ones.

2. **Hard-fail-closed when tracker is down.** Stalls every
   continuous-monitor on a tracker hiccup. The blast radius of
   "tracker hiccups" > the blast radius of "one monitor drifts past
   cap for an hour". Rejected; documented as the migration-path
   trade-off above.

3. **Token-bucket in the gateway.** nginx already does per-IP rate
   limiting, but a continuous-monitor is one process running on the
   dockerhost LAN — it would all hit nginx from the same internal
   IP. The cap we want is per-program, not per-IP. Rejected.

4. **Embed in `go-fleet-preflight`.** Preflight is a
   yes/no-can-this-deploy gate, fired once per deploy attempt. Spend
   tracking is a per-call hot path. Different cadence, different
   contention shape; merging them would muddy both. Rejected.

## References

- [ADR-0002](0002-twenty-fleet-primitives.md) — fleet primitives slate
- [`go-fleet-budget-tracker`](https://github.com/baditaflorin/go-fleet-budget-tracker)
- [`go-fleet-secrets/store.go`](https://github.com/baditaflorin/go-fleet-secrets/blob/main/store.go) — canonical `BEGIN IMMEDIATE` pattern this borrows
- `FLEET-FUTURE-TOOLS.md` Tier B #12 (the entry that scheduled this work)
