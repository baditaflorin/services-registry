# ADR-0023 — Five deploy-pipeline gaps found bootstrapping 20 new primitives

* **Status**: Accepted
* **Date**: 2026-05-17
* **Authors**: claude-session-batch5-2026-05-17
* **Tags**: deploy, fleet-runner, nginx, secrets, dockerfile, bootstrap

## Context

Phase 1 of the post-batches-1-4 follow-up was a sequential `fleet-runner deploy <slug> --bootstrap` for the 20 new go-fleet-* primitives registered in `services.json` (ports 18153-18172, ADRs 0003-0022). All 20 deploys eventually went green externally, but the run surfaced five distinct gaps in the bootstrap pipeline that each cost a fix-forward cycle. Capturing them here so the next batch (or the next fresh agent) recognises them on sight.

## Decision

Catalogue the five gaps. Each is small in isolation; together they delete ~30 min of agent time per bootstrap batch. Track each as a follow-up issue against `go_fleet_runner` (private) or `0crawl-platform` (private) as noted.

### Gap 1 — `HETZNER_TOKEN` not persisted on Builder LXC 108

`fleet-runner deploy ... --bootstrap` calls the Hetzner Cloud API to provision the A record. The fleet-runner-shim forwards argv over SSH but doesn't forward env. The laptop has `HCLOUD_TOKEN` (canonical name per CLAUDE.md); LXC 108 has neither `HCLOUD_TOKEN` nor `HETZNER_TOKEN` set. First-time deploys abort with `! DNS: no A record ... and $HETZNER_TOKEN not set`.

**Workaround used this session**: bypass the shim, SSH directly with the token inlined per-call —
```bash
ssh $FLEET_BASTION "pct exec 108 -- env HETZNER_TOKEN=$HCLOUD_TOKEN /usr/local/bin/fleet-runner deploy <slug> --bootstrap"
```
The token leaks to `ps` on the bastion + LXC for the deploy duration; acceptable for short interactive sessions, not for unattended automation.

**Proper fix** (follow-up): `fleet-runner deploy` should fetch the Hetzner token from `go-fleet-secrets` at deploy time using the keystore-backed `apikey.Cache` path. The secret is already canonically named `hcloud_token` in the vault per RUNBOOK-UNATTENDED.md §"Even shorter"; fleet-runner needs to read it instead of relying on env.

### Gap 2 — Per-service admin tokens not vault-integrated

`go-fleet-budget-tracker` (port 18164) and `go-fleet-schema-validator` (port 18166) require `BUDGET_ADMIN_TOKEN` / `SCHEMA_ADMIN_TOKEN` env vars at boot — the binary refuses to start without them. Their compose files pass these through from the dockerhost shell env (the `${VAR:?msg required}` pattern); fleet-runner's compose-pull step fails with `required variable X is missing a value` because the dockerhost doesn't have them.

**Workaround used this session**: generate a random 32-byte hex token per service and write it to `/opt/services/<slug>/.env` on the dockerhost (which docker-compose auto-loads):
```bash
ssh -J $FLEET_BASTION $DOCKERHOST 'echo "BUDGET_ADMIN_TOKEN=$(openssl rand -hex 32)" | sudo tee /opt/services/go-fleet-budget-tracker/.env && sudo chmod 600 /opt/services/go-fleet-budget-tracker/.env'
```

This violates the canonical convention in CLAUDE.md ("Secrets: live in `go-fleet-secrets`, NEVER in env on dockerhost"). The .env files are also out-of-band — they're not reproducible from the registry, and rotating the token requires manual edit + container restart.

**Proper fix** (follow-up): `fleet-runner deploy` should, before `compose pull`, render a `.env` next to the compose file from `go-fleet-secrets` based on a `service.yaml` `secrets:` block listing the env vars to populate. Service.yaml gains a contract like:
```yaml
secrets:
  - env: BUDGET_ADMIN_TOKEN
    vault_key: budget-tracker-admin-token   # or auto-generate-if-missing
```
Compose continues to use `${BUDGET_ADMIN_TOKEN}` and fleet-secrets becomes the single source of truth.

### Gap 3 — Named volumes initialise root-owned regardless of image user

A Docker named volume mounted into a path that doesn't exist in the image is created **root:root**, not as the image's `USER`. Services running as a non-root user (every fleet primitive that follows the convention) then can't open files on the volume. Symptom: SQLite open returns `SQLITE_CANTOPEN` (error 14), which `modernc.org/sqlite` reports as `unable to open database file: out of memory` — confusing because "out of memory" is a misleading error string.

Failed this batch: `go-fleet-call-tracer` (distroless `nonroot`), `go-fleet-vendor-disclosure-tracker` (alpine `app`). Other stateful primitives (`fingerprint-cache`, `budget-tracker`, `schema-validator`, `target-reputation`) already had the pattern.

**Canonical Dockerfile fix** for the volume mount path (write into the SERVICE-TEMPLATE):
```dockerfile
# Alpine + non-root:
RUN mkdir -p /data && chown -R app:app /data
VOLUME ["/data"]
USER app

# Distroless static + nonroot:
RUN mkdir -p /out/data                      # in the builder stage
COPY --from=build --chown=nonroot:nonroot /out/data /data
VOLUME ["/data"]
USER nonroot:nonroot
```

