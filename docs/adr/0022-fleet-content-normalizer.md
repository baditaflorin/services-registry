# ADR-0022 — Centralise MIME / charset / encoding normalization in `fleet-content-normalizer`

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-opus-4-7-session-2026-05-16-tierB-20
* **Tags**: fleet-infra, primitives, normalization, fp-reduction

## Context

MIME-sniffing, charset detection, and gzip / brotli / deflate decoding
are duplicated across at least four active services
(`comment-extractor`, `secrets-scanner`, `xss-scanner`, `crawler`) and
the inconsistencies between those implementations create live FPs.
The most common failure is that one scanner sees the gzip-compressed
bytes (and pattern-matches against them — useless) while another sees
the decoded HTML — same response, two classifications. Charset
handling drifts the same way: one consumer treats undeclared bodies as
UTF-8 (HTML5 default with no meta tag is actually windows-1252, but
plain ASCII is valid UTF-8 either way), another runs everything
through `iconv` regardless, and we end up with cross-scanner evidence
mismatches that look like real findings until you trace them back.
`FLEET-FUTURE-TOOLS.md` lists this as Tier B #20 — not the highest-
duplication primitive in the fleet (that's the fingerprint cache,
ADR-0003), but the easiest to ship in a day and the one that
eliminates the largest single class of cross-scanner evidence drift.

## Decision

Ship `go-fleet-content-normalizer` on `mesh-0exec` port 18172 with one
primary endpoint:

```
POST /normalize {raw_b64, declared_content_type?, declared_encoding?}
→ {
    content_type_declared, content_type_sniffed, content_type_resolved,
    charset, encoding_chain[], decoded_b64, decoded_text?,
    bom_stripped, sniff_confidence, raw_bytes, decoded_bytes
  }
```

Pipeline (each step appends to `encoding_chain` so consumers get a
provenance trail):

1. **Size gate** — 5 MB raw input (413 on overflow). Re-checked after
   every decompression step to catch gzip bombs that fit under the
   raw cap but expand past it.
2. **Encoding chain unwrap** — parse `Content-Encoding`, peel
   right-to-left per RFC 7231 §3.1.2.2. Supported: `gzip`, `x-gzip`,
   `br`, `brotli`, `deflate` (raw flate — the in-the-wild shape, not
   zlib-wrapped). Hard cap of 4 chain steps (chain-bomb defense).
3. **MIME sniff** — `net/http.DetectContentType` over the first
   ≤ 512 bytes. We trust the declared type on conflict but record
   the sniffed type and a `sniff_confidence` ∈ [0, 1] so a security
   scanner can flag declared-vs-actual mismatches as content-type
   spoofing evidence.
4. **Charset detect + UTF-8 convert** —
   `golang.org/x/net/html/charset.DetermineEncoding`. Skip conversion
   when the bytes are already valid UTF-8 regardless of what the
   detector guessed (the HTML5 fallback to windows-1252 round-trips
   to itself for pure ASCII, but pollutes the encoding_chain log).
5. **BOM strip** — UTF-8 / UTF-16 BE/LE / UTF-32 BE/LE. Flag in
   `bom_stripped`.

Companion endpoints: `GET /selftest` (4 baked-in fixtures: gzip blob,
brotli blob, UTF-16 BOM body, windows-1252 body — returns 503 on any
miss so fleet-runner deploy gate refuses to ship a regressed binary).
`GET /health`, `GET /version`, `GET /metrics` come from
`go-common/server` automatically — never re-implemented manually.

Pure-function over inputs: no SQLite, no per-request allocation
beyond decompression buffers, safe to fan-out unbounded concurrent
calls (verified via `TestNormalize_ConcurrentSafe` — 100 goroutines,
`-race` clean).

## Consequences

**Positive**

- One normalization surface; two scanners CAN'T classify the same
  response differently because they're both calling the same
  primitive. Cross-scanner evidence drift collapses.
