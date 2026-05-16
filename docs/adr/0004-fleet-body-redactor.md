# ADR-0004 — Canonical sensitive-data redaction primitive (`go-fleet-body-redactor`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-session-batch4-2026-05-16
* **Tags**: fleet-infra, evidence, security, redaction, primitive

## Context

At least four fleet services maintain their own header-redaction tables and body-secret detectors:

- **`go-pentest-exploit-verifier`** — redacts Cookie / Authorization before persisting evidence; uses a 6-char prefix-keep shape.
- **`go-pentest-walkthrough`** — `internal/templates.RedactHeaders` table covers 7 headers (missing `X-Admin-Token`); 6-char prefix.
- **`go-http-replay`** — strips Cookie+Authorization only; doesn't touch `X-API-Key` or `Set-Cookie`. JWT bodies pass through intact.
- **`go-pentest-findings-store`** — homegrown redactor; missed `Set-Cookie` until 2026-05-08 incident (full session cookie leaked into a finding attachment that shipped to a triage queue).

The exposure is real: a header that survives one redactor but not another means "the evidence trail is sometimes safe, sometimes not, depending on which service handled it last." That's strictly worse than no redaction (where callers know to be careful) — it's the false sense of safety failure mode. The 2026-05-08 `findings-store` incident was the canonical case; a real `session=...` cookie reached an external triage queue because the redactor was the older 5-header version while `walkthrough` (which had been the auth source-of-truth for this writeup) had been updated to 7 headers a sprint earlier. The two never resynced.

Body-content redaction is even more inconsistent. None of the four services detected JWTs in body content; AWS keys in JSON bodies passed through every one of them. `go_apikey_scanner` has the FP-tightened canonical pattern set (44 vendor patterns), but it's a one-way detector — no redaction primitive exposed.

## Decision

Ship `go-fleet-body-redactor` (port 18154, `mesh-0exec`, `category: fleet-infra`) as a stateless HTTP service that consumers call instead of redacting locally. API:

```
POST /redact {kind:"request|response", method?, url?, headers?, body?, extra_sensitive_headers?}
  → {kind, method?, url?, headers?, body?, redactions:[{field, kind, before_prefix8, after}]}

POST /redact-batch [items...]
  → {count, results:[...]}

GET /selftest  → 200/503 (15-case synthetic gate; 503 fails fleet-runner deploy)
GET /health, /version, /metrics  (from go-common/server)
```

Default sensitive headers (case-insensitive, lifted from `go-pentest-walkthrough` + `X-Admin-Token`):
`Cookie, Set-Cookie, Authorization, X-API-Key, X-Auth-Token, X-Admin-Token, X-CSRF-Token, Proxy-Authorization`.

Body patterns (lifted from `go_apikey_scanner/vendors.go`, ordered most-specific-first):
PEM private key blocks; AWS access key ids (AKIA/ASIA/AGPA/...); GitHub PATs (ghp_/gho_/ghu_/ghs_/ghr_/github_pat_); Stripe (sk_live_/sk_test_/rk_); Slack (xox*); GCP (AIza...); SendGrid (SG.x.y); Mapbox secret (sk.eyJ...); npm (npm_...); generic JWT (eyJ...eyJ...sig).

Replacement shape: `<prefix8>…[REDACTED]` (or `[REDACTED]` if value ≤8 chars). The 8-char prefix lets triage discriminate token classes ("this was a JWT" vs "this was an AWS key") without re-exposing the secret tail.

The evidence trail (`redactions[]`) is the contract — callers can audit exactly what got matched and confirm the redactor did its job without ever logging the secret.

## Consequences

**Positive**:
- One place to update when a new sensitive header pattern appears (e.g. add `X-Forwarded-User` next sprint, every consumer is fixed in one deploy).
- Consistent redaction shape across the whole fleet — evidence from `walkthrough` looks identical to evidence from `exploit-verifier`, no more "is this redacted or just truncated?" ambiguity.
- The `/selftest` gate catches pattern regressions before they ship — fleet-runner deploy refuses to roll a binary whose pattern set has regressed.
- Removes ~200 lines of duplicated regex from 4+ consumer services on adoption.

