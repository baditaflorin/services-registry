# ADR-0005: Canonical 2-of-3 DNS Resolver Quorum

- **Status**: Accepted
- **Date**: 2026-05-16
- **Authors**: claude-session-2026-05-16
- **Related**:
  - Tier S #3 in `services-registry/FLEET-FUTURE-TOOLS.md`
  - Source pattern: `go-pentest-takeover-checker/internal/resolve/quorum.go` (batch-1 0.4.0)
  - Triggering incident: stale-dns-forwarders, 2026-05-15

## Context

`go-pentest-takeover-checker` has its own 2-of-3 multi-resolver quorum
in `internal/resolve/quorum.go` — added in batch 1 to keep
single-resolver NXDOMAIN lies from producing paid bug-bounty
false-positives. The pattern works: it caught the actual signal in the
stale-dns-forwarders incident on 2026-05-15 (where 1.1.1.1 briefly
returned NX for a host that 8.8.8.8 and 9.9.9.9 correctly resolved).

But the pattern is **trapped inside takeover-checker**. Several other
fleet services would directly benefit and don't have it:

- `dns-sync` — reconciles registry → Hetzner Cloud DNS. A stale
  per-resolver view causes phantom drift loops.
- `zone-transfer` — needs authoritative NS confirmation across vendors.
- `asn-lookup` — IP→ASN is brittle when one resolver lies about A
  records.
- `subfinder` — passive subdomain enumeration cross-checks DNS.
- `cert-transparency` — domain validation steps.

Each could (and some do) build a partial, subtly-different version.
That fragmentation is the same anti-pattern WAF detection exhibited
before `go-fleet-fingerprint-cache`: every service builds half a
quorum, none battle-tested, all subtly wrong on edge cases (errored
votes counting toward agreement, no per-resolver timeout, no per-RR
normalization).

## Decision

**Promote the quorum pattern to a canonical fleet service —
`go-fleet-resolver-quorum`.**

- Service shape: `POST /resolve {host, type, quorum?, resolver_overrides?}`
  returning `{result, consensus, split, resolvers[]}`. Plus
  `POST /resolve-batch` for fan-out, `GET /selftest` for the deploy
  smoke gate.
- Default resolvers: 1.1.1.1, 8.8.8.8, 9.9.9.9 (Cloudflare / Google /
  Quad9 — three vendors so a single-vendor outage cannot drop quorum).
- All RR types Go's `net.Resolver` supports: A, AAAA, CNAME, TXT, NS,
  MX, SOA (SOA approximated via LookupNS — see TRL ceiling).
- Errored votes do NOT count toward quorum — a flaky resolver must
  never be able to silence a real signal. This is the load-bearing
  invariant from the source pattern.
- Per-resolver timeout (3s default) AND overall request budget
  (3s default) — fan-out is parallel, so the overall budget is what
  the caller sees.

### Split-handling: 200 with `split: true`, NOT 207 Multi-Status

When no answer reaches the quorum threshold, the service returns
**HTTP 200** with `split: true` and full per-resolver detail in the
body. Considered alternatives:

| Option              | Pros                                   | Cons                                                                                 |
|---------------------|----------------------------------------|--------------------------------------------------------------------------------------|
| 207 Multi-Status    | "Semantically accurate" per RFC 4918   | Forces every caller to special-case the read. Many HTTP clients silently re-cast as error. |
| 5xx (502/503/504)   | Loud failure                           | Wrong — a split is not an upstream failure; the upstreams answered, they disagreed.   |
| **200 + split:true (chosen)** | Uniform caller code path        | Slightly less HTTP-RFC purist                                                        |

The deciding factor: **HTTP status carries the transport layer; the
quorum outcome is application data.** Callers check `body.split` and
`body.consensus` regardless. This matches every other fleet service
that returns a multi-state outcome in a 200 body.

### Migration path for consumers

Wire via `RESOLVER_QUORUM_URL` env var. **Fail-open** to local
`net.DefaultResolver` with `degraded: ["resolver-quorum-down"]` when
the quorum service is unreachable. Hard rationale:

- The quorum service is a quality-of-evidence amplifier, not a
  correctness gate. Going down should degrade signal, not stop scans.
- Pairs with `apikey.Cache` philosophy: gateway outages degrade,
  never hard-fail, while keystore is unavailable.
- Surfaces in the caller's response so triage knows the answer wasn't
  quorum-verified — preserves the audit trail.

The wire pattern (copy into each consumer):

```go
url := os.Getenv("RESOLVER_QUORUM_URL")
if url == "" {
    return localFallback(host, "resolver-quorum-not-configured")
}
res, err := safehttp.Post(url+"/resolve", body, timeout)
if err != nil {
    return localFallback(host, "resolver-quorum-down")
}
// otherwise use res.Result / res.Consensus / res.Split
```

## Consequences

### Positive

- One canonical place to add new resolver vendors, tune timeouts, or
  patch a quorum bug.
- Eliminates a class of false-positive that wastes triage time on
  bug-bounty workflows.
- Provides a `/selftest` endpoint the deploy gate uses to verify the
  patched code path actually runs (see CLAUDE.md "Why /selftest
  matters").

### Negative

- One more service to operate.
- Hop adds ~10-30ms to consumers' DNS-bearing paths. Mitigated by:
  the consumers already wait on the slowest of 3 parallel real DNS
  calls (which dominates the quorum hop).

### Neutral

- Source pattern in `go-pentest-takeover-checker/internal/resolve`
  stays for now. Consumers migrate as they touch the code; the
  fail-open contract means partial rollout is safe. ADR-0005
  supersedes the in-tree copy when takeover-checker is next bumped.

## Top 3 consumers to wire first

1. `go-pentest-takeover-checker` — already has the in-tree copy.
   Wire `RESOLVER_QUORUM_URL` and `internal/resolve` becomes the
   fallback path. Reference implementation of the migration pattern.
2. `go-fleet-dns-sync` — phantom-drift class of bug is exactly what
   the quorum fixes. Highest leverage per LOC changed.
3. `go-pentest-subfinder` (or `cert-transparency`, whichever lands
   next) — passive DNS enumeration cross-checks. The quorum lets
   the enumerator filter "we saw this once from one resolver, ignore"
   from "all three agree, this is real".

Tier-S #1 (`fingerprint-cache`) and #2 (`body-redactor`) are
orthogonal and ship in parallel.

## Hard rules locked in by this ADR

- **NEVER hit real 1.1.1.1 / 8.8.8.8 / 9.9.9.9 in tests.** Every test
  stubs `LookupFunc`. The CI is hermetic.
- **NEVER count errored votes toward quorum.** A SERVFAIL is not an
  NX vote and is not a positive answer vote.
- **NEVER mutate the shared default Quorum** on a per-request
  override. Use `WithResolvers` which clones.
- **NEVER manually mount `/metrics`.** `go-common/server` already
  does it. (Hard rule inherited from the session lessons.)
- **NEVER scaffold GitHub Actions** for build/test. Husky + local
  `go test ./...` is the CI.
