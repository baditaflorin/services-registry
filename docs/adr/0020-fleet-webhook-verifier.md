# ADR-0020 — Canonical inbound-webhook signature verifier (`go-fleet-webhook-verifier`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-opus-4-7-session-2026-05-16
* **Tags**: fleet-infra, security, webhooks, primitives

## Context

Every inbound-webhook consumer currently rolls per-platform signature
verification on its own. GitHub gets `X-Hub-Signature-256`, Slack gets
the `v0:<ts>:<body>` dance plus a 5-minute skew window, Stripe gets
`t=<ts>,v1=<hex>` parsing, HackerOne and Bugcrowd each get their own
header. Done wrong — non-constant-time comparison, missing skew check,
mis-pinned-`v1`-vs-`v0` Slack parser, body-canonicalisation drift —
the upstream platform's signature stops being a real authentication
control and the consumer accepts forged webhooks. We've already seen
two near-misses in the broader ecosystem (one fleet-internal review
caught `bytes.Equal` on signature bytes in a draft `submit-bot`
handler; one third-party CVE this quarter hinged on the same mistake)
and `FLEET-FUTURE-TOOLS.md` lists this as Tier B #18 because the
class of bug compounds: every consumer that re-rolls it is an
independent chance to get it wrong, and a regression in one consumer
is invisible to the others.

## Decision

Ship `go-fleet-webhook-verifier` on `mesh-0exec` port 18170 with a
single primary endpoint:

```
POST /verify
  {source, headers, body|body_b64, secret|secret_key_ref,
   signature_header?, max_skew_seconds?}
→ {authentic, signature_method, claims?, reason?, source}
```

`source ∈ {github, slack, stripe, h1, bugcrowd, generic-hmac}`.
Every per-platform verifier compares signature bytes with
`crypto/hmac.Equal` — **constant-time, no exceptions**. Slack and
Stripe additionally enforce `|now - timestamp| ≤ 5min` per upstream
docs (overridable via `max_skew_seconds` per request). Stripe
tolerates multiple `v1=` entries on one header line (the documented
key-rotation shape).

Companion endpoints: `GET /sources` (lists the six supported schemes
with header names + method strings), `GET /selftest` (signs and
verifies a canonical body for every source, plus a tampered-body
negative case for each; returns 503 on any regression). `GET /health`,
`GET /version`, `GET /metrics` come from `go-common/server`
automatically — never re-implement.

Optional `FLEET_SECRETS_URL` env composes the verifier with
`go-fleet-secrets`. When set, callers can pass `secret_key_ref`
instead of inlining the signing secret; the verifier resolves
`GET ${FLEET_SECRETS_URL}/secrets/{ref}` and forwards the caller's
`X-Auth-User` so the per-secret consumers ACL still applies.
When unset, `secret_key_ref` returns `reason: "secret-ref-unsupported"`
— never silently downgrades.

## Consequences

**Positive**

- One signature surface across every inbound-webhook consumer; the
  constant-time-comparison invariant is enforced in one binary and
  one test (`TestVerify_ConstantTime`).
- New platforms (Twilio, Linear, Shopify) land as one more case in
  `VerifyForSource` + `Sign`, with one TP+TN test pair — never as
  one more place a consumer rolls its own.
- Stale-timestamp rejection is part of the signature contract, not an
  optional check a consumer might forget.
- Vault-resolved secrets via `secret_key_ref` mean consumers don't
  need to mount webhook signing keys at all — only the verifier does.

**Negative / mitigations**

- One more network hop on the inbound-webhook path. Mitigated by the
  verifier's tight in-process work (no I/O on the hot path when
  `secret` is inlined) and by the fact that webhook receivers are
  not latency-sensitive (the upstream platform retries on 5xx).
- New single point of failure for inbound webhooks. Mitigated by
  `/selftest` (caught by every deploy's smoke gate) and by the fact
  that a consumer can still fall through to inline verification using
  `crypto/hmac.Equal` directly if the verifier is down — at the cost
  of giving up the centralised invariants.
- Curated set of six sources. Anything else takes a PR. We accept
  this — the alternative is N-way drift between consumers, which is
  strictly worse.

## Migration path (consumers)

Set `WEBHOOK_VERIFIER_URL` in the consumer's env (canonically
`http://go-fleet-webhook-verifier:18170` on the dockerhost mesh, or
`https://go-fleet-webhook-verifier.0exec.com` over the gateway). On
every inbound webhook, before acting:

```go
// Pseudo-code; the canonical Go client lives in go-common/webhookverify
// (to be added in a follow-up bump, same pattern as go-common/apikey).
res, err := webhookverify.Verify(ctx, source, r.Header, rawBody, secretKey)
if err != nil || !res.Authentic {
    http.Error(w, "forbidden", http.StatusForbidden)
    return
}
```

**Fail-closed default**: any non-200 from the verifier, any
`authentic: false`, or any transport error MUST reject the inbound
webhook. Never fall through to "process anyway" — that re-opens the
exact bypass this ADR exists to close.

Initial consumers to wire up (top 3):

1. **`go-fleet-submit-bot`** — receives HackerOne / Bugcrowd
   disclosure-flow webhooks; today rolls its own H1 signature check.
2. **`go-fleet-notify`** — receives GitHub webhooks for the
   issue-comment / PR-review fan-out; today rolls its own.
3. **`go-fleet-stripe-incoming`** — receives Stripe webhooks for
   billing-side events; today inlines `v1` parsing and skips the
   `t=` skew check entirely.

## Alternatives considered

1. **Per-consumer crypto (status quo).** Six consumers × six sources
   each = 36 places to maintain. Already produced one near-miss
   (`bytes.Equal` on signature bytes) — the failure mode is silent
   and the blast radius is "all forged webhooks accepted by this
   consumer". Strictly worse.
2. **`go-common/webhookverify` as a library, no service.** Catches
   the constant-time-comparison invariant but loses (a) the
   `/selftest` regression gate that every deploy runs, (b) the
   secrets-vault composition that means consumers never hold signing
   keys, and (c) the `/sources` discovery surface that lets a
   dashboard render the list. We will likely ship the library
   wrapper too (so consumers don't have to handroll HTTP), but the
   service is the source of truth.
3. **Vendor SDKs per platform.** GitHub's official Go SDK, Slack's
   `slack-go`, etc., each verify their own signatures. Pulls six
   transitive-dep trees into every consumer for one HMAC each.
   Drift compounds at upgrade time. Rejected.

## References

- `services-registry/FLEET-FUTURE-TOOLS.md` §18 — initial scoping
- `services-registry/docs/adr/0001-adr-process.md` — ADR conventions
- `go-fleet-webhook-verifier/verify.go` — per-platform verifiers
- `go-fleet-webhook-verifier/verify_test.go` — 18 tests, including
  `TestVerify_ConstantTime` and `TestSelftest_RoundTrip`
- `go-fleet-secrets` — vault for `secret_key_ref` resolution
