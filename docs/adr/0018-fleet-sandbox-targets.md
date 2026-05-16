# ADR-0018 — Canonical internal-only sandbox of vulnerable targets

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-session-tierB-16
* **Tags**: scanner-ci, sandbox, security, fleet-infra

## Context

End-to-end tests of fleet scanners (`go_apikey_scanner`,
`go-pentest-takeover-checker`, `go-pentest-subfinder`, the headers
auditor in `fleet-runner audit headers`, etc.) need vulnerable
targets. Today every scanner has good unit-test coverage against
`httptest.NewServer` stubs, but **no fleet-wide integration sandbox
exists** — there is no canonical "actually try it on a deliberately
vulnerable app" gate that runs before a scanner is bumped or
deployed. Concrete incidents this enables:

- 2026-04: `go_apikey_scanner` v1.6.171 tightened the FP filter on
  AWS-shape detection and stopped flagging *real* AWS keys embedded
  in customer JS bundles. Caught in production, not in CI.
- 2026-05-08: a refactor in `fleet-runner audit headers` silently
  stopped reporting `missing-csp-frame-ancestors` on responses that
  *did* set `X-Frame-Options`. Both controls had been treated as
  equivalent in the new code; only one engagement-derived screenshot
  caught it.

Without a sandbox we discover regressions in production via missed
findings, which is the worst-possible feedback loop for a scanner
fleet.

## Decision

Ship `go-fleet-sandbox-targets`: ONE Go binary serving ten
deliberately-vulnerable mini-apps under `/vuln/*` sub-paths, plus a
machine-readable `/catalog` of `{path, class, vulnerable,
expected_findings[]}` so scanner CI can discover the surface without
per-scanner hardcoding, plus a `/selftest` gate that runs each
canonical probe and returns 200/503 (fleet-runner deploy refuses to
roll on 503).

**The service is marked `scope: internal-only` in `service.yaml` and
the nginx vhost** — by construction this is a collection of
deliberately-broken endpoints; it has no business being reachable
from the public Internet. Internal-IP allowlist or admin-token gate
at the gateway.

API shape (catalog excerpt):

```json
{
  "service": "go-fleet-sandbox-targets",
  "scope":   "internal-only",
  "count":   10,
  "endpoints": [
    {
      "path":              "/vuln/xss-reflect/",
      "class":             "xss",
      "vulnerable":        true,
      "probe":             "/vuln/xss-reflect/?q=<script>alert(1)</script>",
      "expected_findings": ["reflected-xss", "html-injection"]
    },
    ...
  ]
}
```

Initial coverage (Tier B #16 — minimal ship): 10 endpoints across
XSS (with TN control), SQLi-shape, open-redirect, SSRF-shape (with
hard allowlist — see below), clickjacking-headers (with TN control),
CORS misconfiguration, 401-username-leak, and secrets-in-JS.

## Consequences

**Positive**

- Scanner CI can assert "given the canonical probe for class X, I
  produce finding Y" against a stable, version-controlled target. No
  more "we tightened the FP filter and broke real detection".
- `/catalog` is the single source of truth — scanners discover what's
  vulnerable without per-scanner duplication. Adding a new class is
  one PR touching one repo.
- Negative controls (`/vuln/xss-escaped/`, `/vuln/protected/`) give
  scanners a way to assert their own FP rate, not just TP rate.
- The `/selftest` 503 gate prevents the sandbox itself from rotting
  silently — if the XSS endpoint accidentally starts escaping its
  input, fleet-runner refuses to deploy.

**Negative**

- Maintenance overhead: every new scanner class needs a target
  added here. We chose the 10 canonical OWASP shapes to minimize
  this, but additions are likely (file upload, XXE, SSTI, etc.).
- The service is a deliberate footgun if accidentally exposed. The
  `internal-only` scope marking is the mitigation, but it's not a
  technical guarantee — see Mitigations.

**Mitigations**

- **SSRF allowlist is total and prefix-exact.** Only
  `http://127.0.0.1:18168/sink` (and `localhost` variant) is
  reachable. File://, gopher://, IPv6 loopback, link-local IPs,
  trailing-querystring bypasses all return 400. Asserted by
  `TestVuln_SSRF_RejectsDNSRebindShape`. The "vulnerability" surfaced
  is the shape of the endpoint (server-side fetch reachable via
  `?url=`), not real egress.
- **NO real SQL.** SQLi target is a stringly-typed shim — no CGO, no
  on-disk database, no destructive query path. `/reset` rebuilds the
  in-memory map.
- **NO real secrets.** Leak fixtures use the `TESTSYNTH-*` prefix —
  matches the shape regex, un-actionable as a credential. Same
  convention `go-fleet-body-redactor` uses for its selftest corpus.
- **Scope marker propagation.** `service.yaml` has `scope:
  internal-only`; the nginx render path must honor it (vhost
  templates in `0crawl-platform` should refuse to render a
  public-facing vhost for `scope: internal-only` services — separate
  follow-up).

## Migration path

**Scanner CI** adopts this via a `--integration` flag (already used
by some scanners for outbound-fetch tests). Typical CI shape:

```go
const sandboxBase = "https://fleet-sandbox-targets.internal" // resolved via internal DNS only

func TestIntegration_AKIA_DetectsInJSBundle(t *testing.T) {
    if !*integration { t.Skip() }
    findings, err := scanner.Scan(sandboxBase + "/api/secrets-leak.js")
    assert.NoError(t, err)
    assertFindingKind(t, findings, "aws-access-key-id")
}
```

For environments without internal-mesh access, scanners can
`docker run --rm ghcr.io/baditaflorin/go-fleet-sandbox-targets:0.1.0`
and point at `http://127.0.0.1:18168` — the service is hermetic
(no outbound dependencies, no keystore call from inside).

Per-scanner integration: ~1 hour to wire the flag, ~30 min per
class to assert the expected finding emerges. Cumulative payoff
across the fleet is far higher than that per-scanner cost.

## Alternatives considered

- **Juice Shop / WebGoat ensemble.** Rejected as the initial ship:
  each is a multi-container deployment with its own auth surface
  and rotating CVE lists. Hard to keep deterministic for scanner
  asserts ("we caught a different finding this run") and a much
  larger attack surface for the accidental-public-exposure case.
  Can be added later as a second sandbox service if needed —
  `go-fleet-sandbox-juiceshop` — orthogonal to this ADR.
- **httptest stubs only.** What we have today. Doesn't catch
  scanner regressions because each scanner owns its own stubs;
  drift between "what the stub returns" and "what real targets
  return" is unobservable.
- **Multi-container fleet (one container per class).** Considered.
  Rejected on operational cost: 10 containers to deploy/monitor for
  the same coverage one binary provides, plus the sandbox surface
  expands every time we add a class. The single-binary shape lets
  `/selftest` exercise the whole surface in one in-process roundtrip.
- **Real sqlite (CGO).** Considered for the SQLi target. Rejected to
  preserve the CGO-free, no-on-disk-state property. The shim
  produces the parser-shape error scanners look for, which is what
  they'd extract from real sqlite anyway.

## References

- Related: ADR-0004 (canonical sensitive-data redaction primitive —
  same TESTSYNTH-* fixture convention).
- Related: FLEET-FUTURE-TOOLS.md Tier B #16 (this was the
  prioritization slot).
- Repos: `go_apikey_scanner` (consumer), `go-pentest-takeover-checker`
  (consumer), `fleet-runner audit headers` (consumer).
- Incidents: 2026-04 apikey-scanner FP-filter regression;
  2026-05-08 fleet-runner headers-audit regression.
