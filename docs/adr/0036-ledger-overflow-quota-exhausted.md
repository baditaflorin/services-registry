# ADR-0036 — Ledger-backed overflow on the keystore's quota-exhausted path

* **Status**: Accepted
* **Date**: 2026-08-06
* **Authors**: claude-sonnet-5 (with florin)
* **Tags**: billing, auth, keystore, ledger, go-common

## Context

`go-apikey-service`'s `verifyHandler` (`main.go:242-309`) is the fleet's
`/verify` endpoint — called synchronously on every `mesh-0exec`/`mesh-0crawl`
request via nginx `auth_request`. When a key's `use_count` reaches its
`use_limit`, it hard-rejects with 401 "usage limit reached" (`main.go:272-276`).
The service's own README calls it the fleet's "single point of compromise...
treat it like a CA root."

ADR-0033 shipped `go-fleet-token-ledger` (prepaid balances, x402-shaped 402s)
and a `go-common/ledger` client, but scoped it as **opt-in per metering
service**, explicitly declining to fold it into the auth gateway/keystore
("Alternative B" in that ADR — rejected for phase 1, left as a candidate
follow-up "once a real settlement rail justifies fleet-wide enforcement").
This ADR is that follow-up proposal for one narrow slice: letting a
ledger-topped-up account push past its key's `use_limit` instead of being
hard-rejected.

This is a bigger deal than a typical service integration because of *where*
it lands: the keystore is the one component every other fleet service
trusts unconditionally. Making its hot path depend synchronously on a
brand-new (TRL 4, ~1 day old, zero production callers) external service is
a real change to the keystore's risk profile, not just a feature add.

### Corrections to prior framing (verified against `origin/main` 2026-08-06)

- **`go-apikey-service` is already on `go-common v0.78.0`**, not v0.68.0.
  `go-common/ledger` shipped in v0.76.0. **No dependency bump is needed** —
  the keystore already has import access to the ledger client. Open
  question 4 below is answered by this fact alone.
- **The ledger's registry port is correctly 18314** (`services.json`,
  `services-registry`). ADR-0033's own prose still says 18208 (the
  pre-collision-fix port) in one spot, and **`go-common/ledger.go`'s
  `New()` still hardcodes `LEDGER_SERVICE_URL` default to
  `http://localhost:18208`** (verified in `go-common/ledger/ledger.go:130`).
  Fixed as part of this ADR's Phase 1 — see the implementation plan.
- **`ledger.Client.Charge` is the only method that exists.** No
  balance-only read in the Go client today, confirmed in
  `go-common/ledger/ledger.go`. The ledger *service* does expose
  `GET /v1/balance` per ADR-0033's wire surface and its own `handler.go`
  (`HandleBalance`) — the gap is client-side, not service-side. Confirmed
  that `HandleBalance`/`HandleCharge` both read identity from
  `header.AuthUser`, populated by the ledger's own
  `server.WithKeystoreAuth("default_token")` middleware — so a direct
  client call with `Authorization: Bearer <cred>` (bypassing nginx) is
  verified correctly by the ledger service itself, the same as any other
  `mesh-0exec` backend called directly.
- **Default HTTP timeout on the ledger client is 5s**
  (`go-common/ledger.go:132`), and the package doc says explicitly: *"the
  client never decides your degrade policy — pick your own."* Nothing
  about this client is tuned for a synchronous auth-gate hot path.
- **TRL 4, 11 tests, "zero fleet services call `/v1/charge` yet"** — this
  is the ledger's own `trl_evidence` in `services.json` as of 2026-08-05.
  This would be the ledger's first real caller, and its first caller would
  be the fleet's highest-QPS, most-trusted endpoint.

## Decision

Add an **opt-in, config-gated overflow check** to `verifyHandler`: when a
key would otherwise be rejected for `use_count >= use_limit`, and overflow
is enabled, check the calling account's ledger balance with a tight budget;
grant the request through if balance > 0, otherwise reject as today.

This does **not** replace `use_limit` — it is a second gate that only
engages after the first one would already reject.

### Answers to the five open design questions

