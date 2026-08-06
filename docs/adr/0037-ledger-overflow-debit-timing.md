# ADR-0037 — Ledger overflow debit timing: async fire-and-forget

* **Status**: Accepted
* **Date**: 2026-08-06
* **Authors**: claude-sonnet-5 (with florin)
* **Tags**: billing, auth, keystore, ledger

## Context

ADR-0036 (ledger-backed overflow on the keystore's quota-exhausted path)
deliberately shipped Phase 2/3 as **observation only**: a balance check that
never grants anything, specifically because granting a request without
ever debiting the account would let a topped-up key bypass `use_limit` for
free, indefinitely. That ADR's "Open — not resolved by this ADR" section
left the debit-timing choice for a separate, explicitly reviewed decision
before Phase 4 (actual enforcement) could ship. This is that decision.

Two shapes were on the table:

- **Per-request async debit** — fire-and-forget `ledger.Charge` right
  after granting, bounded by a short timeout, not blocking the response.
- **Periodic reconciliation** — batch-charge accumulated overflow usage
  on a timer.

## Decision

**Async fire-and-forget debit**, charged immediately after granting a
200, in a goroutine bounded by a short `context.WithTimeout` — the same
established pattern this exact file already uses for the `use_count`
increment (`main.go`'s fire-and-forget `UPDATE ... use_count = use_count
+ 1` goroutine, bounded at 500ms). Charge amount is configurable
(`LEDGER_OVERFLOW_CHARGE_AMOUNT`, default `1`) rather than hardcoded — the
keystore has no way to know a "correct" price for bypassing an arbitrary
downstream service's `use_limit` (this is the same reasoning ADR-0036
already used to reject repurposing `Charge` as a probe), so the amount is
an operator-tunable knob, not a value this design can derive.

A debit that fails (ledger down, a race where the balance changed between
the check and the charge, network hiccup) is logged and counted in a
metric — **it never retracts the already-served request.** An HTTP
response can't be unsent, and retrying synchronously inside `/verify`
before responding would reintroduce exactly the blocking-hot-path risk
ADR-0036 Q2 designed the whole feature to avoid.

### Why not periodic reconciliation

Reconciliation needs a new stateful batching subsystem in the CA-root
service: tracking un-reconciled charges, persisting them across restarts,
a scheduler, retry/backoff logic — meaningfully more code and more new
failure modes in the one service every other fleet service unconditionally
trusts, for a feature whose entire design goal has been to stay small and
inspectable (see ADR-0036's Q2 rationale). Its worst case is also strictly
worse: an account can overflow arbitrarily far past its real balance
during the reconciliation window, whereas async-immediate bounds the
drift to "one grant's worth, per individual race window." Given this path
only fires on already-quota-exceeded requests (low volume by construction)
against a TRL-4 ledger, async fire-and-forget's exposure is small in
absolute terms and directly observable via the new metric below —
reconciliation's added complexity isn't buying a proportionate safety
improvement.

## Consequences

**Positive**: no new stateful subsystem in the keystore; reuses an
already-proven pattern in this exact file; failure mode is bounded and
visible.

**Negative / Mitigations**:
- Rare unbilled overflow grants are possible when the async debit fails.
  **Mitigated**: this is the same class of risk the keystore already
  accepts for `use_count` (best-effort, not transactionally tied to the
  response) — not a new risk category for this service, just applied to
  a second field.
- New metric `keystore_ledger_overflow_debit_total{outcome="charged"|"failed"}`
  makes the failure rate directly visible. **If this rate is ever
  non-negligible, that is itself signal the ledger isn't ready for
  enforcement** — the operational response is to disable
  `LEDGER_OVERFLOW_ENFORCE` again, not to add retry complexity.

## Migration path

Implemented in `go-apikey-service`'s Phase 4 code behind
`LEDGER_OVERFLOW_ENFORCE` (default `false`). **This ADR resolves the
debit-timing design question — it does not by itself authorize enabling
enforcement in production.** That remains gated on ADR-0036's Phase 3
go/no-go data (real `/verify`-scale latency/reliability evidence), a
separate operational decision.

## Alternatives considered

**A — Synchronous debit before responding.** Rejected: doubles the
hot-path's dependency on the ledger (a second round trip, after the
balance check that already just ran) for no real benefit — the balance
was checked moments earlier in the same request.

**B — Periodic reconciliation.** Rejected above.

**C — No debit at all; overflow is simply free.** Rejected: defeats the
purpose of gating on a ledger balance at all — `use_limit` would become
meaningless forever for any account that ever tops up once.

## References

- [ADR-0036 — Ledger-backed overflow on the keystore's quota-exhausted path](0036-ledger-overflow-quota-exhausted.md)
- `go-apikey-service/main.go` — existing fire-and-forget `use_count`
  increment pattern (bounded 500ms `context.WithTimeout`)
- `go-common/ledger.Charge` — the debit call this decision wires in
