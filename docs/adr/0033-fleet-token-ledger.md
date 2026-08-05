# ADR-0033 — Fleet token ledger: prepaid balances, x402-shaped 402s, swappable settlement

* **Status**: Accepted
* **Date**: 2026-08-05
* **Authors**: claude-sonnet-5 (with florin)
* **Tags**: billing, payments, mcp, agent, go-common, registry

## Context

ADR-0032 gave AI agents a single MCP surface to call fleet services
through, but nothing meters or charges for that usage — every call is
free once a caller holds a keystore API key. As the fleet opens more
services to agent callers (human or AI), there needs to be a way for
an account to hold a prepaid token balance and for a service to
refuse a call when that balance is exhausted.

Separately, the industry has converged on **HTTP 402 Payment
Required** as the wire format for exactly this problem. Coinbase
launched the **x402** protocol in February 2026; by April 2026 it had
69,000 active agents and ~$50M cumulative volume, and Coinbase
contributed it to a new **x402 Foundation** under the Linux
Foundation, with Stripe, Visa, Mastercard, Google, AWS, and Microsoft
as founding members. Stripe's own **Machine Payments Protocol (MPP)**
(March 2026) is explicitly layered on top of x402's 402 handshake as
the wire format, not a competing one. In short: 402 with an
`accepts[]` payment-requirements array is no longer a speculative
RFC footnote, it is the shape both fiat (Stripe) and crypto rails are
converging on.

Given that, shaping this fleet's own 402 response to look like x402's
`PaymentRequirements` now — even though there is no real settlement
rail behind it yet — avoids having every future caller's HTTP
contract change again when Stripe or x402 settlement gets wired in.

## Decision

New service **`go-fleet-token-ledger`** (`fleet-token-ledger.0exec.com`,
port 18208, `mesh-0exec`, category `infrastructure`).

**Identity**: reuses the existing keystore identity, nothing new.
The account key is `header.AuthUser` (`X-Auth-User`), the same header
every other `mesh-0exec`/`mesh-0crawl` service already trusts
(`server.WithKeystoreAuth`). No second identity system, no new API
key type.

**Storage**: pure-Go SQLite (`modernc.org/sqlite`, no cgo — same
choice as `go-fleet-persona`), one docker volume. Two tables:
`accounts(account PK, balance)` materialised for O(1) reads, and an
append-only `ledger_entries(account, delta, reason, created_at)` audit
trail every mutation writes to in the same transaction.

**Wire surface**:

```
GET  /v1/balance                → 200 {"account","balance"}
POST /v1/charge                 → 200 {"account","charged","balance"}
                                   402 x402-shaped PaymentRequirements (see below) if balance < amount
POST /v1/topup   (X-Admin-Token) → 200 {"account","credited","balance"}
GET  /selftest                  → sqlite ping check
```

`/v1/charge` is idempotent via `go-common/server.WithIdempotencyKey`
(the existing fleet primitive — no hand-rolled idempotency table).

**402 response shape** (deliberately field-compatible with x402's
`PaymentRequirements`, so a caller that already speaks x402 can parse
this without a fleet-specific branch):

```json
{
  "x402Version": 1,
  "error": "insufficient_balance",
  "accepts": [{
    "scheme": "fleet-manual",
    "network": "internal",
    "maxAmountRequired": "<amount>",
    "resource": "/v1/topup",
    "description": "Insufficient token balance. Request a top-up (admin today; Stripe/x402 later).",
    "mimeType": "application/json",
    "payTo": "fleet-token-ledger",
    "maxTimeoutSeconds": 60,
    "asset": "internal-token",
    "extra": {"account": "<account>"}
  }]
}
```

`scheme: "fleet-manual"` is intentionally honest that this is not yet
a verified onchain or Stripe payment — see TRL note below.

**Settlement seam**: a one-method `Provider` interface —

```go
type Provider interface {
    Name() string
    Topup(ctx context.Context, account string, amount int64) error
}
```

`manual` (admin-token-gated, no real money moving) ships first and is
the only implementation in this phase. `stripe` (MPP) and `x402`
(USDC) implementations slot in later behind the same interface,
selected by `LEDGER_PROVIDER` env var — the HTTP contract for
`/v1/charge` and the 402 shape does not change when that happens,
only what's allowed to call `/v1/topup` successfully.

**Metering is opt-in, not enforced by the gateway.** A service that
wants to charge for a call makes its own `POST /v1/charge` call to
the ledger before doing expensive work. The MCP gateway (ADR-0032)
and per-service handlers are not modified in this phase — wiring
metering into the gateway centrally is a follow-up, not blocked on
this ADR.

## Consequences

**Positive**: one 402-speaking source of truth instead of N
bespoke rate-limiters; reuses the keystore identity verbatim (zero
new auth surface); the 402 shape is forward-compatible with the
wire format Stripe/Coinbase/Visa/Mastercard have already converged
on, so real settlement is a `Provider` swap, not a contract change.

**Negative / Mitigations**: phase 1 has no real payment rail —
`/v1/topup` is admin-only, so this does not yet let an external human
or agent actually buy tokens with money. That's intentional (the
user's own framing: "for now to have the service that could do
that") — the ledger and the 402 contract are the prerequisite
plumbing, Stripe/x402 wiring is separate future work. Metering is
opt-in per service, so a service that doesn't call `/v1/charge`
stays free — this ADR does not retroactively meter the existing
fleet.

**TRL**: ships at TRL 4 ("developing") — real ledger math and tests,
but zero services call `/v1/charge` yet and zero real payment rails
are wired. Honest per the fleet's TRL-evidence convention (see
CLAUDE.md "TRL — technology readiness level").

## Migration path

Net-new service; nothing existing changes. A service that wants
metered calls: call `POST /v1/charge {"amount": N}` with the caller's
own `Authorization`/`X-API-Key` header before doing the expensive
work; treat a 402 response as "tell the caller to top up" (surface
the `accepts[]` array, don't silently degrade). `ADMIN_TOKEN` env var
gates `/v1/topup`; `LEDGER_PROVIDER` selects the settlement backend
(`manual` today).

## Alternatives considered

### A — Wait for a real Stripe/x402 integration before building anything

Rejected: the user's own request was to have the *service* exist now,
with real money wired up later. Waiting means every future caller
gets designed against a moving target instead of the stable
x402-shaped contract this ADR ships today.

### B — Meter calls inside the MCP gateway (`go-fleet-mcp-gateway`) directly

Rejected for phase 1: the gateway (ADR-0032) is deliberately scoped to
aggregation and holds no fleet API key of its own — folding billing
state into it would give it a second job and a reason to hold
sensitive state it doesn't have today. Kept as a candidate follow-up:
the gateway could call the ledger the same way it forwards to
upstreams, once a real settlement rail justifies fleet-wide
enforcement.

### C — New API-key/identity system scoped to the ledger

Rejected: the keystore is already "the fleet's single point of
compromise... treat it like a CA root" (CLAUDE.md). A second identity
system for billing would fragment auth for no benefit — `X-Auth-User`
already uniquely identifies every caller.

## References

- ADR-0032 — Fleet-wide MCP support (gateway, `X-Auth-User` reuse)
- `CLAUDE.md` — "Auth — both container meshes use the same keystore"
- `go-fleet-persona` — precedent for pure-Go SQLite storage in a
  small fleet-infra service
- x402 protocol / x402 Foundation (Linux Foundation, April 2026);
  Stripe Machine Payments Protocol (March 2026) — external, cited for
  the 402 `accepts[]`/`PaymentRequirements` shape this ADR mirrors
- `go-fleet-token-ledger` — new repo, port 18208
