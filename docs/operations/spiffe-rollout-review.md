# SPIFFE fleet rollout: pre-mortem and post-mortem

**Review date:** 2026-10-06  
**Trust domain:** `spiffe://0exec.com`  
**Current rollout stage:** source and CI implementation; runtime rollout not started

## Pre-mortem

### Target and denominator

The canonical registry contains 534 entries: 391 containers (289 Compose,
97 Coolify, 5 external) and 143 static sites. Static sites have no process to
attest. The initial central adapter supports Compose workloads only; Coolify
and externally hosted workloads need their own runtime adapters. The latest
read-only fleet snapshot was refreshed at approximately `2026-10-06T13:14Z`:
375 containers were observed, 374 of 403 gateway probes returned 200, 27
warned, and 2 were broken. These are separate measures: registry services,
running containers, and gateway probes are not interchangeable denominators.

The task-credential-broker graph at `2026-10-06T12:53:44Z` reported zero
observed callers and zero runtime relations. It is not yet a proven live
dependency. The first canary must explicitly enroll one broker and one client;
no broad adoption percentage is claimed before that inventory exists.

An isolated candidate exists as Proxmox VM 102, `pve01-runtime-1`, at
`10.50.0.12`. It currently has no Docker containers, one node-exporter process,
about 1.48 GiB of guest memory available, and 36 GiB of guest root-disk space
free. The VM's operational role is not documented in the fleet inventory, and
it has no VM snapshot recorded. Its current network path from the production
Dockerhost to TCP 8081 times out. This makes it suitable only for a self-
contained staging stack whose server and test workloads stay on that VM; it is
not yet a production control-plane placement or cross-host agent target. The
staging stack must stay under explicit CPU/memory limits and be fully
removable without changing existing host networking.

### Failure scenarios and controls

| Failure | Effect | Control and abort condition |
|---|---|---|
| SPIRE Server or datastore unavailable | New SVIDs cannot be issued or renewed; service calls fail when identity expires | Keep legacy auth enabled during migration; verify renewal and restart recovery before expanding; abort on any SVID renewal failure |
| Agent compromised through Docker socket | Host-level container control and potential host compromise | Only the SPIRE Agent receives the Docker socket; application containers receive the Workload API socket only; do not add broad selectors |
| Wrong label or parent entry | A workload receives no identity or the wrong identity | Use deterministic per-service labels and exact SPIFFE-ID-to-principal mappings; test unknown-ID denial |
| mTLS listener becomes public | Exposes an internal broker surface to the public edge | Bind only inside the private Compose network; do not publish port 8443, add a gateway vhost, or weaken TLS authorization |
| Workload A replays Workload B's task proof | Cross-principal lease operations | Bind task proof principal to the verified mTLS peer on task-close and lease routes; test cross-principal denial |
| Broker regression makes all clients unhealthy | Broker-dependent service failures | Keep the existing public/API-key route; stage one caller first; preserve immutable image and config rollback |
| Shared Compose overlay is malformed | Recreated workloads lose env, volumes, or networks | Default SPIFFE off; mount the socket directory rather than a socket inode; render and inspect the exact Compose diff; apply one canary before any broader render |
| Capacity or host ownership is guessed | Runtime impact exceeds the reviewed target | Require current Fleet Runner capacity and exact host/service counts; stop when telemetry or scope is unavailable |
| Mixed runtimes are treated as identical | Coolify/external/static workloads receive an invalid socket mount | Keep the Compose adapter explicit; exclude other runtime classes until their adapters and ownership are verified |

### Rollback

Disable the affected service's `spiffe_workload` opt-in and its mTLS listener
settings, re-render only that Compose project, and return the caller to the
existing API-key path. Retain the SPIRE server, agent, datastore, trust bundle,
and registration entries until the workload has restarted and the rollback
health checks pass. Do not remove an SVID source while a task or lease client
is still using it.

## Post-mortem — implementation pass

### What changed

- Added a `go-spiffe/v2` adapter for rotating X.509-SVID mTLS and single-audience
  JWT-SVID retrieval/validation in Go Common.
- Added task-credential-broker support for verified SPIFFE peers on an optional
  private mTLS listener. The public API-key route remains available. Exact peer
  IDs map to existing policy principals, and task proofs are bound to that same
  principal on lease and task-close operations.
- Added a default-off Fleet Runner Compose opt-in that mounts only the agent
  Workload API socket and adds a deterministic Docker attestation label.
- Recorded the phased trust-domain decision in ADR-0041.

### Defects caught before release

1. The first task-manager refactor omitted its stored audience field; package
   compilation caught it before release.
2. An initial TLS credential-selection change rejected legacy API-key requests
   on HTTPS connections without a client certificate. The broker end-to-end
   tests caught the compatibility regression. The code now uses SPIFFE identity
   only when a peer certificate is present and verified, and it fails closed if
   that presented peer is malformed.
3. A task proof could otherwise be presented over a different allowlisted
   workload's certificate. The private listener now checks that the peer
   principal and the signed task principal match before lease or task-close
   handlers run.

### Runtime outcome and remaining gate

No SPIRE runtime, service Compose project, production key, database, or service
container was changed in the implementation pass recorded here. There was no
production interruption and no SVID was issued. The latest fleet read-only
snapshot is healthy for 374 of 403 gateway probes, with 27 warnings and 2
broken; the broker itself has no observed callers. Fleet Runner capacity for
`dockerhost` and `domainscope-runtime` could not be evaluated because the
dedicated `fleet-metrics-reader-fleet-runner` Vault record is absent. VM 102 is
now a documented candidate for a self-contained staging stack, but its limited
capacity, missing snapshot, and blocked cross-host TCP path rule it out as a
production control plane without a separate placement and backup review.
Source/CI evidence is not runtime integration evidence. Production migration
remains gated on capacity data, a production server/agent design, pinned SPIRE
artifacts, and a successful staged broker-plus-one-client rotation, restart,
wrong-identity denial, task-close, and rollback canary.

The full Fleet Runner suite has two pre-existing environment-sensitive test
failures on this workstation: tests requiring a root-owned private credential
file and a safe state-directory path fail under the local user. Both failures
were reproduced unchanged against fresh `origin/main`; the SPIFFE-specific
renderer tests pass.
