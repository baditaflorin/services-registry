# ADR-0024 — Phase 3 consumer migration manifest + primitive-to-primitive plain-http convention

* **Status**: Accepted
* **Date**: 2026-05-17
* **Authors**: claude-session-batch5-2026-05-17
* **Tags**: phase-3, consumer-migration, safehttp, ssrf, payload-corpus

## Context

The 20 new fleet primitives shipped in batches 1-4 (ADRs 0003-0022) were live on the mesh after Phase 1 of this batch (see ADR-0023 for the deploy-pipeline learnings). go-common v0.16/0.17/0.18 landed Phase 2 (safehttp auto-trace/backoff/degraded + selftest helper + policyeval mini-DSL), and `fleet-runner update-dep github.com/baditaflorin/go-common@v0.18.0` bumped 191/191 Go fleet repos.

Phase 3 was the consumer-side adoption — wiring per-primitive env vars into consumer compose files, replacing local re-implementations of the primitives, and surfacing `degraded[]` on fail-open. 19 consumer agents ran in two parallel waves (8 + 11), one per consumer (NOT one per primitive — heavy consumer-overlap on shared repos like `walkthrough` made per-primitive dispatch race-prone).

The migrations were uniformly clean (every agent green on `go test ./... -race` + smoke startup, no panics, no force-pushes), but two non-obvious patterns surfaced that should be cemented into convention before the next batch.

## Decision

### Manifest — 19 consumer migrations landed 2026-05-17

| Consumer                            | Tag    | Primitives adopted (env vars wired)                                                                                       | Local fallback kept?               |
|-------------------------------------|--------|---------------------------------------------------------------------------------------------------------------------------|------------------------------------|
| `go_admin_finder`                   | 1.7.1  | FINGERPRINT_CACHE_URL + safehttp v0.18.0                                                                                  | yes (`DetectWAF`)                  |
| `go_crawl_web_application`          | 1.2.1  | FINGERPRINT_CACHE_URL + safehttp v0.18.0                                                                                  | yes (soft-404 + WAF heuristics)    |
| `go-pentest-nuclei`                 | 0.2.1  | FINGERPRINT_CACHE_URL + TECH_INFERRER_URL + safehttp v0.18.0                                                              | yes (full template set)            |
| `go_broken_links`                   | 1.5.1  | FINGERPRINT_CACHE_URL + safehttp v0.18.0                                                                                  | yes (`Soft404Detector`)            |
| `go_cors_tester`                    | 1.5.1  | FINGERPRINT_CACHE_URL + safehttp v0.18.0 (env-wiring only — no local heuristic)                                           | n/a                                |
| `go-pentest-findings-store`         | 0.3.0  | BODY_REDACTOR_URL + safehttp v0.18.0                                                                                      | **yes** (2026-05-08 regression test added) |
| `go-pentest-walkthrough`            | 0.3.0  | BODY_REDACTOR_URL + HAR_BUILDER_URL + POC_CURL_URL + ENGAGEMENT_TIMELINE_URL + safehttp v0.18.0                           | yes (security floor on redactor)   |
| `go-pentest-http-replay`            | 0.3.0  | BODY_REDACTOR_URL + HAR_BUILDER_URL + DIFF_ENGINE_URL + safehttp v0.18.0                                                  | yes (header redactor)              |
| `go-pentest-takeover-checker`       | 0.4.1  | RESOLVER_QUORUM_URL + safehttp v0.18.0                                                                                    | yes (`internal/resolve/quorum.go`) |
| `go-fleet-dns-sync`                 | 0.3.0  | RESOLVER_QUORUM_URL + safehttp v0.18.0 (real gate — demotes Missing→Correct on quorum agreement)                          | yes (single-resolver lookup)       |
| `go_zone_transfer`                  | 1.4.0  | RESOLVER_QUORUM_URL + safehttp v0.18.0                                                                                    | yes (`net.DefaultResolver`)        |
| `go_asn_lookup`                     | 1.5.1  | RESOLVER_QUORUM_URL + safehttp v0.18.0                                                                                    | yes (`net.DefaultResolver`)        |
| `go_xss_scanner`                    | 1.5.1  | PAYLOAD_CORPUS_URL + safehttp v0.18.0                                                                                     | yes (`bundledPayloads`, 10 strings) |
| `go-pentest-cors-misconfig-prober`  | 0.7.1  | PAYLOAD_CORPUS_URL + safehttp v0.18.0 (corpus producer missing `cors-misconfig` class — see Gap A)                        | yes (built-in test matrix)         |
| `go_crlf_tester`                    | 1.5.1  | PAYLOAD_CORPUS_URL + safehttp v0.18.0                                                                                     | yes (20-variant `builtinPayloads`) |
| `go-pentest-ssrf-prober`            | 0.3.1  | PAYLOAD_CORPUS_URL + safehttp v0.18.0 (uses `/payloads/ssrf` path-route, see Gap B)                                       | yes (vendored `BuildPayloads`)     |
| `go-pentest-authz-matrix`           | 0.2.1  | PAYLOAD_CORPUS_URL + safehttp v0.18.0 (corpus producer missing `authz` class — see Gap A)                                 | yes (`expand.go` matrix)           |
| `go-pentest-submit-bot`             | 0.3.0  | HAR_BUILDER_URL + POC_CURL_URL + ENGAGEMENT_TIMELINE_URL + WEBHOOK_VERIFIER_URL + safehttp v0.18.0 (webhook fail-CLOSED)  | yes (local curl emitter)           |
| `go-pentest-continuous-monitor`     | 0.3.0  | BUDGET_TRACKER_URL (fail-CLOSED) + DIFF_ENGINE_URL (fail-OPEN) + safehttp v0.18.0                                          | yes (local diff; budget = skip-on-fail) |

