# ADR-0030 — Cross-fleet federation

* **Status**: Proposed
* **Date**: 2026-05-19
* **Authors**: fleet-agent, baditaflorin
* **Tags**: federation, auth, keystore, mTLS, services-registry, go-apikey-service, go-fleet-graph, trust-boundary

## Context

Every primitive in this codebase — `services-registry`,
`go-apikey-service`, `go-fleet-graph`, `go-fleet-secrets`,
`go-fleet-runner` — is designed for **one operator running one fleet**.
The registry's `services.json`, the keystore's `keys.db`, the graph's
span store, and the gateway's nginx config all assume a single trust
domain rooted at one dockerhost + one keystore + one operator.

That assumption holds today and is good for blast-radius reasons (see
[ADR-0027](0027-fleet-auth-canonical-flow.md) — the consumers-list
model collapses if there's a shared identity across operators). It
won't hold the first time a second fleet exists — whether that's a
second instance of this stack run by a partner operator, a hosted
"public" version of `services-registry`, or a satellite fleet in a
different region that occasionally needs to call services in the
primary fleet.

Concrete questions surfaced during the 2026-05 audit pass that this
ADR exists to answer **before** anyone tries to bolt cross-fleet
calls onto the current single-trust-domain plumbing:

* If fleet A's `go-fleet-dns-sync` needs to query fleet B's
  `go-fleet-resolver-quorum` for a one-off cross-region validation,
  what key does it present? Fleet A's `ak_*` is meaningless to
  fleet B's keystore.
* If a peer fleet is compromised, what's the blast radius? Today
  there's nothing to compromise across boundaries because there is
  no boundary; we need to design that boundary in **before** we
  punch the first hole through it.
* If the public `services-registry` (G3 published `services-public.json`)
  becomes a discovery surface for peer fleets, what subset of metadata
  is safe to expose, and how do peers consume it without trusting it?
* `go-fleet-graph` aggregates spans within one fleet. Do cross-fleet
  calls produce a single distributed trace, or do they terminate at
  the fleet boundary with each side keeping its own half?

Nothing in this stack is wrong for not solving federation today.
This ADR records the **proposed** design so the first cross-fleet
call doesn't get implemented ad-hoc in `go-common/safehttp` against
a hardcoded peer URL.

## Decision

**Proposed: peer-to-peer mTLS federation with per-peer CA pinning.**
Each fleet keeps its own keystore as the sole identity authority for
its principals. Inter-fleet calls present client certificates issued
by the calling fleet's CA; the receiving fleet pins the caller's CA
fingerprint and re-issues a local short-lived `ak_*` token after a
successful handshake. Keys never cross the trust boundary.

This is the **minimum viable** option (option 1 below). A federated
identity broker (option 3) is the right answer at scale; it's
deferred until there are ≥4 peer fleets or a regulatory driver for
single-sign-on.

### Trust model

```
fleet A                                           fleet B
─────────                                         ─────────
  go-fleet-dns-sync                                 go-fleet-resolver-quorum
        │                                                   ▲
        │ outbound HTTPS w/ client cert                     │
        │   cert issued by fleet-A-CA                       │
        │   SNI: peer.fleet-b.example                       │
        ▼                                                   │
  fleet A egress  ──────────  internet  ──────────  fleet B ingress (nginx + mTLS)
                                                            │
                                                            ▼
                                                     verify cert chain ↦ fleet-A-CA
                                                     (pinned in federation.yaml)
                                                            │
                                                            ▼
                                                     mint local ak_* via
                                                     go-apikey-service.Issue(
                                                       user=peer:fleet-A:go-fleet-dns-sync,
                                                       scope=<configured-for-peer>,
                                                       ttl=15m)
                                                            │
                                                            ▼
                                                     ADR-0027 canonical flow
                                                     resumes locally
```

The gateway-side keystore middleware ([ADR-0027](0027-fleet-auth-canonical-flow.md))
is reused unchanged. The only new surface is the **mTLS-to-local-token
exchange** at the federation ingress, which is a small nginx
`auth_request` extension to a new `go-apikey-service` endpoint
(`/peer/exchange`, sketched in "Open questions" below).

### Discovery

Each fleet publishes a sanitized peer-mirror of its registry as
`peer-services.json` — derived from the same generator that produces
[G3's services-public.json](https://github.com/baditaflorin/services-registry/blob/main/services-public.json),
restricted to services that have `peer_visible: true` declared in
their `service.yaml`. Peer fleets fetch this over HTTPS (no mTLS
required for discovery; the data is by definition safe to publish)
and consume it as the cross-fleet discovery format.

`peer_visible` defaults to `false`. A service must explicitly opt
in to being callable by a peer. There is no fleet-wide "expose
everything" switch.

### Configuration shape (operator-facing, no code yet)

Each fleet maintains a top-level `federation.yaml` in `services-registry`:

```yaml
# federation.yaml — proposed (ADR-0030)
peers:
  - id: fleet-b
    discovery_url: https://services-registry.fleet-b.example/peer-services.json
    ingress_host: peer.fleet-b.example
    ca_fingerprint_sha256: ab:cd:ef:01:…   # pinned out-of-band, rotated manually for now
    allowed_scopes:
      - resolver-quorum:read
    expires: 2026-11-19   # forces a fingerprint-rotation review
```

`service.yaml` gains one new field, additive to ADR-0027's `auth:` block:

```yaml
# service.yaml — proposed addition
peer_visible: true            # default false; opts the service into peer-services.json

auth:
  reads_secrets: […]          # unchanged from ADR-0027
  calls_services: […]         # unchanged
  calls_peers:                # new: declared cross-fleet calls
    - fleet-b:resolver-quorum
```

The schema additions are namespaced so a fleet that never federates
sees no change to its existing service definitions.

## Consequences

**Positive**

* **Keys never cross the trust boundary.** Fleet A's `ak_*` is
  meaningless outside fleet A. Compromise of fleet B's keystore
  doesn't yield a single credential that's valid in fleet A.
* **Revocation is local.** Each fleet revokes peer access by
  removing the CA pin from `federation.yaml`; no coordination
  with the peer is required.
* **ADR-0027 flow reused end-to-end.** The peer-exchange endpoint
  produces a normal local `ak_*`; everything downstream of the
  ingress sees a standard fleet principal. No new middleware on
  the callee path.
* **Discovery is publishable.** `peer-services.json` is by
  construction safe to expose — peers consume it without
  trusting it, since the trust check is the mTLS handshake, not
  the discovery feed.
* **Audit clarity.** `go-fleet-graph` and access logs see
  `user=peer:fleet-A:<slug>`, which is grep-distinct from local
  principals. A compromised peer's traffic is trivially isolatable.

**Negative**

* **O(N²) CA exchange.** N fleets means N(N-1) pinned fingerprints
  in aggregate. Manageable at N≤5; painful at N≥10. The federated
  broker option exists to defer this.
* **Manual CA rotation.** Until phase 3 lands rotation tooling,
  rotating a peer's CA is "edit `federation.yaml`, redeploy
  ingress." Forgettable, and a stale pin fails closed (good)
  but visibly (peer fleet's calls 401, operator gets paged).
* **No transitive trust.** If fleet A trusts B and B trusts C,
  A does **not** trust C. A must independently pin C. This is
  deliberate — it preserves the local revocation property — but
  surprises operators who expect transitivity.

**Operational**

* Each peer fleet maintains `federation.yaml` listing trusted peers
  with pinned CA fingerprints + expiry dates. Expiry forces a
  review cadence even when nothing else changes.
* `service.yaml` schema gains `peer_visible: bool` (default `false`)
  and `auth.calls_peers: []` (default empty). Both additive; no
  migration needed for non-federated fleets.
* `bin/generate.py` (registry generator) gains a `peer-services.json`
  output filtered on `peer_visible == true`. Mirrors the existing
  public-mirror pattern.
* `go-fleet-runner audit federation-pin-staleness` is a future
  audit verb: flags pins within 30 days of `expires`, mirroring
  the cadence-review approach `audit fleet-auth-scope` takes for
  internal keys.

## Migration path

This ADR is design-only. No code changes land with it. Implementation
is phased to keep each step reversible.

### Phase 0 — this ADR + schema additions (no behavior change)

1. Land this ADR as `Status: Proposed`. Operator review.
2. Add `peer_visible: bool` and `auth.calls_peers: []` to the
   `service.yaml` schema validator (ADR-0016's
   `fleet-schema-validator`). Default values mean every existing
   `service.yaml` continues to validate unchanged.
3. Document `federation.yaml` shape in `RUNBOOK-UNATTENDED.md` as
   "reserved, see ADR-0030." File is not yet consumed.

### Phase 1 — mTLS peering, manual cert exchange

4. Stand up a second fleet (simplest: a second `services-registry`
   + `go-apikey-service` + `go-fleet-graph` on a separate
   dockerhost / LXC).
5. Each side generates a CA, exchanges fingerprints out-of-band,
   commits `federation.yaml` with the pin + expiry.
6. Implement `/peer/exchange` in `go-apikey-service`: accepts
   the client-cert subject from nginx's `$ssl_client_s_dn`, looks
   up the peer in `federation.yaml`, mints a short-lived `ak_*`
   with `user=peer:<fleet>:<slug>` and the peer's `allowed_scopes`.
7. Configure ingress nginx for mTLS on `peer.<fleet>.example` with
   `ssl_verify_client on; ssl_client_certificate <pinned-ca>;`.
8. Smoke test: one declared `calls_peers` edge, end-to-end.

### Phase 2 — `federation.yaml` CLI tooling

9. `fleet-runner federation add-peer <id> --discovery-url … --ca-fingerprint …`
   — atomic edit of `federation.yaml` + nginx reload.
10. `fleet-runner federation list` / `… remove-peer <id>` /
    `… show <id>`.
11. `fleet-runner audit federation-pin-staleness` — flags pins
    near expiry.

### Phase 3 — automated rotation

12. Replace manual fingerprint exchange with an ACME-style
    rotation protocol (TBD; SPIFFE workload API is the obvious
    candidate but pulls in the broker option below).
13. CA rotation becomes a `fleet-runner federation rotate-ca`
    verb that publishes the new fingerprint to peers' discovery
    URLs and waits for ack before flipping ingress.

If the broker option (option 3 below) becomes necessary before
phase 3, phase 3 is skipped — the broker subsumes rotation.

## Alternatives considered

**Option 1 — Peer-to-peer mTLS (recommended above)**

See "Decision" — keys never cross, revocation is local, reuses
ADR-0027 downstream. O(N²) CA exchange is the cost; tolerable at
small N.

**Option 2 — Hub-and-spoke federation**

One fleet (the "hub") is the trust root. Spoke fleets only trust the
hub's CA; they don't trust each other. Inter-spoke calls route
through the hub.

Pros: O(N) CA exchange. Simpler `federation.yaml` (one peer entry,
ever).

Cons: Single point of failure — the hub is in the critical path of
every cross-fleet call. Hub compromise = full federation compromise.
Hub operator becomes a privileged party, which doesn't match this
codebase's "every operator is equal" framing. Rejected as the
default; could become correct for a "primary operator + satellite
operators" deployment, but that's not the current shape.

**Option 3 — Federated identity broker (SPIFFE/SPIRE, OIDC federation)**

A dedicated identity broker (SPIFFE/SPIRE workload API, or OIDC
federation per [RFC 8414](https://datatracker.ietf.org/doc/html/rfc8414)
+ [RFC 8705](https://datatracker.ietf.org/doc/html/rfc8705) for
mTLS-bound tokens) issues short-lived SVIDs / JWTs that any peer
fleet can verify against the broker's JWKS. No per-peer CA pinning.

Pros: Cleanest separation. Automatic rotation. Scales O(N).
Industry-standard primitives.

Cons: Heavy infra dependency — SPIRE servers, agents, attestors;
or an OIDC provider with federation configured. Adds a privileged
service that every fleet must trust. Operationally, this is a
second keystore-equivalent system on top of `go-apikey-service`,
not a replacement. The complexity isn't justified at N≤5 fleets.

**Deferred, not rejected.** Worth re-evaluating at the first sign
of operational pain from O(N²) CA exchange (>3 active peer
relationships) or any regulatory driver for SSO.

**Option 4 — No federation; document the boundary**

Each fleet stays isolated; cross-fleet integration is left as
"the user copies data via CSV / publishes a webhook to a
public URL with bearer-token auth." Status quo if this ADR is
rejected.

Pros: Zero new infra. No new attack surface.

Cons: The first real cross-fleet integration gets implemented
ad-hoc (probably a hardcoded peer URL + a shared bearer token in
`.env`), which is exactly the failure mode this ADR exists to
prevent. Captured here so future agents see that the explicit
"do nothing" path was considered, not omitted.

## Open questions for operator

1. **Who issues the inter-fleet CA?** Self-signed per-fleet root
   is simplest (each fleet runs its own CA out of
   `go-fleet-secrets`). Alternative: a shared private CA (e.g.
   Smallstep CA) that issues all peer-fleet roots. Self-signed is
   recommended for symmetry — neither fleet is privileged — but
   forces manual fingerprint exchange. **Operator decision.**

2. **Does `go-apikey-service` need a `/peer/exchange` endpoint,
   or do peers re-issue local tokens after mTLS handshake at the
   gateway layer?** The ADR sketches `/peer/exchange` on
   `go-apikey-service` because it keeps token issuance in one
   place. The alternative — nginx mints a JWT itself after mTLS
   succeeds — is fewer hops but duplicates the keystore's role.
   **Recommend `/peer/exchange`; needs operator confirmation
   before implementation.**

3. **`go-fleet-graph`: do we federate spans across fleets, or
   does each fleet's graph stay local-only?** Federating spans
   means a cross-fleet trace ID propagates and both fleets'
   graphs join on it — better forensics, but leaks call patterns
   across the trust boundary. Local-only graphs mean each fleet
   sees its own half; cross-fleet incidents require manual
   correlation by trace ID. **Recommend local-only for phase 1;
   revisit if forensic gaps emerge.**

4. **`services-registry`: is the sanitized peer-mirror
   (`peer-services.json`, analogous to G3's
   `services-public.json`) the cross-fleet discovery format,
   or do we want a richer peer-only schema?** The G3 public
   mirror strips internal fields; the peer mirror could expose
   more (e.g. declared `calls_peers` edges) since the consumer
   is a trusted peer fleet, not the public internet. **Recommend
   starting with G3's exact shape + the `peer_visible` filter;
   add fields later if peers need them.**

5. **Per-peer scope vs. per-service scope.** The proposed
   `federation.yaml` declares `allowed_scopes` per peer fleet.
   Should this be per-(peer, service) instead, so fleet B can
   only call fleet A's `resolver-quorum`, not fleet A's
   `dns-sync`? **Recommend yes, per-(peer, service); the YAML
   gets longer but the blast radius shrinks. Operator
   confirmation needed.**

6. **Expiry semantics.** `federation.yaml` peers have an
   `expires:` field that forces review. What happens at expiry:
   hard-fail (peer calls 401) or warn-and-continue with audit
   verb screaming? **Recommend hard-fail; matches the
   "fail-closed on missing auth" posture of ADR-0027.**

## References

* [ADR-0016](0016-fleet-schema-validator.md) — schema validator
  (must learn `peer_visible` + `calls_peers` fields)
* [ADR-0024](0024-phase-3-consumer-migration-and-primitive-to-primitive-pattern.md)
  — primitive-to-primitive pattern (intra-fleet analog of what
  this ADR proposes inter-fleet)
* [ADR-0025](0025-vault-integrated-admin-tokens.md) — admin
  token class (where the federation CA private key would live)
* [ADR-0026](0026-fleet-overlay-and-shared-pentest-network.md)
  — shared DNS plane (intra-fleet; federation deliberately does
  NOT extend this across the trust boundary)
* [ADR-0027](0027-fleet-auth-canonical-flow.md) — fleet-auth
  canonical flow (the local downstream of `/peer/exchange`)
* [ADR-0028](0028-image-tagging-and-version-bump-policy.md) —
  recent style baseline
* SPIFFE / SPIRE — https://spiffe.io/
* RFC 8414 (OAuth 2.0 Authorization Server Metadata) — https://datatracker.ietf.org/doc/html/rfc8414
* RFC 8705 (OAuth 2.0 Mutual-TLS Client Authentication) — https://datatracker.ietf.org/doc/html/rfc8705
* [ADR-0029](0029-compose-as-deploy-contract.md) — compose-as-deploy-contract
  (merged 2026-05-19 via PR #11; took the 0029 slot, hence this ADR is 0030)
* G10 from `FLEET-FUTURE-TOOLS-v2.md` (operator's planning doc)
