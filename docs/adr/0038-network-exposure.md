# ADR-0038 — `network_exposure`: a 5-state exposure enum replacing `scope`

* **Status**: Accepted
* **Date**: 2026-08-26
* **Authors**: claude-sonnet-5 (with florin)
* **Tags**: registry, schema, nginx, gateway, security

## Context

`scope` was a binary field (`unset` | `"internal-only"`) whose only real
effect was flipping `fleet-runner nginx-render`'s template to emit an
nginx allow/deny IP block (ADR-0018, ADR-0023 Gap 4). That collapses five
operationally distinct situations into one bit:

- a service bound to `127.0.0.1` only, unreachable even from the private
  LAN (found live 2026-08-26: ephemeral SSH-tunnel listeners squatting
  ports 18315-18317 on the dockerhost, invisible to the registry entirely)
- a service reachable fleet-wide over the private LAN but never proxied
  through nginx at all — no vhost, no public DNS (`fleet-prometheus` and
  `fleet-discovery`'s own READMEs describe this as their intended design,
  even though their `services.json` entries actually declare a public
  vhost — see Non-goals below)
- today's `scope: internal-only` — a public vhost + DNS record, gated
  only by an nginx IP allowlist, no real auth
- the common case — public vhost + DNS, keystore `auth_request`-gated
- a genuinely open public vhost, `auth.type: none`

`scope` also only ever expressed intent — nothing confirmed a declared
vhost was actually rendered and live. `fleet-runner audit vhost-drift`
(pre-existing) only diffs registry-rendered output against live gateway
config for services it attempted to render; it never enumerated the
gateway's full `sites-enabled/` directory, so a live vhost with no
registry-side counterpart at all was structurally invisible to it.

## Decision

Replace `scope` with `network_exposure`, one of `loopback`,
`lan-internal`, `gateway-ip-allowlisted`, `gateway-authenticated`,
`gateway-public` (formalized in `schema/v1.json`).

**Computed, not hand-declared, wherever it can be derived** —
`bin/generate.py`'s `compute_network_exposure()` derives it from the same
signals that already drive `nginx-render`'s template:

- `kind: static` (GitHub Pages) → not applicable, omitted (143 of 519
  real entries — not a listening service at all).
- `cert_domain` set (a vhost gets rendered) → `scope: internal-only` in
  the resolved overrides ⇒ `gateway-ip-allowlisted` (exact successor to
  the old flag, 13 real entries); otherwise `auth.type` in
  (`api_key`, `path_token`) ⇒ `gateway-authenticated` (346 entries),
  `none` ⇒ `gateway-public` (15 entries).
- No `cert_domain` (no vhost intent declared) → registry data alone
  can't distinguish `loopback` from `lan-internal`; falls back to an
  explicit manual `network_exposure` override in `overrides.json` (2
  real entries: `claudia`, `plausible` — both `runtime: external`,
  reached at `http://dockerhost:<port>`, no vhost → `lan-internal`).

This keeps the actual `overrides.json` diff to ~4 lines instead of
hand-declaring ~370 entries, and — the point of this ADR — makes the
value structurally unable to drift from what `nginx-render` actually
does, since it's computed from the same inputs.

`scope` is no longer copied into `services.json` output at all (dropped
from the copy-through whitelist in both `make_entry` and
`make_external_entry`); it survives only as an *input* signal inside
`overrides.json` for the 13 remaining `gateway-ip-allowlisted` slugs.

`fleet-runner audit vhost-drift` is extended (not replaced — reuses its
existing SSH plumbing) to enumerate every live `sites-enabled/*.conf` on
the gateway and flag vhosts with no registry counterpart, or whose live
auth/allow-deny contents don't match what the registry's
`network_exposure` implies — closing the "registry says X, live says Y"
gap this ADR started from. See `go_fleet_runner`'s own changelog/ADR for
that half of the work.

## Non-goals

