# ADR-0012 — Engagement-timeline aggregator (`go-fleet-engagement-timeline`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-session-2026-05-16, baditaflorin
* **Tags**: fleet-infra, aggregator, audit, mesh-0exec, tier-A

## Context

Investigating "what happened on program X last week" today requires
querying six fleet services and stitching the results by hand:

* `asset-inventory`   → assets added
* `orchestrator`      → campaigns started / completed
* `findings-store`    → findings opened / closed
* `finding-triage`    → triage decisions (after 0.2.0 from batch 2)
* `submit-bot`        → submissions to disclosure platforms
* `payoff-tracker`    → payouts received

Every agent walk-through and every operator-led incident review
re-derives the same join, often against partially stale data — one
service was queried five minutes ago, the next three are queried
now, and the resulting "timeline" silently splices two different
moments. This is the audit gap that bites whenever an agent
recommends action on a program: the agent's view of "current
state" was assembled from snapshots taken at six unaligned
moments, and there is no single artifact a reviewer can replay to
confirm what the agent actually saw.

## Decision

Ship `go-fleet-engagement-timeline` (port 18162, mesh-0exec,
fleet-infra, ADR-0012) as the canonical per-program event
aggregator. The service owns no data of its own — it is pure
composition over the six siblings, with a 60-second in-memory
cache so the dashboard polling at 5s doesn't melt the upstreams.

### API surface

```
GET  /timeline?program=X&since=RFC3339&limit=N   →  [{ts, kind, source_service, ref_id, summary, link}, ...]
                                                    plus degraded[] listing any sibling whose pull failed
POST /push                                        →  body: Event JSON — sibling-pushed real-time event
GET  /selftest                                    →  200/503 against six in-process httptest stubs
GET  /health, /version, /metrics                  →  auto-mounted by go-common/server
```

Event vocabulary (small fixed set so consumers can switch on kind):
`asset-added`, `scan-started`, `scan-completed`, `finding-opened`,
`finding-closed`, `finding-triaged`, `finding-submitted`,
`payout-received`.

### Fail-open per upstream

A sibling that times out, returns non-200, or emits unparseable
JSON does **not** abort the aggregate. The sibling's ID lands in
the `degraded[]` array on the response, its rows are silently
dropped, and the partial timeline is returned 200. This matches
the fleet-wide pattern of "give the operator something to act on
even when half the mesh is on fire" — the alternative (5xx on any
sibling outage) would mean a single sibling deploy could blind the
dashboard for 30 seconds.

### Cache shape

Keyed on `(program, since)`. Limit is NOT part of the key — two
callers asking for the same program-since with different limits
share one upstream pull. TTL 60s. `POST /push` appends an event
into every matching cached entry and re-sorts in place, so
sibling-emitted real-time events are visible to the next read
without waiting for the 60s tick.

## Consequences

**Positive.** One canonical join eliminates the six-shaped
audit gap. Dashboard, walkthrough, and audit-log replay can all
point at the same artifact ("here is what the agent saw at
2026-05-14T16:42Z, program=shopify, since=7d"). Sibling teams own
their own event emission; the aggregator never reaches into a
sibling's data model, only its public surface.

**Negative.** No persistence — the 60s window is the entire
memory. Asking "what happened on program X six weeks ago" still
requires a long-tail replay from the siblings themselves. Eventual
8+ TRL would need a durable replay log; out of scope for v0.1.

**Mitigations.** TRL ceiling pinned at 7 with `trl_ceiling_reason`
calling out the persistence gap, so the next agent doesn't lift it
without thinking. Per-sibling timeout (default 5s) is short enough
that one slow upstream can't starve a request — the slow sibling
just lands in `degraded[]`.

## Migration path

Consumers read `ENGAGEMENT_TIMELINE_URL`:

```go
url := os.Getenv("ENGAGEMENT_TIMELINE_URL")
if url == "" {
    url = "http://go-fleet-engagement-timeline:18162"
}
resp, _ := safehttp.Get(url + "/timeline?program=" + program + "&since=" + since)
```

Fail-open default: if `ENGAGEMENT_TIMELINE_URL` is unset, consumers
should fall back to direct sibling pulls (the pre-aggregator
behavior). That keeps the aggregator deployable as a non-critical
addition rather than a new mesh-wide hard dep.

Primary consumers (in priority order):

1. **`go-pentest-dashboard`** — replaces six per-program panels
   with a single "program activity" feed.
2. **`go-pentest-walkthrough`** — every agent-authored walkthrough
   now embeds the timeline snapshot it was reasoning over, so the
   reviewer sees the same artifact the agent saw.
3. **`go-pentest-audit-log`** — replay-time enrichment: cross-
   reference an agent decision against the visible timeline at
   decision time to detect "decided on stale data" patterns.

## Alternatives considered

* **Per-consumer fan-out (status quo).** Rejected — every consumer
  re-derives the same join, and there's no single shared snapshot a
  reviewer can replay.
* **Push-only model (siblings POST to a write-only timeline
  store).** Rejected — requires every sibling to gain a new outbound
  dep and a backfill story for events emitted before the aggregator
  existed. Pull-with-optional-push gets the same freshness with
  zero sibling code changes on day one.
* **Persistent store (sqlite / postgres).** Deferred — TRL 6 ceiling
  7 is the right starting point. Persistence is the route to TRL 8+
  but adds operational surface (retention, schema migrations) that
  this v0.1 doesn't need to justify itself.

## References

- Related: ADR-0002 (twenty-fleet-primitives — this is Tier A #10)
- Repo: <https://github.com/baditaflorin/go-fleet-engagement-timeline>
- Siblings consumed: asset-inventory, orchestrator, findings-store,
  finding-triage, submit-bot, payoff-tracker
- Primary consumers: go-pentest-dashboard, go-pentest-walkthrough,
  go-pentest-audit-log
