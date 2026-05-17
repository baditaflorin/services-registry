# ADR-0025 — vault-integrated admin tokens at deploy time

* **Status**: Accepted
* **Date**: 2026-05-17
* **Authors**: claude (paired with @baditaflorin)
* **Tags**: secrets, deploy, fleet-runner, go-fleet-secrets

## Context

Two services in the fleet (`budget-tracker`, `schema-validator`)
require an admin token env var at boot. Pre-2026-05-18, those tokens
lived as plain `.env` files on the dockerhost (`/opt/services/<slug>/.env`),
hand-placed by an operator on the first deploy and never refreshed.
That violates `CLAUDE.md`'s "Secrets live in `go-fleet-secrets`,
NEVER in env on dockerhost" rule and is the last open item in
[ADR-0023](0023-deploy-pipeline-gaps-from-phase-1-bootstrap.md) gap 2.

The dockerhost `.env` files were:
- Stale relative to whatever was in the vault (no rotation path).
- Invisible to `fleet-runner audit` — no checks could even tell a
  service was misconfigured.
- Manually-managed: a fresh dockerhost bootstrap had no playbook,
  the deploy would silently boot with an unset env and produce
  confusing 401s at runtime.

The fleet already has [`go-fleet-secrets`](https://github.com/baditaflorin/go-fleet-secrets)
running on the dockerhost (port 18140) — the encrypted vault that
holds Hetzner/GitHub/SMTP tokens. The gap was purely on the
fleet-runner side: nothing fetched per-service secrets at deploy time.

## Decision

Per-repo `service.yaml` gains an optional `secrets:` block declaring
each env var the service needs at boot, and the vault key that backs
it:

```yaml
secrets:
  - env: BUDGET_ADMIN_TOKEN
    vault_key: budget-tracker-admin-token   # optional; defaults to
                                            # <slug>-<env-lowered-with-dashes>
    auto_generate_if_missing: true          # opt-in; for opaque tokens
                                            # the service mints on first boot
```

`fleet-runner deploy` adds a new step **before `docker compose pull`**
in both `rebuildAndRoll` (steady-state) and `bootstrapFirstTime`
(first deploy) paths:

1. Parse `secrets:` from `origin/main:service.yaml`. No block →
   no-op (vast majority of repos).
2. For each declared secret, `sshRun(bastion, dockerhost, ...)` a
   `curl` to `http://localhost:18140/secrets/<key>` with
   `X-Admin-Token: $FLEET_SECRETS_ADMIN_TOKEN`.
3. On 404 + `auto_generate_if_missing: true`: `crypto/rand` a fresh
   32-byte hex token, POST to the vault with `consumers: ["<slug>"]`,
   use the new value.
4. On 404 + no auto-generate, on any 5xx, on timeout, on missing
   admin token: **fail-CLOSED**. Aborts the deploy. Rolling forward
   with a stale or unset admin token is worse than rolling back.
5. Write `<composeDir>/.env` (mode 600, root:root) via
   `sudo install`. KEY=VALUE lines in sorted order for diffability.
6. The next `docker compose pull && up -d` picks up the .env via
   compose's default env_file resolution.

A new audit `fleet-runner audit vault-secret-coverage` lints
service.yaml declarations against vault state at-rest:
- pass: no secrets declared, or all declared secrets resolvable
- fail: a declared secret is missing AND doesn't opt into auto-generate
- skip: `FLEET_SECRETS_ADMIN_TOKEN` not exported in the audit env

The `fleet-runner-shim` now forwards `FLEET_SECRETS_ADMIN_TOKEN` from
the operator's local env to LXC 108 (joining the existing
`HCLOUD_TOKEN` / `GITHUB_TOKEN` / `APIKEY_SERVICE_ADMIN_TOKEN`
forwarding). So a deploy from a workstation is `export
FLEET_SECRETS_ADMIN_TOKEN=… && fleet-runner deploy <slug>` once.

## Consequences

**Positive**
- A deploy can no longer ship a service with a stale or missing admin
  token in silence — fail-CLOSED stops the roll before compose pull.