- **Whether `fleet-prometheus`/`fleet-discovery`/`fleet-alertmanager`
  *should* be `lan-internal`.** Their own READMEs describe "no gateway
  hop, no auth, no public surface," but their real `services.json`
  entries have `cert_domain` + `auth.type: api_key` set, and (for the
  first two) a live, `auth_request`-gated gateway vhost confirms that's
  real, not accidental drift. Registry and live gateway **agree** with
  each other here — only the READMEs (in a third repo,
  `go-fleet-metrics-hub`) are stale. This ADR classifies them per
  current registry+live reality (`gateway-authenticated`); un-winding
  the architecture to match the README's original intent is a separate
  decision for the service owner, not something a schema migration
  should silently resolve by picking a value that contradicts live
  evidence.
- **Orphaned SSH-tunnel listeners** (127.0.0.1-bound, not registered
  services at all) — `network_exposure` classifies registry entries, not
  arbitrary host listeners. A `fleet-runner audit orphaned-listeners`
  cross-referencing `ss -tlnp` against allocated `host_port`s is a
  separate follow-up.

## Consequences

**Positive**: single source of truth (the computation, not hand-entered
data); `overrides.json` stays small; `schema/v1.json` now documents the
field formally (closing the same undocumented-override gap `scope` and
`access_tier` both had).

**Negative / Mitigations**:
- A future container entry with no `cert_domain` and no manual
  `network_exposure` override silently gets `network_exposure` omitted.
  **Mitigated**: `compute_network_exposure()` prints a `WARN:` to stderr
  on every `bin/generate.py` run for exactly this case, so it surfaces
  in CI/regen output rather than staying silent.
- `network_exposure` is excluded from `services-public.json` (allowlist
  is fail-closed by default — no code change needed there), same
  reasoning as `scope` before it: which exposure class a service is in
  is itself an attack signal.

## Migration path

`go_fleet_runner` must read `network_exposure` (with a bounded, temporary
fallback to legacy `scope == "internal-only"` for the deploy-ordering
gap) before this repo's regenerated `services.json` — reading it
strictly reversed risks silently dropping the IP allowlist on the 13
real `gateway-ip-allowlisted` services. See the go_fleet_runner-side
changelog for the exact rollout sequencing.

## Alternatives considered

**A — Keep `scope` as a permanent alias, add `network_exposure`
alongside it forever.** Rejected: two fields describing overlapping
reachability state is exactly the kind of drift this ADR exists to
close; the field is cheap enough to compute that there's no ongoing cost
to a clean cutover once the rollout order (above) is followed.

**B — Hand-declare `network_exposure` per slug like `scope` was.**
Rejected: 376 of 519 entries are a pure function of `cert_domain` +
`auth.type` + `scope` already in the registry — hand-declaring them
would mean maintaining ~370 lines of data that could independently drift
from what `nginx-render` actually renders, recreating the exact bug this
ADR fixes.

## References

- [ADR-0018 — Canonical internal-only sandbox of vulnerable targets](0018-fleet-sandbox-targets.md) (superseded: `scope: internal-only` → `network_exposure: gateway-ip-allowlisted`)
- [ADR-0023 — Deploy pipeline gaps from Phase 1 bootstrap](0023-deploy-pipeline-gaps-from-phase-1-bootstrap.md) Gap 4 (superseded — the `{{if eq .Scope "internal-only"}}` template conditional it proposed is now `{{if eq .NetworkExposure "gateway-ip-allowlisted"}}`) and Gap 5 (confirms `sites-enabled/` holds regular files, not symlinks — informs the `audit vhost-drift` path fix)
- `bin/generate.py` — `compute_network_exposure()`, `NETWORK_EXPOSURE_VALUES`
- `schema/v1.json` — `network_exposure` property
- `go_fleet_runner` — `registry.go` (`ServiceEntry.NetworkExposure`), `nginx_render.go`, `audit_internal_only_auth_gap.go`, `remediate_cve.go`, `batch.go` (`audit vhost-drift`)
