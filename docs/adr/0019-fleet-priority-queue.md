# ADR-0019 — Composite-scored ranker for the live findings pile (`go-fleet-priority-queue`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-opus-4-7-session-2026-05-16-priority-queue
* **Tags**: fleet-infra, ranker, primitives, dashboard, operator-tooling

## Context

`finding-triage` decides per-finding (`submit_ready` / `needs_review`
/ `drop`). It is a binary-shaped classifier — its output is a yes/no on
ingestion, not an order. In a live engagement the operator (or an
autonomous agent) routinely sits in front of 50–150 `submit_ready`
findings across 3–8 bug-bounty programs and has to pick one. The
existing tooling doesn't help with that choice:

  - `findings-store /findings?status=submit_ready` lists them but
    ordering defaults to insertion time, which has near-zero
    correlation with bounty value.
  - `payoff-tracker /scoreboard` ranks **programs** by 90-day payout,
    not individual findings.
  - The dashboard renders unsorted by default.

Three observed failure modes from `2026-05-15` and earlier sessions:

  1. **High-severity-but-old finding starves.** An RCE submitted three
     weeks ago that the program never replied to stays "submit_ready"
     forever; agents keep ranking it first by severity and burning
     their first action on a re-poke that the program ignores.
  2. **Low-severity-on-rich-program never surfaces.** A medium on a
     program with a $5k average payout outranks a high on a program
     with a $200 average payout — but nothing in the fleet expresses
     that today.
  3. **Dedup churn.** Findings that have been seen 30+ times across
     scanners (typical for the same misconfig replicated across a
     SaaS multi-tenant) clog the top of the list and crowd out
     genuinely fresh signal.

`FLEET-FUTURE-TOOLS.md` lists this as Tier B #17 with the strategic
note: "turns '100 yellow findings' into 'these 5 should ship today'".

## Decision

Ship `go-fleet-priority-queue` on `mesh-0exec` port 18169 with the
canonical composite formula:

```
score = severity
      × (1 + PayoffBoost · payoff_rate)
      × (1 - AgeDecayWeight · min(1, age_days / AgeFloorDays))
      × (1 - DedupPenaltyWeight · min(1, seen_count / DedupFloor))
```

Defaults (tunable at runtime via `POST /weights`):

| Coefficient            | Default                                                      |
|------------------------|--------------------------------------------------------------|
| `severity_weight`      | `{critical: 1.0, high: 0.7, medium: 0.4, low: 0.15, info: 0.05}` |
| `PayoffBoost`          | `0.5`                                                        |
| `AgeDecayWeight`       | `0.3` over `AgeFloorDays = 30`                               |
| `DedupPenaltyWeight`   | `0.2` over `DedupFloor = 10`                                 |

`payoff_rate` is computed from `payoff-tracker /scoreboard` as the
program's `avg_payout_cents / fleet_max_avg_payout_cents`, clamped to
`[0, 1]`.

Endpoints:

  - `GET /priority?cap=20&program=X` — top-N ranked, with `degraded[]`
    surfacing any sibling-down condition.
  - `GET /weights` / `POST /weights` (admin-token) — read / tune.
  - `GET /explain?finding_id=X` — per-component breakdown.
  - `GET /selftest` — three in-process sibling stubs → /priority →
    assert expected ordering. 200/503.
  - `GET /health`, `/version`, `/metrics` — from `go-common/server`.

Cache: 60 s in-memory keyed on `(program, cap)`. POST /weights
invalidates immediately so tuning is visible without TTL wait.

Upstream pulls all go through `safehttp.NewClient` with the standard
`ua.Build` user-agent. Each pull is independent and fails open: a
sibling-down condition adds a string to `degraded[]` on every
response and contributes a neutral default (no triage filter, zero
payoff rate, zero seen-count penalty) so the ranker still produces a
useful order with whatever signal is available. The service never
returns a 5xx for a sibling outage — 5xx is reserved for the ranker
itself misbehaving (and is hard to provoke; the formula is pure).

## Consequences

**Positive**

  - One canonical "what next" surface across dashboard, walkthrough,
    and agents. The same ordering shows up everywhere — no per-consumer
    re-scoring.
  - Tunable in production via `POST /weights` against a running
    container — no redeploy when an operator decides "stale findings
    should age out faster on this engagement".
  - Fail-open semantics mean a payoff-tracker outage degrades to
    severity+age+dedup ranking instead of a black screen — the queue
    keeps working through partial outages.
  - The `Breakdown` per finding is the audit trail: every score is
    explainable as the product of four numbers, each tied to a named
    upstream signal.