Every consumer kept a local fallback path. The default `degraded[]` contract: append `"<primitive>-down"` to the per-request slice on env-unset / timeout / 5xx, surface in the response JSON envelope. **One exception**: `go-pentest-continuous-monitor` uses **fail-CLOSED** on `BUDGET_TRACKER_URL` (per the Phase 3 prompt — the gate exists specifically to prevent self-DoS of egress quota; on tracker outage, skip the scan with `budget_skipped=true, budget_skip_reason="tracker-error: …"` and degraded-tag rather than proceed).

### Convention 1 — primitive-to-primitive calls use plain `http.Client`, NOT safehttp

**Pattern**: every consumer agent independently discovered this. `safehttp` is built around an SSRF guard that rejects private IPs, link-local, loopback, and DNS-rebind shapes. Fleet primitives live on a private docker network (`http://go-fleet-<slug>:<port>`), so a `safehttp.NewClient` call to fetch a sibling primitive returns `safehttp.ErrBlocked` immediately.

Resolution baked into every Wave 1 + Wave 2 migration: outbound (public-internet, user-target) calls go through `safehttp.NewClient` with the new v0.18.0 options; intra-mesh primitive calls go through a per-service plain `http.Client` with a short timeout (typically 1-3 seconds). Examples:
- `go-pentest-takeover-checker/internal/resolve/remote.go` — plain `net/http` to resolver-quorum, comment explains why
- `go-pentest-findings-store/redactor.go` — plain `http.Client` to body-redactor
- `go-pentest-cors-misconfig-prober/corpus.go` — plain `http.Client` to payload-corpus

**Codify this in CLAUDE.md** §"`go-common` packages" so the next agent doesn't re-derive. Suggested edit:

> **safehttp is for OUTBOUND (public internet) HTTP only.** Calls to sibling fleet services on the private docker mesh (`http://go-fleet-*:<port>`) MUST use a plain `net/http.Client` — safehttp's SSRF guard correctly rejects private IPs. Use a short timeout (1-3s) + fail-open semantics + `degraded[]` flag. Don't wrap primitive calls in safehttp.

### Convention 2 — fail-open with `degraded[]` is the default; fail-closed needs a documented reason

Every primitive adoption above either:
- **Fail-OPEN**: on primitive timeout/5xx/env-unset, fall back to local logic, append `"<primitive>-down"` to a per-request `degraded []string` slice, surface in response JSON.
- **Fail-CLOSED**: on primitive timeout/5xx, refuse to proceed. **Only continuous-monitor's BUDGET_TRACKER_URL gate falls in this bucket** — documented in the commit message + a regression test (`TestRunOnce_BudgetGateFailClosed_TrackerDown`).

A third class is the **body-redactor on findings-store** — fail-open at the network layer, but **never bypasses redaction**: local fallback redactor runs and `degraded[]` flags that the canonical primitive was unavailable (regression test `TestRedactor_FallbackWhenEnvUnset` pins the 2026-05-08 incident scenario).

