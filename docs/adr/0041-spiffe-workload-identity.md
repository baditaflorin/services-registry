# ADR-0041 — SPIFFE workload identity for the 0exec fleet

* **Status**: Accepted, phased rollout
* **Date**: 2026-10-06
* **Tags**: SPIFFE, SPIRE, workload-identity, mTLS, fleet-auth

## Context

The fleet has 534 registry entries: 391 containers (289 Compose, 97
Coolify, 5 external) and 143 static sites. A static site has no running
workload to attest. The existing Go Common and task-credential-broker
use service API keys for workload identity; that proves possession of a
shared key, not which running container made the call. The broker had no
observed callers in its latest graph window, so it is not yet a live
dependency of the fleet.

The broker's prior release and isolated canary did not reach healthy
runtime state. That failure was not diagnosed in the earlier rollout.
This ADR therefore separates source/CI evidence from runtime identity
proof and keeps current authentication available until an individual
caller completes the staged migration.

## Decision

Use SPIRE as the workload identity control plane and the maintained
`go-spiffe` library for application Workload API clients, SVID validation,
and mTLS. The registry maps each service mesh to an explicit trust domain
(`0exec` → `0exec.com`, `0crawl` → `0crawl.com`, `0docker` → `0docker.com`).
Each is a separate trust boundary and requires its own SPIRE server/agent
enrollment. The first runtime canary uses the isolated staging trust domain
`staging.0exec.com`. Do not introduce cross-fleet federation as part of this
change.

The first supported runtime adapter is Docker Compose on the approved
0exec runtime. SPIRE Agent's Docker workload attestor matches the explicit
`io.spiffe.workload` and `io.spiffe.workload-id` container labels rendered by
Fleet Runner. These labels are enrollment selectors, not immutable or
cryptographic identity. The registry opts a workload in explicitly; the fleet overlay mounts only the
`/run/spire/sockets` directory read-only and sets
`SPIFFE_ENDPOINT_SOCKET=unix:///run/spire/sockets/agent.sock`. Mounting the
directory lets workloads reconnect after the agent replaces its socket.
Application containers never receive the Docker socket. Agent and server
installation, registration entries, and socket permissions are managed
separately from the application overlay. Fleet Runner's `spiffe-entries`
command renders a JSON registration plan for one mesh and one exact,
already-attested node; it never changes SPIRE state.

The task-credential-broker keeps its existing API-key listener and gains
an optional, private mTLS listener. That listener accepts only verified
X.509-SVID peers whose exact SPIFFE IDs map to existing policy principals.
It is not published on the public gateway port. Lease operations continue
to require the broker-signed task proof, so mTLS authenticates the caller
but does not broaden resource authorization. A supplied but invalid
identity never downgrades to API-key authentication.

Coolify and externally hosted workloads require runtime-specific
attestation/socket delivery adapters before they can be enrolled. They
must not inherit the Compose socket mount by assumption. Static entries
remain outside workload identity. Existing public/API-key behavior is
retained until per-service staging evidence supports removal.

## Rollout gates

1. Pin and verify the SPIRE release and platform-specific image digests; use
   separate server and agent state. Bootstrap agents with short-lived,
   one-use join tokens.
2. Confirm runtime capacity, host ownership, Docker workload labels,
   socket ownership/mode, and a private path to the broker before any
   runtime mutation.
3. Install one agent on an isolated staging runtime; register only the
   broker and one canary caller. Verify X.509-SVID rotation, wrong-ID
   denial, task creation/close, lease acquire/revoke, restart recovery,
   and the unchanged API-key path.
4. Promote the same immutable image and trust configuration through the
   fleet's normal release path. Add callers in bounded batches with an
   exact registry/graph allowlist and rollback record.
5. Remove an API-key path only after all callers of that exact service
   are proven on SPIFFE and a rollback window has elapsed.

No fleet-wide identity is enabled by default. The registry's explicit
opt-in count is the rollout denominator; the live container snapshot is
reported separately because registry rows and runtime instances are not
the same unit.

## Consequences

**Positive**

- Service identity is short-lived and bound to an attested workload,
  avoiding long-lived per-service bootstrap secrets for migrated calls.
- Exact SPIFFE IDs map to the broker's existing default-deny principal
  and resource policy. Authentication and authorization remain separate.
- A central Compose overlay can deliver the Workload API socket without
  hand-editing application Compose files.

**Costs and limits**

- SPIRE server availability, trust-domain recovery, agent upgrades, and
  entry lifecycle become fleet operations that need monitoring and
  backups. A single server is not the final high-availability design.
- Docker workload attestation gives the agent access to Docker metadata
  and its host socket. Keep that privilege on the agent, harden it, and
  never mount the Docker socket into application containers.
- Migration adds runtime components and staged rollout work. It does not
  automatically remove API keys used for public gateway auth or unrelated
  downstream services.
- There is no safe one-template rollout for Compose, Coolify, external,
  and static entries; each runtime shape needs an explicit adapter or
  exclusion.
