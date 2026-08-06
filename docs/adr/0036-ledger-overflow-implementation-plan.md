# Implementation plan — ledger overflow on keystore quota-exhausted path

Companion to [ADR-0036](0036-ledger-overflow-quota-exhausted.md) (Accepted
2026-08-06). Phases are ordered so each one is independently reversible and
low-risk, with a real go/no-go gate between "shadow" and "enforcing."

## Phase 0 — review

Done as part of accepting ADR-0036. One item carries forward as a
pre-enforcing-mode check, not a blocker for Phase 1/2: confirm the
150-300ms timeout budget against the real nginx `auth_request` timeout
config for `apikey-service.0exec.com` (private webgateway config, not
available at design time).

## Phase 1 — `go-common/ledger`: add `Balance`, fix the stale default

Repo: `go-common`. Independent of go-apikey-service; ships and versions on
its own, reviewable in isolation.

- Add `Balance(ctx context.Context, cred Credential) (int64, error)` to
  `ledger.Client`, hitting `GET /v1/balance` (mirrors `Charge`'s
  error/timeout handling: `ErrNoCredential`, `ErrLedgerUnavailable`).
  Confirmed against `go-fleet-token-ledger/handler.go`: the response body
  is the same `response.Success({"account","balance"})` envelope shape
  `Charge` already decodes, so `Balance` reuses the same envelope type.
- Fix `New()`'s `LEDGER_SERVICE_URL` default from `http://localhost:18208`
  to the current registry port (18314). Considered making it required
  (`env.MustString`, fail loudly if unset) instead, matching the fleet's
  `MustResolveCritical` pattern for outbound fleet credentials — rejected
  for this client specifically: `ledger.New()` is a general-purpose
  constructor any service can call, and this whole feature is designed to
  be fail-open (ADR-0036 Q2). A missing env var crashing a caller at
  startup — including, worst case, the keystore itself if wired in
  carelessly — contradicts "the ledger being unavailable should never take
  anything else down" harder than a merely-wrong-but-present default does.
  Runtime connection failures against the fixed default are already
  handled by the existing fail-open `ErrLedgerUnavailable` path.
- Unit tests for `Balance` (200, 401/no-credential, timeout, malformed
  body) alongside the existing `Charge` tests in `ledger_test.go`.
- CHANGELOG entry, version bump, tag, push.
- **Gate**: `go test ./...` green. No other repo needs to consume this yet.

## Phase 2 — `go-apikey-service`: shadow mode only

Repo: `go-apikey-service`. Bump to the new go-common version from Phase 1.
**Checkpoint with the user before starting this phase** — it's the first
change that touches the keystore's own source, even though the change
ships dark (`LEDGER_OVERFLOW_ENABLED=false`) by default.

- New env vars, both default to inert:
  - `LEDGER_OVERFLOW_ENABLED` (default `false`) — master switch.
  - `LEDGER_OVERFLOW_SHADOW` (default `true` when enabled) — if true, run
    the check and log/metric the outcome but still return the normal 401;
    don't actually grant the request.
  - `LEDGER_SERVICE_URL` set explicitly (don't rely on any client default
    per Phase 1).
- In `verifyHandler`, at the existing `use_limit` rejection branch
  (`main.go:272-276`): if `LEDGER_OVERFLOW_ENABLED`, spend a bounded
  `context.WithTimeout` (the Phase-0-confirmed budget) calling
  `ledger.Balance` with the credential from the request's own key (forward
  the caller's own token — same attribution rule the ledger package
  documents for `Charge`, not the keystore's own identity).
- Emit a metric either way: `keystore_ledger_overflow_total{result="would_allow"|"would_deny"|"timeout"|"unavailable"}`.
  This is the data that answers "is the ledger fast/reliable enough" before
  anything real depends on it.
- Still return 401 unconditionally in shadow mode — this phase changes
  nothing about what any caller experiences.
- Deploy with `LEDGER_OVERFLOW_ENABLED=false` (i.e. dark) first; flip to
  `true` + shadow in a low-traffic window and watch the new metric for a
  meaningful period (days, not minutes) before Phase 3.

## Phase 3 — go/no-go gate

Before enforcing anything, check the Phase 2 metrics against an explicit
bar, e.g.:
- p99 ledger round-trip well under the chosen timeout budget (so timeouts
  are rare, not routine).
- Error/unavailable rate low enough that fail-open isn't silently eating
  most requests.
- No `/verify` p99 latency regression traceable to the shadow check itself.

If the bar isn't met: stop here, revert to Phase 2 dark, revisit the
ledger service's reliability before retrying — do not proceed to Phase 4.

## Phase 4 — enforcing mode (only after Phase 3 passes)

- `LEDGER_OVERFLOW_SHADOW=false`: on `would_allow`, actually return 200
  instead of 401. Recommend also setting a response header (e.g.
  `X-Auth-Overflow: ledger`) so downstream services/observability can
  distinguish a paid-overflow allow from a normal allow — useful for
  billing reconciliation and for debugging "why did this over-limit key's
  call succeed."
- Debit timing (the open question from the ADR) must be decided and
  implemented as its own reviewed change before this phase ships — Phase 4
  should not grant free overflow indefinitely.
- Roll out to a single low-risk internal test account/key first, not
  fleet-wide, and watch the same metrics before removing that scoping.

## Explicitly out of scope for this whole plan

- Per-service metering via `Charge` (ADR-0033's existing opt-in mechanism)
  — unaffected, unchanged by any of this.
- Any change to `use_limit`'s meaning or existing key behavior when
  overflow is disabled or the ledger is unavailable.
- MCP gateway billing (ADR-0033's "Alternative B" gateway option) — not
  addressed here either.
