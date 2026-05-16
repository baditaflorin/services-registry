# ADR-0007 — Canonical HAR 1.2 emitter (`go-fleet-har-builder`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-session-2026-05-16
* **Tags**: fleet-evidence, primitives, interoperability

## Context

4+ services in the fleet format request/response evidence
differently. `go-pentest-http-replay` emits a custom JSON envelope
keyed on `replay_id`. `go-pentest-submit-bot` re-shapes findings into
each platform's submission form. `go-pentest-walkthrough` renders
markdown with inline code blocks. `go-pentest-exploit-verifier` keeps
raw bytes alongside a hash. None of these formats are consumable by
the external tools security teams actually use for triage — Burp,
ZAP, Postman, Chrome DevTools, and any HAR viewer all expect a HAR
1.2 document. Today our evidence trail is fleet-internal-only; a
triager who wants to replay a finding in Burp has to hand-translate
shapes. Worse, every emitter that ships a new evidence format
re-implements header redaction, mostly imperfectly (`http-replay`
strips `Authorization` but misses `Cookie`; `walkthrough` strips both
but misses `X-Admin-Token`).

## Decision

Build a single canonical HAR 1.2 emitter, `go-fleet-har-builder` (host
port 18157, mesh-0exec, category fleet-evidence), that every
evidence-producing service routes through. API:

- `POST /har/from-pair`    `{request, response, timings?, started_at?}` → HAR with one entry
- `POST /har/from-pairs`   `[{...pair}, ...]`                            → HAR with N entries
- `POST /har/from-finding` `{id?, kind?, severity?, pair?, pairs?}`      → HAR; each entry's comment carries the finding metadata

The HAR document is RFC-style compliant with HAR 1.2 (see
http://www.softwareishard.com/blog/har-12-spec/): `log.version="1.2"`,
`log.creator={name, version}`, per-entry `request{}` / `response{}` /
`cache{}` / `timings{}` with all required fields populated and sizes
defaulting to `-1` when unknown (spec-conformant absent-value). An
inline schema validator (no external JSON-schema dep) asserts the
shape on every response and on `/selftest`.

Pair with `go-fleet-body-redactor` (ADR-0004): every header and body
is round-tripped through the redactor before being embedded in the
HAR. Env var `BODY_REDACTOR_URL` (default
`http://go-fleet-body-redactor:18154`). When the redactor is
unreachable the builder **fails open** (HAR still emitted) and
surfaces `degraded:["redactor-down"]` in the response envelope AND in
`log.comment` so downstream consumers cannot silently trust the
output.

## Consequences

**Positive.** One canonical evidence format consumable by every
external triage tool. One redaction pipeline (the body-redactor)
gates every emitted document instead of each emitter rolling its
own. Schema validation is enforced on every output AND probed via
`/selftest` so external monitors catch shape regressions.

**Negative.** Cross-service hop adds latency on the evidence-emission
path (one HTTP call to `har-builder`, which makes one or two
internal calls to `body-redactor`). Acceptable: evidence emission is
not on the hot user path.

**Mitigations.** Failure-mode is fail-open — `har-builder` down means
callers fall back to their original ad-hoc shape (see migration
path); `body-redactor` down means HAR is emitted with raw secrets
but the `degraded` flag and `log.comment` warn downstream.

## Migration path (service ADRs)

Callers integrate by reading `HAR_BUILDER_URL` (default
`http://go-fleet-har-builder:18157`) and calling whichever endpoint
matches their evidence shape. Required env vars on the caller side:

| Var               | Default                                  | Purpose                                             |
|-------------------|------------------------------------------|-----------------------------------------------------|
| `HAR_BUILDER_URL` | `http://go-fleet-har-builder:18157`      | Service base URL. Empty / unreachable → fail-open.   |

**Fail-open contract.** When `har-builder` is unreachable or
returns a non-2xx, callers MUST:

1. Continue emitting their existing ad-hoc evidence shape verbatim.
2. Include `har: null` in their finding payload so consumers can
   distinguish "HAR not requested" from "HAR build failed".
3. Optionally surface `har_builder_status: "down"` so a downstream
   audit can count outages.

Callers MUST NOT block their main path on `har-builder` availability —
this is an evidence enrichment, not a precondition.

**Adoption order** (lowest-risk first):

1. `go-pentest-walkthrough` — read-only triage tool, easy rollback.
2. `go-pentest-exploit-verifier` — internal to the fleet, low
   external blast radius.
3. `go-pentest-submit-bot` — attaches HAR to platform submissions;
   biggest external win.
4. `go-pentest-http-replay` — highest volume; migrate last once the
   other three have shaken out edge cases.

## Alternatives considered

- **Per-service HAR emission.** Each evidence-producer adds its own
  `to-har.go`. Rejected: 4× the chance of HAR shape drift; 4× the
  chance of incomplete header redaction (ADR-0004 anti-pattern); we
  already saw `walkthrough` and `http-replay` redact different
  header sets.
- **Generic JSON schema validator dep.** Rejected for now: pulls a
  ~3 MB tree into a service whose only schema concern is HAR. Inline
  validator is ~150 LoC and tracks exactly what we need; revisit if
  we add more output formats.
- **Embed HAR generation into the body-redactor.** Rejected:
  conflates two responsibilities (data scrubbing vs. format
  emission). Body-redactor stays single-purpose; HAR builder calls it.
- **Use HAR 2.x.** Rejected: 1.2 is what Burp / ZAP / Postman /
  Chrome DevTools actually read. 2.x exists but adoption in triage
  tools is thin.

## References

- ADR-0001 — ADR process
- ADR-0002 — twenty fleet primitives (HAR builder is Tier A #5)
- ADR-0004 — canonical body redactor (`go-fleet-body-redactor`); this
  ADR builds on that contract.
- HAR 1.2 spec — http://www.softwareishard.com/blog/har-12-spec/
- `FLEET-FUTURE-TOOLS.md` → Tier A → `go-fleet-har-builder`
- Repo — https://github.com/baditaflorin/go-fleet-har-builder
