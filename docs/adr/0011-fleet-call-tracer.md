# ADR-0011 — Add a per-request call trace collector (`go-fleet-call-tracer`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: baditaflorin
* **Tags**: observability, fleet-infra, sqlite, tracing

## Context

`go-fleet-graph` (batch 1, shipped at 0.2.0) gives us the **aggregate**
service-to-service edge graph — for any (caller, target) pair we know
the per-minute call counts, p50/p99, declared-vs-observed drift, and
1m → 1h → 1d retention rollups. That's enough to answer "is svc A
talking to svc B" or "is anything quietly going silent".

What it cannot answer is **"what path did *this specific request*
take through the fleet?"**. When `go-pentest-finding-triage` makes a
decision that turns out wrong, we currently can't replay the call
chain that fed into it — outbound calls from triage to enrichers,
their sub-calls to OSINT / DNS / cert services, status, latency,
errors. We see the aggregate edge counts went up; we don't see *which
trace* the bad triage came from.

Aggregate-only observability has bitten us twice already in May:
once a finding-triage flap traced to a flaky upstream we couldn't
pinpoint, once an enricher silently degrading where the aggregate
just showed slightly elevated p99. Both wanted span-level replay.

## Decision

Stand up a sister service to `go-fleet-graph`: `go-fleet-call-tracer`,
canonical port `18161`, slug `fleet-call-tracer`, `mesh-0exec`,
`category: fleet-infra`. Repo: `baditaflorin/go-fleet-call-tracer`
(PRIVATE — operational signal).

The tracer is the **receiver and query interface** for per-request
spans. (The library side — making `go-common/safehttp`'s round-trip
transport emit spans here — is a separate change, see "Migration"
below.)

### Storage

SQLite (modernc.org/sqlite, pure Go, same as fleet-graph), pragmas
`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`.

Schema:

```
traces (
    id INTEGER PK, trace_id, span_id, parent_span_id NULLABLE,
    from_service, to_service, method, path, status, duration_ms,
    error NULLABLE, ts (RFC3339), ts_ms INTEGER
)
INDEX (trace_id, ts_ms)
INDEX (from_service, ts_ms)
INDEX (ts_ms)

trace_aggregates (
    hour_ts, from_service, to_service,
    count, p50_ms, p95_ms, error_count
    PK (hour_ts, from_service, to_service)
)
```

### Write hot path

POST `/traces` uses **`BEGIN IMMEDIATE` on a pinned `*sql.Conn`**
(same pattern as `go-pentest-job-queue` 0.2.1's
`enqueueIdempotent`). With deferred transactions, concurrent batch
inserts hit SQLITE_BUSY on lock upgrade and time out before
`busy_timeout` helps; the pinned-conn + BEGIN IMMEDIATE pattern
serialises writers early on the Go side and the `-race` clean
50-goroutine concurrent-insert test confirms it.

Malformed batches are rejected wholesale (any span missing
`trace_id`, `span_id`, `from_service`, `to_service`, `method`, or
`path` → 400). Retries won't help — fail loud.

### Retention

Modelled on `go-fleet-graph` 0.2.0:

- Every 5min, fold raw `traces` rows older than `RETENTION_RAW_HOURS`
  (default 24h) into `trace_aggregates` (per (hour, from, to)) with
  p50/p95 computed in Go (SQLite has no percentile_cont) and an
  `error_count` for status ≥ 400.
- Delete the rolled raw rows.
- Drop `trace_aggregates` older than `RETENTION_DAYS` × 24h
  (default 7d).
- Panic-safe loop so a transient SQL hiccup doesn't kill the
  collector.

### Query surface

```
POST /traces                                 → {"accepted":N}
GET  /traces?trace_id=X                       → flat spans + parent/child tree
GET  /traces/recent?from=X&since=1h&limit=N   → debug "what did X call lately"
GET  /flame/{trace_id}                        → Speedscope-compatible JSON
GET  /selftest                                → ingest→query→flame→retention (200/503)
GET  /health, /version, /metrics              → from go-common/server
```

`/flame` emits the Speedscope "evented" schema (also consumed by
Pyroscope and Speedscope-adapter pprof viewers). One synthetic root
frame plus one frame per span; events ordered by start time with
opens-before-closes on tie so the stack stays well-formed.

