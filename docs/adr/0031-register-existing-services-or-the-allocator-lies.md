# ADR-0031 — Register existing services, or the allocator lies

* **Status**: Accepted
* **Date**: 2026-05-20
* **Authors**: claude-opus-4-7 (with florin)
* **Tags**: registry, allocate-port, fleet-runner, plausible, incidents

## Context

`fleet-runner allocate-port` derives the in-use host-port set from the
registry (`services.json` → `services.ports.json`). The dockerhost is
the source of *running* containers; the registry is the source of
*who-owns-what*. The allocator trusts the registry exclusively — it
does not (and intentionally should not) shell into the dockerhost on
every call. That makes any container running on the dockerhost
**without** a corresponding registry row a silent squatter.

This has now hit the fleet twice in three days:

* **2026-05-18 — fleet-discovery.** Already-running on port 18201 but
  not in the registry, so `allocate-port` re-handed-out 18201 for a
  fresh service. Fixed by ADR-coordinated `$expand` of
  `go-fleet-metrics-hub` (overrides.json), surfacing three children
  (`fleet-discovery`, `fleet-grafana`, `fleet-prometheus`).
* **2026-05-19 — plausible.** Plausible Analytics
  (`ghcr.io/plausible/community-edition:v3.2.1`) was running on the
  dockerhost listening on host port 18204, with its own
  hand-managed docker-compose at `/opt/services/plausible/`. Not in
  the registry. `fleet-runner allocate-port --count 1` returned
  18204 as free. The subsequent `fleet-persona` deploy then proceeded
  to 18204, and fleet-runner's container-rebuild logic clobbered
  `/opt/services/plausible/docker-compose.yml` (it saw a container
  listening on the requested host port and assumed it was an old
  deploy of fleet-persona). The container kept running due to the
  port-conflict abort, but the compose file was corrupted; recovery
  required restoring from a sibling `compose.yml` and moving
  fleet-persona to 18209 (commit bc15d11).

Both incidents share one shape: *running container, no registry row,
allocator lies, next deploy clobbers*. The fleet-discovery fix
generalised through `$expand` because that container had a fleet
parent repo. Plausible is harder: it's a third-party upstream image,
managed outside the fleet repo set, with no `mesh-*` GitHub topic the
generator can latch onto. The topic-driven generator (bin/generate.py)
will never see it.

## Decision

Add a `$external` directive to `overrides.json`, parsed by
`bin/generate.py`, that emits standalone registry entries for
third-party containers running on the dockerhost outside the fleet
repo set.

**Shape** (top-level in `overrides.json`):

```json
"$external": [
  {
    "id": "plausible",
    "name": "Plausible Analytics",
    "description": "...",
    "category": "infrastructure",
    "mesh": "0exec",
    "kind": "container",
    "language": "other",
    "runtime": "external",
    "host_port": 18204,
    "container_port": 8000,
    "url": "http://dockerhost:18204",
    "health_url": "http://dockerhost:18204/api/health",
    "repo_url": "https://github.com/plausible/community-edition",
    "auth": { "type": "none" },
    "tags": ["external", "third-party", "analytics"],
    "external_compose_dir": "/opt/services/plausible/",
    "external_image": "ghcr.io/plausible/community-edition:v3.2.1",
    "scope": "internal-only",
    "why": "incident reference — allocate-port re-handed 18204 on 2026-05-19"
  }
]
```

**Required fields**: `id`, `host_port`, `repo_url` (upstream source).
Everything else has a sensible default (see `make_external_entry` in
`bin/generate.py`).

**Defaults**:

* `runtime: external` — the documented "runs outside the fleet,
  included for reference only" runtime value already in the schema
  (ADR-0029 lists it under the FLEET.md axes table). This is the
  flag fleet-runner-deploy/build dispatches off to *keep hands off*
  the compose dir.
* `kind: container`, `mesh: 0exec`, `language: other`,
  `category: infrastructure`, `auth: { type: none }`.

**`$rules` are NOT applied to external entries.** Fleet-wide rules
(wildcard cert pinning, proxy_egress by category, …) are knobs for
fleet-managed services. An upstream-managed third-party container
has no fleet vhost and no fleet egress story — silently injecting
`cert_domain: wildcard.0exec.com` would be misleading metadata.

