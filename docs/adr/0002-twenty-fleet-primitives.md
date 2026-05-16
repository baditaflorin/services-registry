# ADR-0002 — Build 20 fleet-primitive services as a single batch

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: baditaflorin + claude-opus-4-7
* **Tags**: process, fleet-architecture, primitives

## Context

`FLEET-FUTURE-TOOLS.md` (in the workspace root) enumerates 20 services that emerged repeatedly as "we keep half-implementing this in each scanner" during the TRL-bump batches 1-3 (35 services, 2026-05-16).

The most-duplicated logic across the existing fleet:
- WAF / soft-404 / CDN-noise classification (`admin-finder`, `broken-links`, `crawl-web-application`, `nuclei`, `cors-tester`)
- Sensitive-header redaction (`exploit-verifier`, `walkthrough`, `http-replay`, `findings-store`)
- Multi-resolver DNS quorum (`takeover-checker`, `dns-sync`, `zone-transfer`, `asn-lookup`)
- Adversarial-payload corpora (`xss-scanner`, `cors-misconfig-prober`, `crlf-tester`, `smuggling-probe`, `ssrf-prober`)

Each duplication is a place where:
1. We pay implementation cost N times.
2. We diverge subtly — different services classify the same input differently.
3. When a new bypass / signature appears, we have to update N repos.

## Decision

Build all 20 primitives from `FLEET-FUTURE-TOOLS.md` as **new fleet services** in a single coordinated batch (mesh-0exec, kind=container, language=go, runtime=compose). Each gets:

- Its own private GitHub repo `baditaflorin/go-fleet-<slug>`.
- A pre-allocated host_port (18153-18172, allocated 2026-05-16).
- A registration row in `services-registry/overrides.json` (`mesh-0exec`, category `fleet-infra` or `fleet-evidence` depending on role).
- A per-service ADR (ADR-0003 through ADR-0022) documenting the API surface and the deduplication-rationale it addresses.
- A `/selftest` endpoint, `BEGIN IMMEDIATE` on `*sql.Conn` for any SQLite writes (see job-queue 0.2.1), `/metrics` middleware short-circuit (NEVER `srv.Mux.Handle("/metrics")` — see rate-coordinator 0.2.1 regression).

The 20 services are built **before** any rollout to consumers. Consumer migration is a separate, monitored phase (Tier 1 of "next steps", out of scope for this ADR).

## Consequences

**Positive:**
- Single coordinated landing eliminates the "wait for primitive N+1 before tackling consumer Y" deadlock.
- Each primitive becomes the authoritative source for its concern — N-way divergence stops.
- Per-service ADRs make the API contracts explicit; consumer agents don't have to read the source to know what to call.

**Negative:**
- Operational footprint grows by 20 services (~7%). Some are stateful (SQLite) — adds backup surface.
- Until consumers migrate, the primitives are dead weight. We need to push adoption in a follow-up batch.
- 20 services × first-time bootstrap (DNS + cert + compose + image) = real deploy-pipeline load. May surface fresh fleet-runner / gateway bugs (we've seen NAT-loopback false-smoke; expect more).

**Mitigations:**
- The "consumer migration" follow-up is named: each new primitive has an `INTEGRATIONS.md` listing what existing services would adopt it on next-touch.
- Per-primitive ADR includes a "Migration path" section so future agents know how to adopt without re-deriving.
- We accept that some primitives may not get consumers right away — sandbox-targets (#16) is L-effort and may stay empty until needed.

## Naming convention

All 20 use the `go-fleet-<slug>` prefix to mark them as fleet-infra (parallels existing `go-fleet-graph`, `go-fleet-secrets`, `go-fleet-preflight`, `go-fleet-dns-sync`, `go-fleet-visualizer`).

This is distinct from `go-pentest-<slug>` (offensive-detection services) and `go-<slug>` / `go_<slug>` (general utilities + recon).

## Port allocation

Pre-allocated via `fleet-runner allocate-port --count 20` on 2026-05-16 to avoid the 20-agent port-allocation race.

| Service | Port |
|---|---|
| fleet-fingerprint-cache | 18153 |
| fleet-body-redactor | 18154 |
| fleet-resolver-quorum | 18155 |
| fleet-payload-corpus | 18156 |
| fleet-har-builder | 18157 |
| fleet-poc-curl | 18158 |
| fleet-tech-inferrer | 18159 |
| fleet-diff-engine | 18160 |
| fleet-call-tracer | 18161 |
| fleet-engagement-timeline | 18162 |
| fleet-backoff-coordinator | 18163 |
| fleet-budget-tracker | 18164 |
| fleet-selftest-aggregator | 18165 |
| fleet-schema-validator | 18166 |
| fleet-vendor-disclosure-tracker | 18167 |
| fleet-sandbox-targets | 18168 |
| fleet-priority-queue | 18169 |
| fleet-webhook-verifier | 18170 |
| fleet-target-reputation | 18171 |
| fleet-content-normalizer | 18172 |

## References

- `FLEET-FUTURE-TOOLS.md` (workspace root) — the full 20 with effort estimates + strategic rationale per item.
- ADR-0001 — ADR process.
- ADR-0003 through ADR-0022 — per-primitive decisions (written by the agent that builds each).
