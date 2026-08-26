# ADR-0039 — `placement`: pinned vs. replicable classification

* **Status**: Accepted
* **Date**: 2026-08-26
* **Authors**: claude-sonnet-5 (with florin)
* **Tags**: registry, schema, placement, scheduling

## Context

Work on dynamic load-based dispatch (see private `fleet-state/OPS.md` "0mcp
fleet — Woodpecker dual-host dispatch") surfaced a real instinct that had no
formal home: some things can safely run on whichever host has capacity
(a build agent), and some structurally can't (Gitea's SSD-backed git
storage — moving it means moving the data). Getting this wrong is expensive
in both directions — routing a stateful service to "wherever's free" risks
data loss or a cold, empty volume; refusing to ever move a genuinely
stateless one wastes the whole point of dynamic placement. Nothing in the
registry currently distinguishes the two, so the only way to know is to
already know the service.

## Decision

Add `placement: "pinned" | "replicable"`, optional, to `schema/v1.json`,
following the exact convention `trl` already established: **absence means
not yet assessed**, not a default value silently applied. No automatic
derivation — unlike `network_exposure` (ADR-0038), nothing in the existing
registry signals (`kind`/`mesh`/`runtime`) reliably predicts whether a
service holds meaningful local state, so guessing would be worse than
leaving it unset. Declared per-slug in `overrides.json`, same mechanism as
`trl`.

- `pinned` — holds state that doesn't trivially move with it (a data
  volume, a database, anything where "just start it somewhere else" loses
  or orphans data). Never a dynamic-placement candidate as-is.
- `replicable` — safe to start on any capacity-eligible host. The
  common case for this registry's `kind: container` entries, which are
  overwhelmingly stateless recon/scanner services by design — but stated
  explicitly per-service rather than assumed, so a future stateful
  addition doesn't silently inherit a wrong default.

Audited the current ~220 entries for a first pass (2026-08-26): found
none in this specific catalog that clearly need `pinned` today (the two
`runtime: external` entries, `claudia` and `plausible`, are third-party
infra outside this registry's own container fleet, not scheduling
candidates either way). This field is being established ahead of need,
not to fix a present miscategorization — the near-term consumers are
fleet infrastructure (Woodpecker/CI agents, build hosts) that live
outside this registry's catalog entirely, in `fleet-state`.

## Consequences

**Positive**: a placement tool (dynamic dispatch, once built) has one
place to check before ever proposing a service for relocation, instead of
re-deriving "does this hold state" per incident. **Negative**: another
optional field to keep current — mitigated the same way `trl` already is
(absence is a valid, meaningful state, not a bug). **Mitigation**: no
enforcement added yet (nothing currently reads this field to gate an
action) — it's declarative groundwork, consistent with not building a
placement tool before there's a second real use case beyond Woodpecker's
already-working native dispatch (see OPS.md "top 10 hardest challenges",
item 9: don't build a custom coordinator where a working one exists).

## Migration path (service ADRs)

Nothing to migrate — the field is additive and optional, `generate.py`
passes it through unchanged when present and omits it when absent. Added
to `PUBLIC_FIELDS` (operational scheduling metadata, not a disclosure
risk, same reasoning as `trl`).

## Alternatives considered

- **Compute it from `kind`/`runtime`** (e.g. `runtime: external` ⇒
  pinned): rejected — the audit above shows the correlation is weak in
  this registry's actual data (both `external` entries here are pinned
  for reasons `runtime` alone doesn't capture: they're not even in the
  fleet's own container orchestration). A wrong auto-derived value that
  looks authoritative is worse than an honest "not assessed."
- **A boolean `stateful`** instead of an enum: rejected in favor of
  `placement`'s two named states — reads directly as a scheduling verb
  ("can I place this?") rather than requiring the reader to know the
  boolean's polarity.

## References

- Private `fleet-state/OPS.md` — "0mcp fleet — Woodpecker dual-host
  dispatch" and the "top 10 hardest challenges" writeup this ADR answers
  item #6 of.
- ADR-0038 (`network_exposure`) — the precedent for a classification
  field added to `schema/v1.json` outside the original kind/mesh/runtime
  axes, including the same computed-vs-declared design conversation.