## Consequences

### Positive

* `allocate-port` and `audit registry-host-port-set` see external
  containers' host ports. No more "free" ports that are actually
  taken.
* Class of incident closes: the registry is now a complete picture
  of *every claimed host port*, not just fleet-owned ones.
* The `external_compose_dir` field flags the compose-file ownership
  boundary — agents reading the registry can see "this lives at
  /opt/services/plausible/, don't render it via fleet-runner
  scaffold-compose."
* Future third-party additions (Grafana Cloud agent, Loki, a hand-
  rolled prometheus federation target, an external bug-bounty
  intake) get one-line registration.

### Negative

* Registry shape grows by one top-level directive (`$external`).
  Acceptable: `$rules` and `$expand` already establish the
  `$`-prefixed-metadata pattern.
* Some fields on external entries (`external_compose_dir`,
  `external_image`) are not in the JSON schema yet. The schema is
  not strictly enforced (existing `cert_domain` / `proxy_egress` /
  `extra_server_names` fields aren't in it either), so this is no
  regression — schema tightening is a separate ADR.

### Mitigations

* **Public mirror**: `external_compose_dir` and `external_image`
  are NOT added to `PUBLIC_FIELDS`, so they don't leak through the
  sanitized `services-public.json`. They are operator-only metadata.
* **Test coverage**: `bin/test_generate.py` gains
  `TestExternalEntry` covering happy path + the three hard-error
  shapes (missing id / host_port / repo_url).

## Migration path

* Plausible registered as the first `$external` entry in this same
  commit (overrides.json: `$external[0]`).
* No fleet-runner code changes required — the existing dispatch
  on `runtime` (`compose` / `external` / etc.) already gates
  deploy/build/bump off `external` entries.
* When a new third-party container lands on the dockerhost, the
  procedure is one PR to `services-registry`:

  1. Add a `$external` row with the container's host_port and
     upstream repo_url.
  2. `python3 bin/generate.py` regenerates `services.json` and
     `services.ports.json`.
  3. `fleet-runner allocate-port --count 1` now sees the port as
     taken.

## Alternatives considered

### A — Hand-roll a synthetic fleet repo with a `mesh-0exec` topic

Create an empty `baditaflorin/plausible-registration` repo with a
`mesh-0exec` topic so the topic-driven generator picks it up.
**Rejected**: pollutes the fleet repo set with placeholder repos
that don't have a Dockerfile, don't pass `fleet-runner build-test`,
and confuse `fleet-runner audit compose-shape`. The honest answer
is "this isn't a fleet repo" — let the registry say so.

### B — Squat-detection from a periodic `docker ps` audit

`fleet-runner audit registry-host-port-set` already finds services
*without* a registered host_port. Generalise the reverse — find
containers on the dockerhost without a slug. **Rejected as primary
fix**: a useful complementary audit (and worth adding separately),
but it's a discovery tool, not a registration mechanism. The fix
must be: register the thing, so the allocator stops lying.

### C — `$expand` plausible from a synthetic parent

Reuse the existing `$expand replace_parent: true` machinery with
some fleet repo as the synthetic parent. **Rejected**: `$expand` is
semantically "one repo emits N catalog entries because it ships N
compose services." Plausible's repo is not in the fleet at all; the
metaphor doesn't fit. Naming it `$external` is honest.

## References

* ADR-0029 — Compose-as-deploy-contract (introduced `$expand` as
  the precedent for `$`-prefixed registry directives)
* `services-registry/overrides.json` — `$external[0]: plausible`
* `services-registry/bin/generate.py` — `make_external_entry`,
  `split_overrides` (4-tuple return)
* `services-registry/bin/test_generate.py` — `TestExternalEntry`
* 2026-05-19 plausible incident — fleet-persona deploy clobbered
  `/opt/services/plausible/docker-compose.yml`, restored from
  sibling `compose.yml`. See commits bc15d11 (move fleet-persona)
  and the plausible compose dir on the dockerhost.
* 2026-05-18 fleet-discovery incident — port-allocation collision on
  18201; precursor to this ADR's pattern.
