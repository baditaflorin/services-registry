# ADR-0009 — fleet-tech-inferrer composes single-signal services into a stack guess

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-session-2026-05-16
* **Tags**: composition, fleet-infra, recon, tech-detection

## Context

`go-pentest-favicon-hash`, `go_analyze_headers`, and `go_cookie_checker`
each surface a single fingerprint signal. None of them composes those
signals into a single normalized tech-stack answer — every consumer
that wants "what is this site running?" duplicates the same hand-roll:
fetch the URL, parse cookies, grep meta tags, optionally call favicon-
hash, optionally call analyze-headers, merge the results, guess the
stack. This duplication has cropped up in `go-pentest-dependency-cve`
(needs `tech → likely-CVE-set`), `go-pentest-nuclei` (needs `tech →
template-set`), and `go-pentest-attack-chainer` (needs a stack label
to pick the right exploit family). Each implementation is slightly
different and silently disagrees on confidence semantics.

## Decision

Add `go-fleet-tech-inferrer` (port 18159, mesh-0exec, fleet-infra,
TRL 6, ceiling 8). It is a thin composition primitive:

- `POST /infer {url}` (also `GET /infer?url=...` for browsers)
  - Fetches the URL **once** via `safehttp`.
  - In parallel (5s timeout each, fail-open) calls
    `go-pentest-favicon-hash` and `go_analyze_headers`.
  - Runs an embedded ~83-row signal corpus (`signals/tech.jsonl`,
    `//go:embed`) locally against headers + Set-Cookie names +
    `<meta name="generator">` + path-fingerprints in the body.
  - Aggregates per-tech contributions using probabilistic OR
    (`1 - Π(1-wᵢ)`, capped at 0.99) and returns:
    ```json
    {
      "url": "...",
      "tech": [
        {"name":"WordPress","confidence":0.96,
         "signal_sources":["cookie-name","favicon-hash","meta-generator"],
         "stack":"wordpress","evidence":["..."]}
      ],
      "stack_guess": "wordpress",
      "inferred_at": "2026-05-16T19:27:04Z",
      "degraded": ["favicon-hash:upstream 500"]
    }
    ```
- `GET /infer/cached?url=<url>` — cache-only lookup; 404 on miss.
- `GET /signals?page=&per_page=` — paginated corpus dump.
- `GET /selftest` — 5 baked stubbed-httptest fixtures end-to-end;
  200 on all-pass, 503 on regression (autofix.py gate).
- 1-hour in-memory LRU cache (1024 entries) keyed on a normalized URL
  (fragment stripped, query sorted).
- `stack_guess` heuristic: sum per-tech confidence into per-stack
  buckets, then roll less-specific stacks into more-specific
  prefix-children ("java" → "java-spring" when both present), pick
  the highest.

## Consequences

**Positive**
- Three downstream services (`dependency-cve`, `nuclei`,
  `attack-chainer`) stop hand-rolling tech detection.
- Confidence semantics (probabilistic OR) are uniform — a caller can
  threshold on `confidence >= 0.8` and mean the same thing across
  signal sources.
- `/selftest` runs the FULL pipeline against in-process httptest
  fixtures, so `autofix.py`'s post-deploy gate exercises the actual
  classifier and not just a "binary booted" probe.

**Negative**
- Inferrer becomes a single point of false-negative: a missing
  fingerprint row blocks every downstream from seeing that tech.
  Mitigation — corpus is a flat JSONL anyone can PR; `/signals` is
  paginated and public for diff.
- Inferrer adds one extra hop to the dependency chain. Mitigation —
  1-hour LRU cache means repeated queries on the same URL skip both
  upstream calls and the local fetch.

**Mitigations**
- Fail-open per upstream: an unreachable favicon-hash or
  analyze-headers downgrades to a `degraded[]` entry in the response
  and the local pipeline still runs.
- The embedded corpus is small (~83 rows, ~6 KB) so it's always shipped
  in the binary; no separate sync step.

## Migration path

Consumers read a single env var:

```bash
TECH_INFERRER_URL=http://go-fleet-tech-inferrer-app-1:18159
```

Call shape:

```go
resp, err := http.Post(
    techInferrerURL+"/infer",
    "application/json",
    strings.NewReader(`{"url":"`+target+`"}`),
)
```

**Fail-open contract**: on `err != nil` or `resp.StatusCode >= 500`,
the consumer should treat tech as empty and proceed with its
fallback (regex on the response, hand-rolled signal extraction, etc.)
— never block the user-facing flow on inferrer reachability. Capture
the failure as a `degraded[]` entry in the consumer's own response.

## Alternatives considered

1. **Bake the composition logic into each consumer.** Status quo.
   Rejected because the confidence semantics drifted: nuclei was
   using max-of-signals, dependency-cve was using sum-clamped-at-1,
   attack-chainer was using "any signal counts". One inferrer with
   one OR rule fixes that.

2. **Extend `go_analyze_headers` to do the composition.** Rejected
   because `analyze-headers` is single-purpose (OWASP-grade audit
   on response headers); coupling it to favicon-hash + body parsing
   would balloon its surface and make its `/selftest` meaningless.

3. **Make favicon-hash and analyze-headers internally call each
   other.** Rejected as creating a cycle in the service graph and
   leaving cookie/meta/path signals nowhere to live.

## References

- ADR-0002 — Twenty fleet primitives (this is the 21st, slotted into
  fleet-infra)
- Sibling services: `go-pentest-favicon-hash`, `go_analyze_headers`,
  `go_cookie_checker`
- Top 3 consumers: `go-pentest-dependency-cve`, `go-pentest-nuclei`,
  `go-pentest-attack-chainer`
- Source: `github.com/baditaflorin/go-fleet-tech-inferrer`
