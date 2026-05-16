# ADR-0008 — Canonical PoC curl emitter

**Status**: Accepted (2026-05-16)
**Service**: `go-fleet-poc-curl` (Tier A #6 in `FLEET-FUTURE-TOOLS.md`)
**Port**: 18158 (host & container)
**Mesh**: `mesh-0exec`
**Repo**: <https://github.com/baditaflorin/go-fleet-poc-curl> (private)

## Context

Every bug-bounty submission, walkthrough writeup, exploit verifier
output, and dashboard finding card needs a copy-paste `curl` command —
the "PoC" the receiving security team will paste into a terminal to
reproduce the issue. Today each consumer builds the string ad-hoc.
Quality is uneven:

- **Shell escaping is rolled by hand.** Several services use
  `fmt.Sprintf("'%s'", url)` and break the moment a URL or header
  value contains a single quote. Worse, a body containing `';
  rm -rf /tmp; '` can flip a benign PoC into command injection on
  paste — observed during a 2026-04 incident review when a fuzzed
  body landed in a Slack channel.
- **Sensitive headers leak.** Cookie and Authorization values get
  pasted verbatim. Bug-bounty SOPs require redaction; nobody
  centralised it.
- **No "destructive-method" gate.** `DELETE /v1/items/42` curls have
  shipped without a "this is destructive" callout, leading to two
  near-misses where a copy-paste replay deleted production rows.
- **No risk_warnings surface at all.** Consumers each invent their
  own warning vocabulary, or skip it.

The breakage is in the seam between consumers and the curl-text
boundary. Fixing it inside each consumer multiplies bugs.

## Decision

Ship a dedicated primitive — `go-fleet-poc-curl` — that owns the
entire emit-curl seam:

1. **Single canonical emitter.** All consumers POST
   `{http_request, options?}` to `/curl`; nobody else builds curl
   strings.
2. **POSIX single-quote escaping for every user-controlled byte.**
   URL, header names, header values, and body all go through a
   single `shellSingleQuote` that closes/escapes/reopens (`'\''`)
   embedded quotes. `$`, backtick, `\`, and `!` cannot interpolate.
3. **Redactor-composed by default.** The emitter calls
   `go-fleet-body-redactor` (`BODY_REDACTOR_URL`, default
   `http://go-fleet-body-redactor:18154`) for bodies, and applies a
   local headers-side mirror for `Cookie` / `Authorization` /
   `X-API-Key` / `X-Auth-Token`. The redactor is *composed*, not
   bundled — keeping the body-redaction policy in one place.
4. **Fail-open on redactor outage.** If the redactor errors or times
   out, the emitter still returns a curl (with the unredacted body)
   but adds `degraded: ["poc-curl-redactor-down"]` so the dashboard
   can warn the operator. Closing this seam to 5xx would degrade
   every consumer's user-facing flow when an internal service
   blips; the safer default is "ship the PoC, flag the risk".
5. **Parse-only shell validation.** Every emitted curl is
   `bash -n -c "<curl>"` parseable. `/selftest` enforces this
   against three baked-in cases (GET, POST+body, DELETE+auth) and
   returns 200/503 — the same shape the autofix smoke gate
   already expects.
6. **Risk warnings are the side channel.** The structured response
   carries `risk_warnings`: `contains-cookie`, `contains-auth`,
   `destructive-method`, `large-body-truncated`, `binary-body`,
   `non-https`. Consumers render them next to the PoC.
7. **`include_secrets=true` is an explicit opt-in.** Some exploit
   replays genuinely need the raw credential. The flag is allowed,
   but every response includes a `notes` warning that the curl
   carries unredacted secrets — so the warning rides with the
   string into any UI/log that displays it.

## Migration

Consumers gain an env-keyed pointer; no SDK ships yet (the request
shape is small enough to hit directly):

```bash
POC_CURL_URL=http://go-fleet-poc-curl:18158
```

Per-consumer adoption:

1. Replace the local `buildCurl(...)` function with a POST to
   `${POC_CURL_URL}/curl`.
2. **Fail-open**: on any network error or 5xx from the emitter,
   render the raw curl string the consumer would have built and
   tag it `degraded: ["poc-curl-down"]`. This preserves the
   pre-ADR behaviour as a floor; the worst case is "no better
   than today", never "worse than today".
3. Render `risk_warnings` next to the curl in whatever UI the
   consumer owns.

Top-3 first consumers (by callsite count / current ad-hoc-ness):

- **walkthrough** (`go_walkthrough_*`) — writeup builders embed a
  curl per replay step; today each step builds its own string.
- **submit-bot** (`go-submit-bot`) — bug-bounty platform submitters
  attach a curl PoC per finding.
- **exploit-verifier** (`go-exploit-verifier`) — exploits ship with
  a reproducer; verifier currently rolls curl by hand.

(Secondary: dashboard finding cards, http-replay history view.)

## Consequences

**Positive**:

- Shell-escape bugs become a one-line fix in this repo instead of
  hunting through every consumer.
- Secret redaction is uniform; rotation of the policy is one
  redeploy.
- `risk_warnings` become a stable taxonomy fleet-wide.
- `bash -n` gate on `/selftest` means a regression cannot ship a
  syntactically broken curl.

**Negative**:

- One more network hop on the PoC path (≤ ~10 ms in-mesh).
- Mitigated by fail-open: a poc-curl outage degrades to today's
  behaviour, never worse.
- `include_secrets=true` is a sharp edge; documented and
  log-warned.

**Neutral**:

- `multipart/form-data` with file uploads, gRPC, and websockets
  cannot be faithfully represented as a one-liner — that's a curl
  limit, not a poc-curl one. For those, consumers should use
  `go-fleet-har-builder` (ADR pending).

## Hard rules (codified in tests)

- Every emitted curl MUST be `bash -n` parseable. Enforced by
  `TestCurl_*` (parse-check on every asserted output) and
  `/selftest` (parse-check on every baked-in case).
- NEVER ship raw unredacted `Cookie` / `Authorization` /
  `X-API-Key` / `X-Auth-Token` in a PoC unless
  `include_secrets=true` is explicit. Enforced by
  `TestCurl_RedactsCookieByDefault`.
- `BODY_REDACTOR_URL` outage MUST NOT 5xx the emitter. Enforced by
  `TestCurl_RedactorDown_DegradedFlag`.

## Status

- [x] Service shipped (`0.1.0`).
- [x] `/selftest` green locally.
- [ ] Top-3 consumers migrated (tracked separately).
- [ ] `services-registry/services.json` entry added (handled by
      `bin/generate.py` once `mesh-0exec` + `category-infrastructure`
      topics are set on the repo).