**Negative**

  - The formula is a heuristic, not a learned model. Until we have a
    "top-ranked finding paid / dropped" feedback loop into the weights,
    coefficient tuning is operator-judgment. We accept this — the
    explicit knobs are strictly better than the current "no ordering".
  - One more sibling for the dashboard. Mitigated by fail-open
    semantics (dashboard sees `degraded: ["priority-queue"]` and falls
    back to its previous "unsorted" view).
  - `payoff_rate` is program-aggregate, not target-aggregate or
    vuln-class-aggregate. A program with one $50k RCE and 200 $50
    informational payouts gets a smoothed-out average. Acceptable for
    v1 — the alternative is a much richer payoff-tracker query
    surface that doesn't yet exist.

**Mitigations**

  - `degraded[]` on every response is the structured signal a consumer
    needs to know "ranking is degraded; some signals missing".
  - 60 s cache is short enough that a freshly-landed triage decision
    is visible quickly; long enough to absorb dashboard polling.
  - `POST /weights` clamps coefficients to `[0, 1]` (or `[0, 5]` for
    `PayoffBoost`) — the formula can't be tuned into producing
    negative scores or unbounded blow-ups.

## Migration path (consumer ADRs)

Consumers adopt in priority order (no breaking changes — the existing
unsorted view keeps working):

1. **dashboard** — replace the unsorted `submit_ready` list with
   `GET /priority?cap=10`. Render the `Breakdown` per row as a small
   tooltip so the operator can see "why is this one first".
2. **walkthrough generator** — pull `GET /priority?cap=20`, render the
   top-N section-by-section as "act on this first, then this, then
   this". Use `/explain` to fill in the per-finding rationale.
3. **autonomous agents** — `GET /priority?program=$ENGAGEMENT&cap=5`
   becomes the "what next" call. Agent's first action of a session
   is whichever finding sits at position 0.

**Default**: fail-open. A missing `PRIORITY_QUEUE_URL` env var or an
outage returns the consumer to its pre-this-ADR behaviour (typically
"unsorted submit_ready list"), never an empty/wrong order.

**Per-call shape**: see `GET /priority` schema in the repo README; the
`degraded[]` array names sibling-down conditions; `cache_hit` flags
the 60 s in-memory cache.

## Alternatives considered

1. **Push ranking into `findings-store` as a query parameter** (e.g.
   `/findings?status=submit_ready&order=composite`). Rejected: the
   ranking needs joins across three services and an in-memory
   normalization step; embedding that in findings-store would make
   findings-store depend on payoff-tracker (today it depends on
   nothing). One-way arrows preserve the DAG.
2. **Move ranking into the dashboard JS.** Rejected: every consumer
   would re-do the same computation, and the formula would drift
   across consumers. The fleet rule is "change the library, not 130
   consumers" — here the analogue is "one ranker service, not three
   JS implementations".
3. **Learned ranker (XGBoost / LR over historical "paid vs not"
   outcomes).** Rejected for v1: we don't have enough resolved
   submissions yet for a calibrated model, and the explicit formula
   is operator-tunable today without training data. `trl_ceiling: 7`
   reserves room for a learned variant later when the labels exist.
4. **Per-program separate rankers.** Rejected: scoring is per-finding
   and program identity is just one input. A single ranker that takes
   `?program=X` is strictly simpler than N ranker instances and the
   formula already encodes the program signal via `payoff_rate`.

## References

- [`FLEET-FUTURE-TOOLS.md`](../../../FLEET-FUTURE-TOOLS.md) — Tier B #17 entry.
- [`ADR-0002`](0002-twenty-fleet-primitives.md) — the broader primitives strategy this ADR implements.
- [`go-pentest-findings-store`](https://github.com/baditaflorin/go-pentest-findings-store) — upstream #1; carries `seen_count` and `created_at`.
- [`go-pentest-finding-triage`](https://github.com/baditaflorin/go-pentest-finding-triage) — upstream #2; provides `/decisions` so we only rank submit_ready.
- [`go-pentest-payoff-tracker`](https://github.com/baditaflorin/go-pentest-payoff-tracker) — upstream #3; provides `/scoreboard` for `payoff_rate`.
- Consumer repos that should adopt next: dashboard, walkthrough generator, autonomous engagement agents.
