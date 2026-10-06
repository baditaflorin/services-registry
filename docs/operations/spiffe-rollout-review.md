# SPIFFE fleet rollout: pre-mortem and post-mortem

**Review date:** 2026-10-06  
**Trust domain:** `spiffe://0exec.com`  
**Current rollout stage:** isolated staging canary passed; production adoption not started

## Pre-mortem

### Target and denominator

The canonical registry contains 534 entries: 391 containers (289 Compose,
97 Coolify, 5 external) and 143 static sites. Static sites have no process to
attest. The initial central adapter supports Compose workloads only; Coolify
and externally hosted workloads need their own runtime adapters. The preflight
read-only fleet snapshot was captured at approximately `2026-10-06T13:14Z`:
375 containers were observed, 374 of 403 gateway probes returned 200, 27
warned, and 2 were broken. These are separate measures: registry services,
running containers, and gateway probes are not interchangeable denominators.

The preflight task-credential-broker graph snapshot at
`2026-10-06T12:53:44Z` reported zero observed callers and zero runtime
relations. It was not a proven live dependency. The later isolated staging
canary explicitly enrolled one broker and one client, as detailed below; it
does not establish production adoption or justify a fleet-wide percentage.

At the preflight baseline, isolated candidate VM 102, `pve01-runtime-1`, at
`10.50.0.12` had no Docker containers, one node-exporter process, about 1.48
GiB of guest memory available, and 36 GiB of guest root-disk space free. The
VM's operational role is not documented in the fleet inventory, and it has no
VM snapshot recorded. Its network path from the production Dockerhost to TCP
8081 timed out. It was used only for a self-contained staging stack whose
server and test workloads stayed on that VM; it is not a production control-
plane placement or cross-host agent target. The staging stack stayed internal,
used explicit CPU/memory limits, and was removable without changing existing
host networking.

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

## Post-mortem — implementation and staging canary

### What changed

- Added a `go-spiffe/v2` adapter for rotating X.509-SVID mTLS and single-audience
  JWT-SVID retrieval/validation in Go Common.
- Added task-credential-broker support for verified SPIFFE peers on an optional
  private mTLS listener. The public API-key route remains available. Exact peer
  IDs map to existing policy principals, and task proofs are bound to that same
  principal on lease and task-close operations.
- Added a default-off Fleet Runner Compose opt-in that mounts only the agent
  Workload API socket and adds a deterministic Docker attestation label.
- Added pinned SPIRE Server and Agent images plus a fully isolated, bounded
  staging stack on VM 102. Two exact Docker-label registrations enrolled the
  broker and canary under `spiffe://staging.0exec.com`.
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
4. The first immutable policy image gave `/etc/credential-broker` mode `0444`
   as a side effect of `COPY --chmod=0444`; the non-root process could read the
   file only after it could traverse the directory. The image now stages the
   policy directory separately and keeps the final directory traversable and
   root-owned, with policy files read-only. An exported image filesystem
   verified the final directory and file modes before the broker was restarted.
5. The broker constructor rejected an explicitly empty provider-issuer map,
   even when the default-deny staging policy contained no lease rules. Go
   Common v0.102.21 now accepts a non-nil empty map while rejecting grants
   without a configured issuer; its regression test proves the failure remains
   closed. Broker v0.1.16 then started healthy with this policy.
6. The first mTLS request reached the broker, but task creation returned 401
   while `/health` and `/version` returned 200. The handler expected Go's
   `VerifiedChains`, which SPIRE's `VerifyPeerCertificate` callback does not
   populate. Broker v0.1.17 now carries the authenticated peer from the
   dedicated go-spiffe mTLS listener into request context; a client-certificate
   request without that listener marker still fails closed, while the legacy
   API-key path remains available when no client certificate is present.
7. The initial SPIRE image needed an explicit binary entrypoint, and the
   non-root Server needed its private API socket inside its writable data
   directory rather than `/tmp`. The first join-token attempt also supplied
   `-spiffeID` in the reserved agent namespace. These were corrected in Fleet
   State PRs #49-#52; the runbook now extracts the token value from SPIRE's JSON
   output without printing it and applies registration JSON through stdin.

