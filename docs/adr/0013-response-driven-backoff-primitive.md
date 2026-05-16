# ADR-0013 — Response-driven backoff is a fleet primitive, not a scanner concern

- Status: accepted
- Date: 2026-05-16
- Deciders: fleet (claude-session-2026-05-16)
- Sister: ADR coordinated with `go-pentest-rate-coordinator` (rate prevention)

## Context

`go-pentest-rate-coordinator` is the canonical primitive for **rate
prevention** — every scanner asks for a token before firing, so the
fleet collectively respects a per-host RPS/burst budget. That covers
the proactive half of "don't get banned."

It does **not** cover the reactive half: what to do when a target
*tells us* we've crossed a line (a `429` with `Retry-After`), or when
the target is having a bad day (`5xx` spikes). Today, every active
scanner re-implements response-driven backoff locally. We observed
three concrete failure modes:

1. **Too-aggressive scanners.** `go_xss_scanner` and
   `go-pentest-subfinder` retry 5xx with a flat 1s sleep. Against a
   target with an actually-broken upstream, that's effectively a
   constant load — we keep the upstream pinned at the failure rate and
   risk a real abuse complaint.
2. **Too-shy scanners.** `go-pentest-takeover-checker` gives up after
   one 429, regardless of `Retry-After`. We've measured ~12% of
   findings missed because the target was momentarily throttled, not
   permanently rate-limited.
3. **Inconsistent attribution.** When a target returns 5xx across
   multiple scanners running in parallel, each scanner independently
   backs off at its own rhythm. There's no "the fleet has decided
   this host is hot, everyone hold off" signal.

The rate-coordinator's existence proved the fleet-primitive pattern:
one in-process service, called via HTTP, owns the cross-scanner state.
Response-driven backoff fits the same pattern.

## Decision

Introduce `go-fleet-backoff-coordinator` (port 18163, mesh-0exec,
category `fleet-infra`) as the canonical response-driven backoff
primitive.

Shape:

- `POST /backoff {host, last_response:{status, retry_after_header, ts}}`
  → `{wait_ms, next_attempt_ok_at, classification, streak}`.
- `GET /status?host=...` → per-host state snapshot.
- `/health`, `/version`, `/metrics`, `/selftest` per fleet contract.

Per-host state (`{streak, last_status, opened_at, next_ok,
classification}`) is kept in-process. 15-min idle window resets the
streak; circuit-open survives idle.

Decision table:

- `2xx` → reset streak, no wait.
- `429` with `Retry-After: N` → wait exactly N seconds, no streak bump
  (rate-limit is signal, not a server fault).
- `5xx` → streak++, wait = `min(2^streak * 100ms, 60s) ± 10% jitter`.
- 5 consecutive `5xx` → open the circuit for 5 minutes. All calls
  during the cooldown return `circuit-open` without state mutation.
- 3xx / 4xx (not 429) → no wait, no state change.

Coordination with rate-coordinator (`RATE_COORDINATOR_URL` env, when
set): callers gate token requests on `/status` → `in_cooldown`. The
reverse direction (backoff-coord pushing into rate-coord) is
intentionally *not* implemented — it keeps both services
single-responsibility and avoids a circular dependency.

## Migration

Same pattern as rate-coordinator:

1. Each scanner adds `BACKOFF_COORDINATOR_URL` env. When set, after
   every upstream response, POST `/backoff` and honor `wait_ms`.
2. When unset, scanner falls back to its existing local backoff
   (degraded mode: no cross-scanner state, but no regression).
3. The fail-open default lets us land the dep without coordinating a
   simultaneous flip across ~20+ scanners.

Migration targets in priority order (top 3 consumers):

1. `go_xss_scanner` — highest 5xx volume; currently uses flat 1s sleep.
2. `go-pentest-takeover-checker` — currently gives up on first 429.
3. `go-pentest-subfinder` — flat retry; benefits from circuit-breaker
   when a passive DNS source goes down.

## Consequences

- One canonical retry-with-backoff implementation. Bug fixes land in
  one place. Tuning (jitter percent, circuit threshold) is one
  service-yaml edit, not 20 PRs.
- In-process state means a single coordinator instance — same
  constraint as rate-coordinator. Multi-instance needs Redis
  (`sethvargo/go-limiter` already supports it; same swap pattern).
- Each upstream response now involves one extra HTTP call to the
  backoff-coordinator. Co-located on the dockerhost; sub-millisecond.
- Failure mode: if backoff-coord is down, scanners fall back to local
  backoff (degraded), not to no-backoff (broken). The
  `BACKOFF_COORDINATOR_URL`-unset path is the same as the
  upstream-down path; both are graceful.

## Alternatives considered

- **Extend rate-coordinator** to also track response state. Rejected:
  conflates rate prevention (prospective) with response reaction
  (retrospective). Two different state machines that happen to share
  a host key. Single-responsibility wins.
- **Library, not service.** A `go-common/backoff` package would
  remove the HTTP hop. Rejected: would silo per-process state — three
  scanners hitting the same struggling host wouldn't share the
  circuit state, defeating the "fleet decides" property.
- **Use stdlib `golang.org/x/time/rate` for backoff too.** Rejected:
  rate.Limiter is a token bucket, not an exponential backoff with
  circuit-breaker. Wrong primitive.
