# ADR-0021 — Pre-scan reputation lookup primitive (`go-fleet-target-reputation`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-opus-4-7-session-2026-05-16
* **Tags**: fleet-infra, security, recon, primitives

## Context

Before any probing service in the fleet touches a target — scanner,
banner-grabber, crawler, takeover-checker — we want to answer one
question first: **is this host known-bad?** Compromised box, known
sinkhole, security researcher's tarpit, malware delivery domain,
phishing landing page. Today the fleet has **nothing**. Every probe
hits cold and we learn the target was a tarpit only after we've
wasted budget on it, or worse, after our probing has been logged as
"this researcher targeted my honeypot."

The signal exists in the world — Spamhaus DBL is free, URLhaus is
free, AbuseIPDB has a generous free tier, PhishTank has a free key
flow, plus we accumulate operator knowledge ("I scanned X last week
and it was a honeypot, don't scan again"). What's missing is a
single composable primitive that fans out, caches, and returns a
uniform verdict. Every consumer would otherwise re-roll the same
five HTTP calls + cache + key handling — and worse, get the
operator-flag short-circuit wrong (the part where a freshly added
flag must override a stale-but-not-yet-expired cache row).

`FLEET-FUTURE-TOOLS.md` lists this as **Tier B #19**.

## Decision

Ship `go-fleet-target-reputation` on `mesh-0exec` port 18171 with
this surface:

```
POST /reputation
  {host_or_ip}
→ {target, fetched_at, ttl_seconds, score 0..100, known_bad bool,
   categories [...], sources [{name, status, hit, score?, last_seen?, ...}],
   notes?}

POST /flag    (admin token)
  {target, source?, reason, ttl_days?}

GET  /flag/{target}
→ {target, flags [...], count, has_active}

GET  /selftest    (in-process integration test)
GET  /health      (standard)
GET  /version     (standard)
GET  /metrics     (Prometheus, from go-common/server — never manual)
```

Five default sources, all opt-in. Missing API key degrades to
`status: "not_configured"` — **never** a hard error:

| Source         | Key env             | Notes                                     |
|----------------|---------------------|-------------------------------------------|
| local-flag     | (always on)         | Operator always-flag list. Short-circuit. |
| spamhaus-dbl   | (DNS-only, no key)  | `<host>.dbl.spamhaus.org` A-record probe. |
| abuseipdb      | `ABUSEIPDB_KEY`     | IPv4/IPv6 only. Score≥50 → known_bad.     |
| urlhaus        | (free, no key)      | `urlhaus-api.abuse.ch/v1/host/` POST.     |
| phishtank      | `PHISHTANK_KEY`     | `http://<target>/` probe.                 |

24h SQLite cache keyed on the normalized lowercase target. Cache and
flag writes both use `BEGIN IMMEDIATE` so concurrent updates on the
same row serialize cleanly. The local-flag short-circuit runs
**before** the cache read so a freshly added flag is never masked by
a stale-but-not-yet-expired cache row. `AddFlag` also deletes the
cache row for the target inside the same transaction as the insert,
as defense in depth.

`/selftest` exercises the full round-trip against an in-process stub
source set — never reaches the network. The deploy pipeline's
`/selftest` smoke gate (see `services-registry/CLAUDE.md`) can verify
the patched code path actually runs, not just that the binary booted.

Tests stub **every** upstream:
- DNS via an injected `Resolver` interface (production wires
  `net.DefaultResolver`; tests wire a map-backed stub).
- AbuseIPDB / URLhaus / PhishTank via `httptest.Server` and a
  `BaseURL` field on each source struct.

8 tests pass with `-race`. Zero test reaches the real internet.

## Consequences

**Positive**

- One reputation surface across the fleet; consumers stop re-rolling
  the five HTTP calls. New sources (Recorded Future, Mandiant,
  custom honeypot feeds) land as one more case in `sources.go` +
  one TP/TN test pair.
- The local-flag short-circuit is enforced in one place — the
  invariant "operator overrides external sources" can't drift.
- 24h cache amortises the cost of upstream rate limits across every
  fleet consumer; AbuseIPDB free-tier 1000 req/day suddenly covers
  a much larger probe volume.
- `/selftest` gives the deploy pipeline a real verification gate.
- Operator-flagged compromised targets propagate fleet-wide
  immediately (cache invalidated on flag add).

**Negative / mitigations**

- One more network hop on the pre-scan path. Mitigated by the cache
  (hot rows are sub-ms), by the sequential-but-bounded source
  fan-out (3 s timeout per source), and by the fact that the
  pre-scan check is itself an optimisation — it lets the consumer
  skip a full scan, so a 50 ms reputation lookup that saves a
  30 s scan is a net win.
- Free-tier sources are not authoritative. Mitigated by `trl_ceiling:
  7` (this primitive can't get past TRL 7 without paid feeds) and by
  the explicit `score 0..100` shape: a consumer asking "is this safe
  to scan?" must look at the score, not just `known_bad`.
- Spamhaus DBL queries leak the target hostname into Spamhaus's
  query logs. Acceptable — same as any DNS query for the target —
  but worth noting for any future ADR that wants to add a "high
  sensitivity" mode that skips DBL.

## Migration

A consumer adopts the primitive in three steps:

1. Add an env knob `TARGET_REPUTATION_URL=http://go-fleet-target-reputation:18171`
   (canonical; matches the secrets / dns-sync / preflight pattern).
2. Before issuing a probe, `POST $TARGET_REPUTATION_URL/reputation
   {host_or_ip}` and gate on `known_bad`.
3. When the operator notices an in-flight scan hit a honeypot, file
   `POST /flag` with the target and reason; future scans across the
   fleet skip it.

**Top expected consumers**, in adoption priority:

1. **`go-pentest-asset-scope-resolver` / scope-guard** — auto-deny
   any target whose reputation lookup returns `known_bad: true`.
2. **`continuous-monitor` (any periodic re-scanner)** — skip
   previously-flagged targets entirely; flag → permanent skip.
3. **`go-fleet-preflight`** — add a "target reputation" red/green
   row to the pre-deploy checklist when a deploy targets external
   integrations or webhook URLs.

## Alternatives considered

- **Embed each consumer's own reputation logic.** Rejected:
  five repeated HTTP integrations × N consumers = N×5 places to get
  the operator-flag short-circuit wrong.
- **Call upstream sources from the consumer directly with no
  caching layer.** Rejected on free-tier rate limits alone.
- **Mandate paid threat-intel feeds.** Rejected: out-of-budget and
  the free signal is already enough to catch the obvious
  honeypot/sinkhole cases. TRL ceiling captures the gap.
- **Pure cache layer with no aggregation.** Rejected because the
  uniform-verdict shape (one score, one known_bad bit) is what
  makes the primitive usable as a one-line consumer gate.

## Implementation

- Repo: `github.com/baditaflorin/go-fleet-target-reputation` (private).
- Port: **18171** host & container.
- Mesh: `mesh-0exec`.
- Category: `fleet-infra`.
- TRL 6 / ceiling 7.
- Compose pin: `:0.1.0`.
- Initial tag: `0.1.0` (no `v` prefix).
- Sources: `main.go`, `store.go`, `sources.go`, `lookup.go`,
  `handler.go`, `reputation_test.go`.