### Staging evidence and remaining gate

The staging project ran on Proxmox VM 102 (`10.50.0.12`) on a Docker bridge
marked `internal: true`. The four configured services had no published host
ports. The broker and canary mounted only `/run/spire/sockets` read-only; only
the Agent mounted the Docker socket. SPIRE Server, Agent, and broker each
reported healthy. The broker image was `0.1.17`, commit
`6ee7b924c5fd258ed281f171bb37ce7eaebbf60c`, digest
`sha256:76123d59f10750d7fa5b7298a4814d3351ae4c148b18c6a83e5d0581b5b01602`.
The Server and Agent images were digest-pinned to
`sha256:3aa2dce70fc1098d6718a87c629aa8cadb83e4672c71dd63c5575ea5d0603789`
and `sha256:0d9c792d7f409b748d3a1fc93fe70b6d1450474c177d2a731ddc36e97a17e9fe`,
respectively.

The maintained Go Common SPIFFE integration canary exited 0 twice: once after
the first successful task path and again after restarting the SPIRE Agent. It
created and closed a broker task over SPIFFE mTLS without an API key, then
rejected an unapproved broker server identity. A temporary status-only probe
also observed `/health` 200, `/version` 200, task create 201, and task close
204. The staging policy had no lease rules and no provider issuers, so no
credential lease was possible or requested. The first policy-permission,
deny-all-configuration, and peer-context failures were retained in this
post-mortem and fixed before declaring the staging canary passed.

This was a bounded integration pass, not the full rollout gate in ADR-0041.
The canary did not exercise scheduled X.509-SVID rotation, a lease acquire/
revoke path, or API-key fallback against the live staging listener. Agent
restart recovery, task creation/close, and rejection of an unapproved broker
identity are the behaviors actually verified.

No production service, production key, database, gateway, or production
container was changed. The broker's last fleet graph snapshot reported zero
observed callers in its earlier seven-day window. A fresh read-only Fleet
Runner discovery at `2026-10-06T15:34Z`, using the canonical service registry,
found zero graph callers and zero declared `depends_on` consumers. A source
scan found one `go_apikey_service` match, but inspection of current `origin/main`
showed that match only in `lease_test.go` using an `httptest` server. This
targeted source scan found no production call site; dynamically configured or
differently named callers are not ruled out by that scan. There is not yet a
verified production caller to migrate, and this run made no production
enrollments.

A second read-only Fleet Runner source scan for `credentiallease`,
`SPIFFE_ENDPOINT_SOCKET`, and `spiffe://` returned zero matches among eligible
Go container services, with no missing workspaces. This signature-based scan
does not cover non-Go workloads or runtime-only/dynamically configured
clients. Combined with the graph and registry results, no production migration
target was verified.

The previous capacity-reader blocker is resolved: `fleet-runner capacity
-json` succeeded against fleet Prometheus at `2026-10-06T15:34:31Z`. All three
reported hosts were up; the report recommended `0mcp-runtime-general`, while
`dockerhost` carried a load-per-CPU warning. This capacity snapshot does not
establish control-plane durability, a private path from the selected runtime
to each workload, or recovery readiness. VM 102 also lacks a recorded VM
snapshot and had a blocked cross-host TCP path, so it is not approved as the
production control plane. Production migration remains gated on identifying a
real production caller, an approved Server/Agent placement with backup and
recovery, complete host/runtime and private-path inventory, the remaining
staging identity/key-path checks, and then a separately reviewed production
canary. The central Compose adapter remains default-off; Coolify, external,
and static entries were not enrolled.

The full Fleet Runner suite has two pre-existing environment-sensitive test
failures on this workstation: tests requiring a root-owned private credential
file and a safe state-directory path fail under the local user. Both failures
were reproduced unchanged against fresh `origin/main`; the SPIFFE-specific
renderer tests pass.