**Negative**:
- One more network hop per evidence write. Mitigation: redaction is sub-millisecond (stateless, no I/O); the round-trip is the cost, not the work. For latency-sensitive paths (e.g. live request mirroring) callers can keep a `safehttp.Client` with a 100ms timeout and fail open.
- Centralizing redaction = centralizing risk if the service goes down. Mitigation: explicit fail-open contract (below).
- The redactor sees raw evidence in transit. Mitigation: stateless container, no log retention of bodies, `read_only: true` filesystem, `cap_drop: ALL`, no outbound network. The service is the most-hardened compose definition in the fleet.

**Mitigations**:
- Fail-open contract (caller MUST surface `degraded: ["redactor-down"]`). Fail-closed would block bug bounty submissions on a transient infrastructure outage, which is worse than the leak risk for our internal-mesh evidence trail.
- `/selftest` gate prevents regressed patterns from reaching prod.
- The redactor itself is stateless; restart is instantaneous; no state migration risk.

## Migration path (service ADRs)

Consumers read `BODY_REDACTOR_URL` from env (default `http://body-redactor:18154` in the docker network, or `https://fleet-body-redactor.0exec.com` from outside the mesh). Canonical client snippet:

```go
type RedactorClient struct {
    URL    string
    Client *http.Client // 2s timeout
    Key    string       // X-API-Key for the keystore
}

func (rc *RedactorClient) Redact(ctx context.Context, in RedactInput) (RedactOutput, error) {
    body, _ := json.Marshal(in)
    req, _ := http.NewRequestWithContext(ctx, "POST", rc.URL+"/redact", bytes.NewReader(body))
    req.Header.Set("Content-Type", "application/json")
    req.Header.Set("X-API-Key", rc.Key)
    resp, err := rc.Client.Do(req)
    if err != nil { return in.AsOutput(), err } // fail open
    defer resp.Body.Close()
    if resp.StatusCode != 200 { return in.AsOutput(), fmt.Errorf("status %d", resp.StatusCode) }
    var out RedactOutput
    return out, json.NewDecoder(resp.Body).Decode(&out)
}

// In the caller:
out, err := rc.Redact(ctx, evidence)
if err != nil {
    log.Warn("redactor down, surfacing raw evidence", "err", err)
    response.Degraded = append(response.Degraded, "redactor-down")
    response.Evidence = evidence // raw — caller MUST surface degraded flag
} else {
    response.Evidence = out
}
```

**Fail-open default with mandatory degraded flag.** Surface the flag in every response shape that carries evidence; UIs and downstream consumers MUST treat `degraded` as a "read carefully" signal. A future ADR may move payment-flow services to fail-closed once the redactor's TRL hits 8+.

Top 3 consumers to adopt first (highest evidence-leak surface today):
1. **`go-pentest-findings-store`** — the canonical evidence sink; the 2026-05-08 cookie-leak incident is direct motivation.
2. **`go-pentest-walkthrough`** — emits writeups that go to external platform triage; current redactor missing `X-Admin-Token`.
3. **`go-pentest-exploit-verifier`** — produces per-replay evidence trails; currently strips headers but not body content.

After those land, the rollout continues to `go-http-replay`, `go-pentest-submit-bot`, and eventually every service that emits a `redacted_*` field today.

## Alternatives considered

- **Library, not service.** Ship the redactor as a `go-common/redactor` package every consumer imports. Rejected because pattern updates would require bumping every consumer's go-common version and redeploying — the exact "130 PRs" problem that the fleet's cardinal rule (change the library, not the consumers) is meant to avoid. A service is one deploy, a library is many.
- **Fold into `go-common/middleware`.** Same problem as above plus couples redaction to HTTP middleware (the redactor is also used in non-handler code paths like cron-scanned findings-store entries).
- **Use trufflehog-style continuous-corpus updates inside each service.** Rejected for the same library-vs-service reason; also unnecessary for the day-1 shape (we need 10 patterns at TRL 6, not 800 at TRL 4).
- **Fail-closed by default.** Rejected — the redactor going down should not block evidence emission entirely; the degraded-flag contract is the safer real-world tradeoff. Re-visit at TRL 8 with multi-instance deployment.

## References

- ADR-0002 — Twenty fleet primitives (this is Tier S #2 from that list)
- `go_apikey_scanner/vendors.go` — canonical FP-tightened provider pattern set
- `go-pentest-walkthrough/internal/templates/templates.go` — original `RedactHeaders` table
- 2026-05-08 incident: `findings-store` redactor mismatch → session cookie reached external triage queue
- `FLEET-FUTURE-TOOLS.md` Tier S #2 (the spec this ADR implements)