Future primitive adoptions should default to fail-open + degraded[] unless there's a specific reason (DoS prevention, security gate) to fail-closed — and that reason should land in the commit message AND a test.

## Consequences

**Positive**:
- 19 consumer services now compose with the 20 new primitives via env-var wiring (with sane defaults).
- Auto-trace + auto-backoff + degraded-sink flows through safehttp v0.18.0 for every outbound call across the fleet.
- The plain-http convention for primitive-to-primitive calls is captured; future agents won't burn cycles rediscovering it.

**Negative**:
- Two payload-corpus classes (`cors-misconfig`, `authz`) are missing from the producer (Gap A below). The two consumers that need them are bumped + wired, but currently run on local-fallback only.
- The fleet-runner `push` post-update-dep race condition (every Phase 3 agent had to rebase against the Phase 2 dep bump) cost ~30s per agent and is the largest paper-cut in this batch.

**Mitigations**:
- Gap A: file a follow-up issue on go-fleet-payload-corpus to add the two missing classes. Until then, the env-wiring + degraded[] flag means the consumers will pick up canonical payloads silently the moment the producer ships.
- Race condition: future bulk-bump operations should ensure the dep bump COMPLETES (PUSH + propagation, ~30s) before dispatching consumer agents that will pull fresh.

## Gaps to file as follow-up issues

### Gap A — `go-fleet-payload-corpus` missing two classes

`ValidClasses` in `go-fleet-payload-corpus/corpus.go` currently lists: `{xss, sqli, ssrf, crlf, oob, csrf, jwt-confusion, path-traversal, command-injection, idor, sst-injection, xxe}`. Missing: `cors-misconfig` (needed by `go-pentest-cors-misconfig-prober`), `authz` (needed by `go-pentest-authz-matrix`). Adding them is an additive commit on the corpus repo + version bump + deploy.

### Gap B — `go-fleet-payload-corpus` URL shape is path-based, not query-based

ADR-0006 + the Phase 3 prompt described the corpus API as `GET /payloads?class=xss`. The actual handler (per `go-fleet-payload-corpus/handler.go`) is path-based: `GET /payloads/<class>`. Every Phase 3 agent that read the actual handler corrected to the path shape; the ones that only read the ADR got it wrong on first attempt. Either fix the ADR or add a query-shape alias.

## Migration path

Future per-primitive adoption (when a new primitive ships):
1. Producer ships the primitive at TRL 6+, registered in services.json, ADR written.
2. CLAUDE.md and SERVICE-TEMPLATE.md gain a one-liner pointing at the new env var + canonical URL.
3. Consumer agents follow the per-service prompt template from this batch (env-vars + safehttp v0.18.0 wiring + local-fallback + `degraded[]` surface).
4. Default fail mode: fail-open + degraded; document any deviation in the commit message + a regression test.

## Alternatives considered

- **One agent per primitive (Phase 3 prompt's literal dispatch)**: rejected during planning because shared consumers (walkthrough touched by body-redactor + har-builder + poc-curl + engagement-timeline agents) would race on git push. The per-consumer model gave each repo a single coherent commit per consumer; the trade-off was that the high-leverage primitives (fingerprint-cache, payload-corpus) each spanned multiple consumer agents.
- **Wrap primitive-to-primitive calls in safehttp with an "internal-mesh exception"**: rejected because the SSRF guard is the cardinal safety net for safehttp's intended use case (user-controlled URL). Adding an internal-mesh bypass risks losing the guard for the wrong path.
- **Fail-closed by default**: rejected because the fleet's philosophy is graceful degradation — a sibling primitive going down should NOT cascade to the whole fleet refusing to scan. The continuous-monitor budget-tracker exception is the carve-out: that specific gate exists to PREVENT self-DoS, so cascading is preferable to bypassing.

## References

- ADR-0002 — twenty fleet primitives (the batch this Phase 3 adopted)
- ADR-0003 through ADR-0022 — per-primitive ADRs (consumer lists)
- ADR-0023 — Phase 1 deploy-pipeline gaps from the same session
- Per-consumer commits (search GitHub by tag): `1.7.1` (admin-finder), `0.3.0` (5 services), `0.4.1` (takeover-checker), etc.