**1. Overflow vs. replacement for `use_limit`?**
**Overflow.** `use_limit` is an admin-set governance/rate control on a
specific key; a ledger balance is a property of the account. Replacing
`use_limit`'s semantics fleet-wide ("pay to bypass any limit") would
require re-auditing every existing key's `use_limit` as now-soft instead
of hard, and changes what admins were promising when they set it. Overflow
preserves today's behavior byte-for-byte in the ledger-absent or
ledger-down case (see Q2) and only adds a new path when a request would
otherwise have failed. This also matches ADR-0033's own framing of the
ledger as a *prepaid top-up*, not a replacement access-control system.

**2. Fail-open vs. fail-closed, and what timeout?**
**Fail-open, with a budget in the 150-300ms range — not the client's 5s
default.** Three reasons:
- CLAUDE.md already codifies the fleet's canonical pattern for intra-mesh
  calls: *"short timeout (1-3s), fail-open semantics, append
  `<primitive>-down` to `degraded[]`"* — and that guidance is for feature
  work calling siblings, not for a CA-root-equivalent hot path. `/verify`
  should be tighter than that baseline, not looser.
- A stuck/slow ledger call held synchronously inside `/verify` at scale
  reproduces the shape of the 2026-05-26 SQLite incident (goroutine/thread
  pileup under load taking down the host) — this time triggered by a
  network call instead of an fcntl lock. The keystore's own CLAUDE.md
  section on that incident is the reason `/verify` is unusually careful
  about anything blocking per-request.
- Fail-closed here means a brand-new, TRL-4, never-yet-called-in-prod
  service being slow or down can take the *entire fleet* down via its most
  trusted endpoint. That blast radius is not justified by what overflow
  buys (a nicer error for a small number of topped-up accounts). On
  ledger-unavailable/timeout, behavior must degrade to exactly today's
  behavior: hard reject at `use_limit`, nothing worse.
- The exact nginx `auth_request` timeout budget for
  `apikey-service.0exec.com` should still be confirmed against the live
  webgateway config before this ships to enforcing mode (private repo,
  not available at design time) — flagged in the implementation plan's
  Phase 0.

**3. New `Balance`-only read method on `go-common/ledger`, or repurpose
`Charge`?**
**Add a real read method** (`Balance(ctx, cred) (int64, error)` hitting
`GET /v1/balance`), don't repurpose `Charge`. Three reasons:
- The ledger service already exposes `GET /v1/balance` — a read wrapper is
  a thin, low-risk client addition, not new service-side scope.
- "Charge 1 token to probe access" mutates ledger state on **every**
  quota-exhausted call fleet-wide the moment this ships, turning what is
  today a free 401 into a paid, stateful probe against a service with zero
  production mileage — a materially different (and larger) scope than
  ADR-0033 signed off on.
