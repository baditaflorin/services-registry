# ADR-0010 — Canonical structured-diff primitive

- Status: Accepted
- Date: 2026-05-16
- Service: `go-fleet-diff-engine` (port 18160, mesh-0exec, fleet-infra)
- Tier: A (FLEET-FUTURE-TOOLS.md #8)

## Context

Three existing fleet services already do response-diff work, badly:

1. **`go-pentest-exploit-verifier`** decides patched/vulnerable by
   comparing the prior PoC response to the current one with a
   substring match (`strings.Contains(old, marker) !=
   strings.Contains(new, marker)`). False-flips are common — when a
   site adds a CSP comment or rotates a CSRF token the substring
   "moves" and the verifier reports a regression.
2. **`go-pentest-continuous-monitor`** diffs successive asset-inventory
   snapshots by hashing the whole blob and comparing hashes. Any
   single-URL change re-reports the entire inventory as changed; the
   human operator can't see which assets actually moved.
3. **`go-pentest-http-replay`** records request/response pairs but
   has no way to surface "what changed between this replay and the
   last one." The dashboard renders both bodies side-by-side and
   leaves diffing to the eyeball.

Three pain points, one shape: structured diff of two values whose
schema the comparer knows (HTTP-response shape, JSON document, HTML
DOM, asset-URL set).

The pattern is a textbook fleet primitive — pull the logic out, name
the section kinds, version them, and let every consumer call a single
service.

## Decision

Ship `go-fleet-diff-engine` as the canonical structured-diff primitive
for the fleet.

- **Shape**: `POST /diff {a, b, kind: "http_response|json|html|asset_set|text"}` →
  `{score: 0.0-1.0, sections: [{kind, path, before, after}], summary, kind}`.
- **Engines**:
  - `http_response`: status comparison + case-insensitive header diff
    (added / removed / changed) + body diff via the appropriate
    sub-engine based on `Content-Type` (json → `diffJSON`, html →
    `diffHTML`, else → `diffText`).
  - `json`: walk both values in parallel, emit per-path
    `json-path-changed|added|removed` sections. Arrays are positional;
    a length mismatch emits per-position adds/removes for the tail.
  - `html`: `golang.org/x/net/html` tokenizer; flatten to a list of
    `htmlNode{path, tag, attrs, text}` records keyed on a
    path like `html>body>div[0]>p[1]`. Position-aware: a "swap"
    surfaces as remove-at-X + add-at-Y rather than a text change.
  - `text`: stdlib LCS line-diff. Adjacent remove+add coalesce into a
    single `body-token-changed` section so the consumer sees "this
    line changed" rather than two unrelated events.
  - `asset_set`: Jaccard similarity over URL sets; per-URL
    `asset-added` / `asset-removed` sections.
- **Section kinds are public contract** — exploit-verifier /
  continuous-monitor / http-replay will gate state-machine transitions
  on the kind labels (`status-flipped`, `marker-vanished`,
  `json-path-changed`, etc.). Renaming a kind is an ADR amendment.
- **Score** is a coarse signal (0.0…1.0) for fast paths like
  "score == 1.0 → identical, no further work." Composition for
  `http_response`: 50% status, 40% body, 10% headers. Other kinds:
  shared-leaves / total. Asset-set: Jaccard.
- **`/selftest`** runs four baked-in diff fixtures (one per primary
  engine kind) and returns 503 on regression — fleet-runner deploy
  refuses to roll a build that fails it.
- **Stdlib-first**. The only external dependency is
  `golang.org/x/net/html` (already used elsewhere in the fleet). No
  `sergi/go-diff`, no `nsf/jsondiff`, no `pmezard/go-difflib`.

## Consequences

### Positive

- Three consumers can drop ~80 LOC each of bespoke diff code and gain
  better signal in return:
  - `go-pentest-exploit-verifier`: substring → `kind=marker-vanished`
    is a sharper "the PoC marker no longer appears" signal that
    survives unrelated body churn.
  - `go-pentest-continuous-monitor`: per-asset `asset-added` /
    `asset-removed` sections replace the whole-blob hash diff.
  - `go-pentest-http-replay`: structured sections make the dashboard's
    side-by-side render machine-summarizable.
- One central place to extend section kinds (e.g. when we want
  `csp-header-tightened`, we add it here and every consumer benefits).
- `score=1.0` short-circuit lets continuous-monitor skip downstream
  work cheaply on no-change snapshots.

### Negative

- One more network hop for the calling services. Mitigated by
  `DIFF_ENGINE_URL` env override + fail-open fallback to
  `bytes.Equal(a, b)` so a transient outage degrades to "naive
  string-equality flag" rather than a hard error.
- HTML position-aware diff cannot distinguish "true subtree moved" from
  "removed-at-X + added-at-Y". TRL ceiling 7 reflects this — lifting
  to TRL 8 means a Myers-on-trees / GumTree-style anchored move
  detector, which is the next ADR's problem.

## Migration

Each consumer wires in via a single env var; no behaviour change is
mandatory until the consumer's next bump.

```
# Per-service env:
DIFF_ENGINE_URL=http://fleet-diff-engine:18160   # in-mesh
# or
DIFF_ENGINE_URL=https://fleet-diff-engine.0exec.com   # cross-mesh
```

```go
// go-common/diffclient (future) — fail-open shape:
type Client interface {
    Diff(ctx context.Context, a, b []byte, kind string) (*Result, error)
}
// On any RPC error (network, 5xx, parse), the helper returns a
// degraded result: {score: 0 or 1 based on bytes.Equal, sections:[]}
// and logs a metric. Consumers see "diff engine unavailable, fell
// back to byte-equality" in service logs but the state machine
// keeps moving.
```

The three priority consumers (call out by repo):

1. `go-pentest-exploit-verifier` — swap `strings.Contains` for
   `Diff(prior, current, "http_response")` and gate on
   `kind=marker-vanished`.
2. `go-pentest-continuous-monitor` — swap snapshot-blob-hash for
   `Diff(prevURLs, currURLs, "asset_set")` and emit per-URL events.
3. `go-pentest-http-replay` — call `Diff(prevReplay, currReplay,
   "http_response")` on replay-pair fetches and surface the
   structured sections on the dashboard.

## Alternatives considered

- **In-process Go library** (no service, just a shared package):
  rejected. The fleet's cardinal rule is "change the library, not 130
  consumers," but a library-only fix would defer the ergonomic win —
  consumers would still need to bump go-common and re-deploy. A
  service makes "use the canonical diff" zero-bump for callers
  already wired to HTTP fleet primitives.
- **External diff libs** (`sergi/go-diff` for Myers,
  `nsf/jsondiff` for JSON): rejected on supply-chain grounds (per
  `feedback_npm_install_lag.md` we lean toward stdlib + curated
  deps). Our needs fit cleanly in stdlib + `x/net/html`.

## Hard rules carried from session

- `golang:1.24` (matches `go.mod`)
- compose pin `:0.1.0`
- git tag without `v` prefix
- gh repo private
- no GitHub Actions
- no `--force` on git
- no manual `/metrics` (provided by `go-common/server`)