Add to SERVICE-TEMPLATE.md and write a `fleet-runner audit volume-ownership` check that greps every Dockerfile for `VOLUME` + non-root `USER` and verifies the path is pre-created with the right ownership.

### Gap 4 — `scope: internal-only` not honoured by `fleet-runner nginx-render`

`go-fleet-sandbox-targets` (ADR-0018) ships deliberately-vulnerable endpoints and is marked `scope: internal-only` in its service.yaml. The current `fleet-runner deploy` pipeline renders the standard public-facing nginx vhost for it regardless. ADR-0018 §Mitigations already flagged this as a follow-up to `0crawl-platform`.

**Workaround used this session**: hand-injected an allow/deny block into the gateway's `/etc/nginx/sites-enabled/go-fleet-sandbox-targets.0exec.com.https.conf` directly:
```
allow 127.0.0.1;
allow <internal-mesh-cidr>;
allow <dockerhost-egress-ip>;
deny  all;
```
HTTP vhost (port 80) intentionally left open so Let's Encrypt HTTP-01 renewals keep working. Verified externally: 403; internally (from dockerhost): 200.

**Fragility**: the next `fleet-runner nginx-render` push overwrites the gated file with the standard template.

**Proper fix** (follow-up to `0crawl-platform`): the vhost template gains a `{{if eq .Scope "internal-only"}}` conditional that emits the allow/deny block + .well-known exemption. `fleet-runner nginx-render` already has access to `service.yaml` fields; just pass `Scope` through.

### Gap 5 — `sites-enabled` holds regular files, not symlinks to `sites-available`

When you hand-edit a vhost on the gateway, the canonical Debian/nginx pattern is to edit the file in `sites-available/` (because `sites-enabled/` is a symlink). The fleet-runner deploy pipeline pushes the rendered vhost **directly to `sites-enabled/` as a regular file**, breaking that assumption. Editing `sites-available/` does nothing; nginx never reads it.

Burned ~10 min of agent time this session before the symptom (allow/deny not firing) pointed at the right file. There is no documentation surface that mentions this — both CLAUDE.md and RUNBOOK-UNATTENDED.md describe nginx as "the public TLS terminator" without specifying the file layout.

**Proper fix** (documentation + tooling):
- Add a note to CLAUDE.md §"Infrastructure topology" → Webgateway: "vhosts live as REGULAR FILES in `/etc/nginx/sites-enabled/<host>.{http,https}.conf`. `sites-available/` is unused. Edit sites-enabled directly, or render via `fleet-runner nginx-render`."
- Consider: `fleet-runner nginx-render` could write to `sites-available/` and symlink, to match the convention — but that's a larger change. Documentation alone is enough to unblock future agents.

## Consequences

**Positive**: documenting these gaps in one ADR means the next agent recognises them in <60s instead of <60min per gap.

**Negative**: this ADR is descriptive (catalogue of follow-ups), not architectural. It will look stale once the follow-up issues are filed and resolved — at that point, supersede it with a tightened CLAUDE.md / SERVICE-TEMPLATE.md.

**Mitigations**: every gap above has a "Workaround used this session" line and a "Proper fix" line. Future agents can run the workarounds verbatim if the proper fix hasn't landed yet.

## Migration path

Nothing for consumer services — this is a deploy-pipeline / template ADR. The five follow-up tasks live in `go_fleet_runner` (gaps 1, 2, 3-audit), `0crawl-platform` (gap 4), and CLAUDE.md / SERVICE-TEMPLATE.md (gap 5 + the volume-ownership convention from gap 3).

## Alternatives considered

- **Five separate ADRs (0023-0027)**: each gap is small enough that a per-gap ADR would mostly be padding. Consolidating reflects how they actually surfaced (one bootstrap batch, one fix-forward loop).
- **Inline-fix in CLAUDE.md, no ADR**: loses the historical trail. ADR-0023 lets the next agent grep `docs/adr/` for "bootstrap" and find this whole class of issue in one place.
- **Spawn follow-up issues immediately**: blocked on this session's Phase 2 + Phase 3 still being in flight. The follow-ups should be filed once those land so the issue tracker doesn't fragment a single in-progress thread.

## References

- ADR-0002 — twenty fleet primitives (the batch this Phase 1 was bootstrapping)
- ADR-0018 — fleet-sandbox-targets (the `scope: internal-only` source)
- `services-registry/CLAUDE.md` §"Service conventions", §"Auth", §"Infrastructure topology"
- `services-registry/RUNBOOK-UNATTENDED.md` §"Secrets bootstrap"
- `fleet-state/OPS.md` §"Hetzner Cloud DNS token"
- Per-fix commits:
  - `go-fleet-body-redactor` — service.yaml YAML-quoting fix
  - `go-fleet-vendor-disclosure-tracker` — service.yaml flow-mapping fix + Dockerfile /data ownership
  - `go-fleet-call-tracer` — Dockerfile distroless /data ownership
  - `go-fleet-engagement-timeline` — branch rename master → main