- Audit-able at-rest: `fleet-runner audit vault-secret-coverage`
  fleet-wide tells us every service whose secret declarations
  match (or don't) the vault inventory.
- Rotation playbook simplifies: rotate the value in the vault, run
  `fleet-runner deploy <slug>`, the new container picks up the
  refreshed env on its next recreate. No SSH-edit-the-.env step.
- The `.env` is owned root:root mode 600 on the dockerhost — same
  security posture as any other dockerhost secret, but now derived
  rather than human-placed.

**Negative**
- The `FLEET_SECRETS_ADMIN_TOKEN` briefly appears in argv of the
  bastion sshd process and the dockerhost curl process during the
  fetch. Acceptable trade-off — both are private hosts that already
  see plaintext tokens via other deploy paths. Documented in
  `deploy_secrets.go`'s top-of-file comment; a future hardening
  could pipe via `read -s` on a dockerhost wrapper script.
- The deploy now has a vault dependency. Vault outage → no deploys
  for services with `secrets:` blocks. This is intentional
  (fail-CLOSED) — see Mitigations.

**Mitigations**
- Services *without* a `secrets:` block (the vast majority) are
  unaffected — no new deploy path, no new dependency.
- The vault is itself a fleet service with its own `/health` and
  is monitored; outage → page → recover → resume deploys. The
  same is already true for any deploy that needs GHCR pull or
  bastion SSH.

## Migration path

For each service that currently has a hand-placed `.env` on the
dockerhost:

1. Inspect the env vars the service actually needs at boot
   (`docker exec <container> env | grep -v '^PATH\|^HOME\|^HOSTNAME'`).
2. Register each one in the vault:
   ```bash
   curl -sf -X POST -H "X-Admin-Token: $FLEET_SECRETS_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name":"budget-tracker-admin-token","value":"<existing-value>","consumers":["budget-tracker"]}' \
     http://localhost:18140/secrets
   ```
   (Run from the dockerhost. For one-off operator runs, the public
   URL via `https://go-fleet-secrets.0exec.com/secrets` also works
   with the same admin token.)
3. Add the `secrets:` block to `service.yaml`, commit, push.
4. `fleet-runner deploy <slug>` — fail-CLOSED if anything's missing.
5. Verify `/opt/services/<slug>/.env` was written by fleet-runner
   (not the original hand-placed file) by re-deploying with a fresh
   value rotated into the vault and observing the container picks
   it up on next roll.
6. Delete the original hand-placed `.env` (only once step 5 confirms
   the vault path works — the deploy will overwrite it anyway, but
   leaving stale state is sloppy).

Two services migrate in this commit: `budget-tracker`,
`schema-validator`. Both follow the same shape.

## Alternatives considered

**A) fleet-runner fetches secrets via the public gateway (HTTPS).**
Rejected: would require a per-service-tenant API key in the
keystore, and the gateway auth path already gates the vault from
public reach. The dockerhost-localhost path is shorter, doesn't add
gateway hops, and is consistent with ADR-0024's plain-net/http
primitive-to-primitive convention (the curl runs *inside* the
docker mesh's network, even though fleet-runner orchestrates it).

**B) Bake the admin token into the container image at build time.**
Rejected obviously — image layers are tar archives, anyone with
GHCR pull access can extract the token, and rotation requires a
rebuild.

**C) Use docker secrets / docker swarm secrets.** Rejected: we run
docker-compose, not swarm. A migration to swarm has not been on
the table.

**D) Have the service fetch its own admin token from the vault at
boot.** Considered. The downside is that *every* service would
need a vault client and an admin token of its own, which expands
the vault's blast radius (more clients = more compromise paths).
Centralizing the fetch in fleet-runner means only fleet-runner
holds the admin token. We may revisit this if individual services
ever need secrets that change between rolls (rotation faster than
deploy cadence) — for now, deploy-time materialization is right-
sized.

**E) `vault list` to also flag stale vault entries no service declares.**
Deferred until `go-fleet-secrets` exposes a list endpoint. Currently
the audit only catches *missing* registrations, not *unused* ones.

## References

- [ADR-0023](0023-deploy-pipeline-gaps-from-phase-1-bootstrap.md) —
  gap 2 was the open item this closes.
- [ADR-0024](0024-phase-3-consumer-migration-and-primitive-to-primitive-pattern.md) —
  plain-net/http convention for intra-mesh calls (which the dockerhost
  curl satisfies even though fleet-runner is the orchestrator).
- `go_fleet_runner/deploy_secrets.go` — implementation.
- `go_fleet_runner/audit_vault_secrets.go` — at-rest audit.
- `go-fleet-secrets` — the vault primitive (port 18140).
- `services-registry/CLAUDE.md` — "Secrets live in go-fleet-secrets,
  NEVER in env on dockerhost" rule.