- Gzip-bomb defense lives in one place — every consumer benefits
  from the post-decompression cap without re-implementing it.
- The selftest endpoint is the regression contract; any caller can
  poll it to detect drift between the primitive's behavior and
  their expectations before a real engagement.
- Sniff-vs-declared disagreement (`sniff_confidence < 1.0`) becomes
  cross-fleet evidence — a security scanner can build a new
  detector class ("content-type spoofing") on top without any new
  parsing code.

**Negative**

- New SPOF for consumers that adopt it. Mitigated by the fail-open
  migration contract (see below): callers fall back to raw bytes
  with a `degraded: ["normalizer-down"]` flag rather than crashing.
- Adds a network hop to every response-body classification.
  Acceptable: the alternative is N-way drift between consumers,
  which is strictly worse, and the normalizer is in the same mesh
  as its consumers (sub-ms RTT).
- The 5 MB raw cap is a deliberate exclusion: response bodies bigger
  than that almost certainly shouldn't be sniffed inline. Consumers
  with legitimate large-body needs (e.g. binary asset scanners)
  must skip the normalizer and handle their own bytes.

**Mitigations**

- The selftest catches regressions before they roll to prod.
- The fail-open contract means a normalizer outage degrades a
  consumer's FP rate, not their availability.
- The `encoding_chain` provenance trail means a consumer can always
  reconstruct what the normalizer did (or didn't) to a body.

## Migration path (service ADRs)

Consumers adopt incrementally:

1. Read `CONTENT_NORMALIZER_URL` from env. Default to empty (= skip
   the normalizer entirely, behave as before — no surprise behaviour
   change on rollout).
2. POST `/normalize` with the response bytes + observed
   `Content-Type` + `Content-Encoding` headers.
3. On 200, use `decoded_b64` / `decoded_text` / `content_type_resolved`
   / `charset` instead of the local copy of this logic.
4. On any non-200 (timeout, 5xx, 413), fall back to raw bytes and
   add `"normalizer-down"` (or `"normalizer-413"`, `"normalizer-422"`)
   to the response's `degraded` field. Never crash on normalizer
   outage.

Per-call auth: the canonical fleet shape —
`X-API-Key: $api_key` or `?api_key=$api_key` — exactly like every
other `mesh-0exec` service. No new auth surface.

Top consumers (in dependency-removal order):

1. `comment-extractor` — currently re-implements its own brotli +
   UTF-16 path; the simplest swap.
2. `secrets-scanner` — gzip-handling lives in
   `fetch_response.go`; centralizing it means new provider keys can
   appear without touching the secrets scanner's decompression
   pipeline.
3. `xss-scanner` — needs the resolved Content-Type for the active-
   scan budget (skip XSS probing on declared-application/json bodies
   that actually sniff as text/html — a known FP class).

## Alternatives considered

- **Embed the normalizer as a `go-common/normalize` library**, no
  separate service. Rejected because it propagates dep bumps across
  150 repos for every brotli vendor release; the network hop is the
  cheaper coordination cost.
- **Skip declared-encoding entirely, always sniff**. Rejected: many
  servers send Content-Encoding without a recoverable signal in the
  body bytes (raw deflate is unsniffable). Trust the declaration,
  fail loud on disagreement.
- **Use `golang.org/x/text/transform` chains directly in consumers**.
  Same library, same code, N copies — exactly the duplication this
  ADR exists to eliminate.

## References

- Repo: <https://github.com/baditaflorin/go-fleet-content-normalizer>
- Related: ADR-0003 (fleet-fingerprint-cache — the other "centralise
  classification logic" ADR; same motivation, different axis).
- Related: ADR-0004 (fleet-body-redactor — same "stateless primitive
  on mesh-0exec with selftest gate" shape).
- `FLEET-FUTURE-TOOLS.md` Tier B #20 — the original motivation.
