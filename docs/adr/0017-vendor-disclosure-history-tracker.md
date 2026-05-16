# ADR-0017 — canonical vendor-disclosure history primitive

- **Status**: accepted
- **Date**: 2026-05-16
- **Repo**: `baditaflorin/go-fleet-vendor-disclosure-tracker`
- **Port**: 18167 (host = container)
- **Mesh**: `mesh-0exec`
- **Category**: `fleet-infra`
- **Tier**: B #15 (FLEET-FUTURE-TOOLS.md)
- **Sister**: `go-pentest-leak-bounty-policy` (batch 2, ADR-bootstrapped)

## Context

`go-pentest-leak-bounty-policy` (TRL-7 as of 2026-05-16) decides
**where** a leaked-credential disclosure should go: `auto_apply`,
`disclose-issue`, `email-security`, or `private-bounty`. It owns the
`policies` table (vendor × secret_class → channel) and writes one row
per /classify call to its `decisions` ledger.

The decisions ledger answers "for THIS finding, what channel did we
pick?" — it does NOT answer the operational questions that come up
once disclosure is in flight:

- "When did we **last** email security@stripe.com? Was it about THIS
  finding or a different one?"
- "Have we ever heard back? When? What did they say?"
- "Which disclosures are past the responsible-disclosure window and
  need escalation?"
- "What's GitHub's median ack-time across all the reports we've ever
  filed?"

Today the answer is "grep the operator's email, hope nothing was
deleted, try to remember if the submit-bot's notes file is current."
That's a documentation gap, not a tooling gap — the **history** of
who-we-contacted-when is a fleet primitive that more than one
consumer needs.

## Decision

Stand up a tiny SQLite-backed history primitive — `go-fleet-vendor-
disclosure-tracker` — with two tables (`disclosures`, `responses`)
and six endpoints. Owns no curated data, no policy table — just the
log of outbound contacts and inbound replies.

**Schema** (intentionally minimal):

| Table         | Columns                                                                                        |
|---------------|------------------------------------------------------------------------------------------------|
| `disclosures` | `id, vendor, finding_id, channel, contact_at, recipient_prefix, severity, status`              |
| `responses`   | `id, disclosure_id, response_kind, ts, notes` — `INDEX (disclosure_id)`                        |

**Endpoints**:

| Method | Path                       | Purpose                                                      |
|--------|----------------------------|--------------------------------------------------------------|
| POST   | `/disclose`                | log one outbound contact                                     |
| POST   | `/response`                | log one inbound reply (or noted silence); flips parent status|
| GET    | `/history?vendor=X`        | every disclosure for vendor X with linked responses + age    |
| GET    | `/open?older_than_days=N`  | `status='pending'` AND age > N days — escalation candidates  |
| GET    | `/vendor-stats/{vendor}`   | p50/p90 first-response-hours + fix-rate                      |
| GET    | `/selftest`                | round-trip disclose → response → history on `:memory:`       |

**Hard PII rule** — the `recipient` field is the ONE place where
operator-supplied email addresses could leak into our data store.
The HTTP boundary runs `RedactRecipient` exactly once: max 8 chars of
the local-part, followed by `…`. The string `security@stripe.com`
lands as `security…` and never as anything longer. `scrubEmails`
applies the same defense to free-text `notes` fields. The selftest
asserts the synthetic email is NOT present in any persisted row, so
the deploy gate enforces the invariant on every roll.

## Consequences

**Migration / consumers** (top 3):

1. **`submit-bot`** — after every disclosure (issue file, email send,
   H1 / Bugcrowd submission) POSTs `/disclose` with the same channel
   it just used. Replaces ad-hoc notes-file pattern.
2. **`go-pentest-leak-bounty-policy`** — after `/classify` resolves
   to anything other than `auto_apply`, POSTs `/disclose` so the
   decision ledger AND the contact history both record the event.
   No data duplication: leak-bounty's `decisions` table answers "what
   did we decide", this tracker answers "what did we do about it".
3. **Dashboard** (`catalog.0exec.com`) — surfaces a "stale
   disclosures" panel from `/open?older_than_days=14` plus a
   per-vendor "responsiveness" panel from `/vendor-stats/{vendor}`.

**Hard rules honored**:

- SQLite with `BEGIN IMMEDIATE` for every write path; 50-goroutine
  `-race` concurrent test (`TestConcurrent_DiscloseAndResponse`)
  proves no interleave corruption.
- `golang:1.24` Dockerfile matches `go.mod`.
- `docker-compose.yml` image pinned to `:0.1.0` (NOT `:latest`).
- Git tag is `0.1.0` (no `v` prefix).
- `gh repo create` is `--private`.
- No GitHub Actions workflow scaffolded (per `feedback_local_build_only`).
- Never `--force` pushed.
- Never modified `services-registry/overrides.json` from this repo's
  session — registry merge is the operator's call.
- No manual `/metrics` handler — `go-common/server` mounts the
  Prometheus surface for us.

**TRL**: 6 at ship, ceiling 7. Ceiling reason: vendor-reply
classification is manual (a human or the submit-bot decides "this
email = ack" or "this email = wontfix"). TRL 8+ would need an IMAP
poller that auto-classifies inbound mail and a durable per-thread
message-id index.

## Alternatives considered

- **Extend `go-pentest-leak-bounty-policy`'s `decisions` ledger** to
  carry `contact_at` and `replied_at`. Rejected: that service is
  already curated-list-shaped (policies are the data); adding a
  history surface bloats its single responsibility and forces every
  consumer of "what's the policy" to also wear the auth scope of
  "who can write history".
- **A shared `findings_store` row per disclosure**. Rejected:
  `findings_store` is per-finding (one row per scanner hit); a
  disclosure is per (finding × contact) — one finding might be
  emailed to two vendors. The natural cardinality lives here.

## File map

| File                          | Role                                                  |
|-------------------------------|-------------------------------------------------------|
| `main.go`                     | wire endpoints + start server                         |
| `clock.go`                    | `Clock` interface + `FakeClock` for deterministic age |
| `store.go`                    | SQLite schema, `BEGIN IMMEDIATE` writers, percentile  |
| `handler.go`                  | HTTP boundary, `RedactRecipient`, `scrubEmails`       |
| `selftest.go`                 | `:memory:` round-trip + redaction assertion           |
| `store_test.go`               | 8 tests, `-race` clean                                |
| `helpers_test.go`             | tiny test scaffolding                                 |
| `service.yaml` / `deploy.yaml`| fleet-runner catalog metadata                         |
| `Dockerfile`                  | `golang:1.24-alpine` builder + `alpine:3.20` runtime  |
| `docker-compose.yml`          | image pinned to `:0.1.0`, named volume for SQLite     |
