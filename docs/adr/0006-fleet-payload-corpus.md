# ADR-0006 — Canonical signed payload corpus for the fleet

**Status**: Accepted
**Date**: 2026-05-16
**Service**: `go-fleet-payload-corpus`

## Context

Six active probers each bake in their own attack payloads:

- `go-pentest-xss-scanner`
- `go-pentest-cors-misconfig-prober`
- `go-pentest-crlf-tester` (planned)
- `go-pentest-smuggling-probe`
- `go-pentest-ssrf-prober` (planned)
- `go-pentest-authz-matrix`

When a new bypass class appears (a fresh CRLF double-encoding trick, a
new JWT alg-confusion variant, a new SSRF DNS-rebind setup, a payload
that defeats a current WAF generation), the new payload gets added to
one or two of these probers and **forgotten in the others**. The fleet's
offensive surface drifts class-by-class. There is no way to ask "does
every prober know about this 2026-Q1 SSRF bypass?" — the question
requires reading six source trees.

The same drift happened with `default_token` rotation (fixed by
moving token state to the gateway) and with the JS-bundle source-map
recovery code (fixed by moving it into `go-common`). The cardinal
rule applies: **change the library / data, not 130 main.go files**.

## Decision

Build `go-fleet-payload-corpus` as a fleet primitive that serves a
canonical, versioned, validated payload set over HTTP. Every prober
fetches payloads from this service at startup (or per request, if
hot-swap is needed) instead of baking them in. A new bypass class
becomes a single PR against this repo, and the entire fleet picks it
up on next deploy or restart.

The corpus structure on the wire is uniform regardless of class:

```json
{
  "id":              "<class>-<NNN>",
  "class":           "<class>",
  "payload":         "<the literal payload string>",
  "target_context":  "url-param | header | body | cookie | json-field | xml-field | path-segment | form-field | any",
  "expected_signal": "marker | status-flip | timing | callback",
  "version":         "<corpus version>",
  "notes":           "…",
  "added_at":        "RFC3339",
  "added_by":        "…"
}
```

The `version` field on each row lets us add new payloads (and even
new fields) without breaking old consumers — an old prober knows
nothing about new rows and ignores unknown fields.

## Migration plan

1. **Phase 0 (this commit)** — ship the corpus with a curated initial
   set (~120 payloads across 12 classes). `/selftest` enforces ≥ 5
   per class so a regression breaks the deploy smoke gate, not a
   live engagement.
2. **Phase 1** — each prober gains a `PAYLOAD_CORPUS_URL` env var.
   When set, the prober fetches `GET /payloads/{class}` at startup
   and uses those payloads. When unset, the prober uses its baked-in
   vendored copy (preserving today's behavior).
3. **Phase 2** — probers keep their vendored copy as a **last-resort
   fallback** for keystore outages. The fetch path is wrapped in
   `apikey.Cache`-style 15-minute positive cache. If the corpus
   service is unreachable for longer than the cache lifetime AND the
   prober has no cached corpus, it falls back to the vendored copy
   and logs a warning. Probing degrades gracefully — never fails open
   to no-payloads.
4. **Phase 3** — vendored fallback gets refreshed quarterly by a
   `fleet-runner refresh-payload-fallback` command that fetches the
   current corpus, dumps it into each prober's `payloads/` dir, and
   bumps the dep. The baked-in copy stays a stale-but-safe lower
   bound.

## Why HTTP, not a Go package

A `go-common/payloads` package would force every prober to recompile
+ redeploy when a payload changes. HTTP + cache lets a single deploy
of `go-fleet-payload-corpus` propagate new payloads to every prober
on its next cache miss. Tradeoff: probers gain a runtime dependency
on a fleet primitive. Mitigated by the vendored fallback (Phase 2).

## Why admin-gated POST instead of git-only

PRs against this repo are the canonical write path. POST exists for
two narrow cases:

1. **Capture from a live engagement** — a researcher discovers a new
   bypass mid-engagement and wants it baked into the corpus before
   the session ends. POST + a fleet-runner-mediated commit-back-to-git
   is faster than waiting for a manual PR round-trip.
2. **autofix / disclose tooling** — `go-pentest-leak-bounty-policy/bin/autofix.py`
   can land a corpus addition as a mechanical fix without human review
   when a gap is detected during a real engagement.

Either path eventually writes through to git via a follow-up commit
that captures the runtime-submitted rows back into the JSONL files.

## Why JSONL on disk, not JSON / YAML

Merge conflicts. With six probers planning to submit payloads, a
single JSON-array file becomes a daily conflict surface. One-row-per-
line means concurrent additions touch different lines and merge
cleanly. JSONL is also git-diff-friendly — adding payload `xss-031`
shows up as one added line, not a re-pretty-printed array.

## Why no live-data exfil payloads

Hard rule. The corpus is for confirming presence of a vulnerability
class with the smallest possible blast radius. `<script>alert(1)</script>`,
`sleep(5)`, `' OR 1=1 --`. Payloads that read live data (`SELECT
password FROM users`), modify state (`'; DROP TABLE …`), or generate
disproportionate load (full nine-level billion-laughs, real Slowloris
shapes) are out of scope. A prober that needs data exfil for proof
builds it on top of a presence confirmation, not the other way around.

## Consequences

**Good**:

- Single PR to add a new class member; every prober picks it up on
  next restart.
- Floor-enforced `/selftest` catches "someone deleted the xss class
  by accident" at deploy time, not engagement time.
- Audit surface: `curl /payloads` answers "what's our coverage today"
  in one round-trip.
- Versioned `Payload.version` field lets us evolve the schema without
  breaking old probers (forward compatibility built in).

**Bad**:

- Probers gain a runtime dependency on a fleet primitive. Mitigated
  by 15-min cache + vendored fallback.
- POST path requires admin token rotation discipline (rotate
  `CORPUS_ADMIN_TOKEN` quarterly).

**Neutral**:

- Migration is opt-in per prober (`PAYLOAD_CORPUS_URL` env). Probers
  that don't set it keep current behavior. Zero forced flag day.

## Top 3 consumers (priority for Phase 1 migration)

1. **`go-pentest-xss-scanner`** — biggest payload list today (~40
   baked-in), biggest drift surface. Highest ROI from canonicalization.
2. **`go-pentest-cors-misconfig-prober`** — Origin-header variants
   drift fastest; fresh bypass classes published weekly.
3. **`go-pentest-smuggling-probe`** — request-smuggling variants are
   the most-bypass-class-y payload family on the fleet; baking them
   in here gets every smuggling research the whole fleet for free.