## Consequences

**Positive**

- Per-request replay: given a trace id, recover the full call chain
  with method, path, status, latency, error.
- Familiar operations surface: same fleet-runner deploy path, same
  `/selftest` 200/503 gate, same retention discipline as
  `go-fleet-graph`.
- Flamegraph-native: existing Speedscope / Pyroscope / pprof tooling
  reads the `/flame` output without a custom adapter.

**Negative**

- Write throughput is single-writer-bounded (SQLite). The fleet's
  current per-request rate fits comfortably; if a future spike
  changes that, the migration path is the same as `go-fleet-graph`
  would face — pinned conn + batch + adjust pragmas.
- Storage churn: every fleet HTTP call becomes a row for ≤ 24h.
  Sized for ~10⁵ calls/day per fleet (well within SQLite's headroom);
  the rollup keeps the long horizon cheap.

**Mitigations**

- `BEGIN IMMEDIATE` on a pinned conn + Go-side mutex queues writers
  early — no SQLITE_BUSY storms on burst.
- Retention is opportunistic: 5min ticker, panic-safe, no-op when
  nothing's old enough to roll.
- `/selftest` exercises the real pipeline (insert → query → flame
  → retention move) end-to-end against an in-memory store, so the
  fleet-runner deploy smoke gate catches the worst regressions
  before they roll into prod.

## Migration path (service ADRs)

### Consumer env var

`CALL_TRACER_URL` — base URL of the tracer. If unset, the safehttp
transport must **not** call it (no-op). Example:
`http://<dockerhost>:18161`.

### Fail-open contract (HARD requirement)

Callers **MUST NOT block on POST `/traces` failure or latency**. The
emit path is fire-and-forget:

- Buffer spans in an in-memory channel (small bounded buffer, drop
  on overflow with a counter — never apply backpressure to the
  business call).
- Flush in batches (≤ 100 spans, ≤ 1s) on a background goroutine.
- Tracer 4xx/5xx is logged at debug, never returned to the caller.

The library-side change for this (a `go-common/safehttp` middleware
that wraps `http.RoundTripper`) is a **separate PR** against
`go-common` — out of scope for this ADR's repo, in scope for the
companion change.

### Per-call shape (what the library emits)

```json
{
  "spans": [
    {
      "trace_id": "abc123…",
      "span_id":  "def456…",
      "parent_span_id": "abc123…",
      "from_service":   "go-pentest-finding-triage",
      "to_service":     "go-osint-aggregator",
      "method": "POST",
      "path":   "/enrich",
      "status": 200,
      "duration_ms": 42,
      "ts": "2026-05-16T13:00:00.123Z"
    }
  ]
}
```

`trace_id` propagation: reuse `X-Trace-Id` if present on the inbound
request; otherwise mint a new one and forward it on every outbound
call. Same for `X-Span-Id` (becomes the next call's
`parent_span_id`).

## Alternatives considered

1. **Extend `go-fleet-graph` to also store raw spans.** Rejected: the
   graph collector's job is aggregation. Adding per-request storage
   would force its retention math and query surface to serve two
   masters; the schemas don't compose cleanly.
2. **Jaeger / Tempo / OTLP.** Rejected for now: brings a JVM (Jaeger)
   or a Grafana-stack dependency (Tempo) that doesn't match the
   fleet's single-binary Go + SQLite pattern. Speedscope JSON output
   keeps the door open to migrate later — operators can already point
   Speedscope at `/flame/{trace_id}` today.
3. **Log-based tracing (structured logs + post-hoc reconstruction).**
   Rejected: the parent_span_id links are unreliable when reconstructed
   from logs (clock skew, sampling), and there's no native flamegraph
   output. Building this on top of logs is more code than just storing
   the spans.

## References

- ADR-0002 — twenty fleet primitives (this is Tier A #9)
- `go-fleet-graph` — sister aggregate collector, retention pattern
- `go-pentest-job-queue` — BEGIN IMMEDIATE on pinned conn pattern
- `services-registry/FLEET-FUTURE-TOOLS.md` — original entry