- `verifyHandler` has to decide allow/deny *before* the request proceeds
  and has no idea what the downstream service's actual per-call cost is
  (the keystore doesn't know per-service pricing) — it structurally cannot
  compute a correct charge amount here. A balance check is the only
  operation that's actually well-defined at this integration point. Real
  metering (`Charge` with a real reason/amount) belongs at the individual
  service per ADR-0033, unchanged by this ADR.

**4. go-common v0.68.0 → v0.76.0+ upgrade risk?**
**Moot.** `go-apikey-service`'s `go.mod` is already pinned to `v0.78.0`
(verified 2026-08-06), which post-dates the ledger package's v0.76.0
introduction. No version bump, no upgrade-risk assessment needed. This
open question was based on stale context.

**5. Config-gated, off by default?**
**Yes, and staged further than a simple flag** — see the phased rollout in
the companion implementation plan. Given TRL 4 and zero production
callers, this should not go straight from "off" to "enforcing" for the
fleet's most trusted endpoint; it goes through a shadow (log-only) mode
with a measured go/no-go gate first.

## Consequences

**Positive**: keys that legitimately want to pay past a limit get a real
path instead of contacting an admin; exercises the ledger's first real
production traffic in a controlled, reversible way; keeps `use_limit`'s
existing semantics intact for every account that never tops up.

**Negative / Mitigations**:
- Adds a synchronous outbound dependency to the fleet's most trusted
  endpoint. **Mitigated** by fail-open + tight timeout (Q2) and by shadow
  mode before enforcement (see plan).
- The ledger is unaudited at `/verify`-scale QPS. **Mitigated** by shadow
  mode measuring real latency/error rates before any request's outcome
  actually depends on it.
- Debiting the account (the actual charge, not just the balance check) is
  a separate synchronous call this ADR does **not** resolve — see
  "Open — not resolved by this ADR" below. Do not implement the debit path
  without a follow-up decision.
- `go-common/ledger`'s stale `LEDGER_SERVICE_URL` default (18208) is a
  live footgun independent of this ADR — anyone wiring in the client today
  without setting the env var explicitly silently points at the wrong
  port. Fixed as part of Phase 1 regardless of enforcing-mode rollout.

## Open — not resolved by this ADR

**How/when does the debit actually happen?** Granting an overflow request
via a balance check doesn't spend the balance. Two shapes, deliberately
left open for a separate decision because it changes the risk profile
again:
- Per-request async debit (fire-and-forget `Charge` after granting, same
  pattern as the existing fire-and-forget `use_count` increment at
  `main.go:301-305`) — risk: request granted, debit later fails silently,
  balance drifts from real usage.
- Periodic reconciliation (batch-charge accumulated overflow usage on a
  timer) — risk: an account can overflow far past its actual balance
  between reconciliation runs.

Resolve as its own follow-up ADR once shadow-mode data exists — not a
blocker for shipping Phase 1/2 (shadow mode grants nothing, so no debit is
needed yet).

## Migration path

Config-gated: `LEDGER_OVERFLOW_ENABLED` (default `false`) plus
`LEDGER_OVERFLOW_SHADOW` (default `true` when enabled) for a log-only
dry run before anything is actually granted. When disabled,
`verifyHandler` behavior is byte-identical to today — zero risk to ship
the code path dark. Fail-open semantics apply whenever the ledger call
doesn't complete successfully within budget (see Q2). No existing key or
account is affected until explicitly enabled and taken out of shadow
mode. See the companion implementation plan for the full phase breakdown
and go/no-go gate.

## Alternatives considered

**A — Do nothing; keep quota-exhausted as a hard, unconditional 401.**
Rejected only in the sense that it's the status quo this ADR proposes
changing — but remains the safe default if shadow-mode data shows the
ledger isn't ready. Worth stating explicitly: shipping this ADR's *design*
does not obligate shipping the *enforcing* mode.

**B — Meter every `/verify` call via the ledger (not just overflow).**
Rejected: this would mean every fleet API call costs tokens, which is a
fleet-wide pricing/business decision far outside this ADR's scope and
ADR-0033's explicit non-goals ("this ADR does not retroactively meter the
existing fleet").

**C — Put the ledger check in nginx/the gateway instead of the keystore.**
Rejected: nginx `auth_request` already delegates all decision logic to
`/verify`; splitting the decision across nginx config and Go code doubles
the places an agent has to look to understand auth behavior, and ADR-0033
already rejected folding billing into the gateway (`go-fleet-mcp-gateway`)
for the same "deliberately scoped, don't give it a second job" reason.

## References

- ADR-0033 — Fleet token ledger (this ADR is its explicitly-flagged
  follow-up, "Alternative B")
- [`0036-ledger-overflow-implementation-plan.md`](0036-ledger-overflow-implementation-plan.md)
  — phased, reversible rollout with a go/no-go gate
- `go-apikey-service/main.go:242-309` — `verifyHandler`
- `go-apikey-service/README.md`, `CLAUDE.md` — "CA root" framing, SQLite
  incident precedent for why blocking calls in `/verify` are dangerous
- `go-common/ledger/ledger.go` — current client (Charge-only, 5s timeout,
  stale `LEDGER_SERVICE_URL` default)
- `go-common/CLAUDE.md` — canonical intra-mesh call pattern (short
  timeout, fail-open, `degraded[]`); `loadshed` package as prior art for
  protecting a hot path from a slow dependency
- `go-fleet-token-ledger/handler.go` — `HandleBalance`/`HandleCharge` both
  authenticate via `header.AuthUser` through the ledger's own
  `server.WithKeystoreAuth` middleware, confirming direct (non-gateway)
  client calls are verified correctly
