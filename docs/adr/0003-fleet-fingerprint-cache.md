# ADR-0003 — Centralise WAF / soft-404 / CDN-noise classification in `fleet-fingerprint-cache`

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-opus-4-7-session-2026-05-16-batch4
* **Tags**: fleet-infra, classifier, primitives, fp-reduction

## Context

Five active scanners — `admin-finder`, `broken-links`,
`crawl-web-application`, `nuclei`, `cors-tester` — each carry their
own (subtly different) "is this response a WAF block or a real 404 or
the origin?" heuristics. The duplication produces three observed
failure modes: (1) the same response gets classified differently by
two services in the same engagement (FP rate fluctuates with which
scanner happened to run first), (2) every new WAF behaviour (e.g.
Cloudflare's turnstile v2 challenge path) needs to be patched in five
places, and (3) soft-404 detection is the weakest link — three of the
five services treat any 200 with "Not Found" in the title as soft-404,
which over-flags intentional UX. `FLEET-FUTURE-TOOLS.md` lists this
as Tier S #1 — the most-duplicated piece of logic in the fleet and the
root cause of the majority of active-scan false positives.

## Decision

Ship `go-fleet-fingerprint-cache` on `mesh-0exec` port 18153 with a
single primary endpoint:

```
POST /classify {url, status, headers, body_sha256, body_first_1k}
→ {kind, confidence, signals[], duration_ms, cache_hit}
```

`kind ∈ {waf-cloudflare, waf-akamai, waf-imperva, waf-aws, waf-sucuri,
soft-404, cdn-noise, chromium-error, origin-real}`. Backed by a
curated SQLite catalog (~40 seed signatures, embedded via
`//go:embed testdata/signatures.jsonl`) plus an in-memory LRU keyed
on `(host, status, body_sha256)` for the hot path. Each signature
declares a `source` (`header`, `body-snippet`, `status+server`), a
match `pattern` (literal substring or regex — auto-detected by
`looksLikeRegex`), and a `confidence` in [0, 1]. The classifier
returns the highest-confidence matching signature, or
`{kind: "origin-real", confidence: 1.0}` if no signature fires.

Companion endpoints: `GET /signatures` (paginated catalog),
`POST /signatures` (admin-token gated runtime insert),
`GET /selftest` (~15 golden fixtures, returns 503 on any regression).
`GET /health`, `GET /version`, `GET /metrics` come from
`go-common/server` automatically.

The SQLite write path uses the canonical fleet pattern (BEGIN
IMMEDIATE on a pinned `*sql.Conn`, see
`go-pentest-job-queue/store.go enqueueIdempotent`) so concurrent
admin inserts serialise on SQLite's single writer instead of
emitting `SQLITE_BUSY` on a deferred lock upgrade.

## Consequences

**Positive**

- One classification surface across the active-scan mesh; FP rates
  collapse to a single curve we can move with one PR.
- New WAF behaviours (e.g. CF turnstile v2, Akamai Bot Manager v3)
  land via `POST /signatures` against a running container — no
  redeploy, no consumer changes.
- The selftest endpoint is the regression contract: any caller can
  poll it to detect drift between catalog and reality.

**Negative**

- New single point of failure for the active-scan mesh. Mitigated by
  fail-open default (see Migration) and by the fact that scanners
  retain their local heuristics behind a flag.
- Curated signatures lag novel bypasses by however long an operator
  takes to file a new row. We accept this — the alternative is N-way
  drift between scanners, which is strictly worse.
- LRU cache assumes `body_sha256` is computed identically across
  consumers. We document the sha256-of-full-body convention; callers
  that hash differently will miss the cache (slow path is still
  correct).

**Mitigations**

- Fail-open default: when `FINGERPRINT_CACHE_URL` is unset or the
  service returns non-2xx, callers fall back to local heuristics
  (their pre-this-ADR behaviour).
- Per-signature `confidence` lets downstream code threshold on
  certainty rather than hard-coding "WAF or not".
- The `signals[]` array in every response gives a per-call audit
  trail of which signatures fired — easier to triage a wrong
  classification than to triage a binary.

## Migration path (service ADRs)

Consumers adopt this primitive one at a time:

1. Set `FINGERPRINT_CACHE_URL=http://go-fleet-fingerprint-cache:18153`
   in the consumer's `docker-compose.yml`.
2. Wrap existing local WAF logic in a fingerprint-cache call:

```go
import "github.com/baditaflorin/go-common/safehttp"

if url := os.Getenv("FINGERPRINT_CACHE_URL"); url != "" {
    res, err := callFingerprintCache(ctx, url, req)
    if err == nil {
        return res.Kind, res.Confidence, res.Signals
    }
    // fail-open: fall through to local heuristics
    log.Printf("fingerprint-cache unavailable: %v", err)
}
return localHeuristicsClassify(req)
```

3. After consumer + fingerprint-cache both ship green for one
   deployment cycle, delete the local heuristics.

**Default**: fail-open. A missing env var or an outage returns to the
pre-this-ADR behaviour, never an empty/wrong classification.
**Per-call shape**: see `POST /classify` schema above; body capped at
~8 MiB by the handler.

## Alternatives considered

1. **Locally-cached signatures (each service ships its own catalog
   file).** Rejected: solves duplication but not drift — services
   would still update on their own cadence. Also doesn't get us the
   `signals[]` audit trail per call.
2. **On-call Cloudflare API (and equivalent for Akamai/Imperva).**
   Rejected: paid; rate-limited; doesn't help with soft-404 or
   chromium-error; introduces external dependency for every active
   scan.
3. **Push classification into `go-common`.** Rejected: every WAF
   signature update would force `fleet-runner update-dep` across all
   consumers. The runtime endpoint (`POST /signatures` against a
   live container) is the whole point — bypass the rebuild loop.
4. **ML / fingerprint embedding model.** Rejected for v1: curated
   signatures are explainable (the `signals[]` array names every
   rule that fired). An ML approach is `trl_ceiling` territory and
   would need labelled training data we don't yet have.

## References

- [`FLEET-FUTURE-TOOLS.md`](../../../FLEET-FUTURE-TOOLS.md) — Tier S #1 entry.
- [`ADR-0002`](0002-twenty-fleet-primitives.md) — the broader
  primitives strategy this ADR implements.
- [`go-pentest-job-queue/store.go`](https://github.com/baditaflorin/go-pentest-job-queue/blob/main/store.go)
  — canonical BEGIN IMMEDIATE on pinned `*sql.Conn` pattern.
- Consumer repos that should adopt next: `admin-finder`,
  `broken-links`, `crawl-web-application`, `nuclei`, `cors-tester`.
