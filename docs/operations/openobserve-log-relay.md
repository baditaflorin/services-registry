# OpenObserve read-only log relay (`bin/oo-relay`)

Any agent can read historical container logs from the fleet OpenObserve (VM 630
on 0own, stream `docker_logs`) without holding the OpenObserve root password.
OpenObserve OSS has no read-only role, so the password stays on VM 630 and a
small relay holds it instead. This is the generic version of the fixed-query
CrUX relay (`go-url-categorizer-api`, `docs/operations/crux-openobserve-readonly-relay.md`);
the code, its tests and the query rules are in `bin/oo-relay/README.md`.

Status: code merged-ready in a PR; **not deployed**. Deploying needs the
founder (VM 630 access, a vault secret, a tailnet ACL). The steps are below.

## What agents get

- One call: log lines of one container (exact `container_name`, optional exact
  `host`, optional case-sensitive substring `contains`) between `start` and
  `end`, at most 7 days and 500 lines, oldest first or newest first.
- Only `_timestamp`, `container_name`, `host`, `message` come back; messages
  are masked for known credential shapes and cut at 8 KB.
- No SQL, no other stream, no organization choice, no write endpoint.
- Failures are explicit (`401`, `400`, `429`, `502 unavailable`). A relay or
  OpenObserve problem is never reported as "zero matching lines".

## Using it from an agent

`bin/oo` has a relay mode, selected by `OO_RELAY_URL`. It serves `logs`,
`grep --container`, `since-redeploy` and `context`; the commands that need SQL
or a fleet-wide search (`query`, `errors`, `containers`, `hosts`, `summary`, ...)
refuse with exit 2 in relay mode.

Two ways to carry the token, never on a command line or in an environment
variable of another process:

```sh
# A. Token never leaves the builder: the request is sent from Builder CT 108
#    through the bastion you already use for fleet-runner.
export OO_RELAY_URL=http://100.90.206.116:18084
export OO_RELAY_VIA=bastion          # reads /root/.config/oo-relay/token on CT 108

# B. Direct from a tailnet machine: the token is in a private file (mode 600).
export OO_RELAY_URL=http://100.90.206.116:18084
export OO_RELAY_TOKEN_FILE=$HOME/.config/oo-relay/token
```

Acceptance query (DomainScope prod, the Extended traits backfill run of
2026-11-03; the previous `domainscope-api` container is gone, its lines survive
in OpenObserve):

```sh
bin/oo logs domainscope-api --host 0docker_docker_prod \
  --from 2026-11-03T12:00:00Z --to 2026-11-03T18:30:00Z \
  --grep '[ext-traits-backfill]' --limit 500
```

With `--from`, lines come oldest first, so the start of the run is at the top.
If the relay prints `note: the relay stopped at its row limit`, run the same
command again with `--from` set to the printed `next_start` (that exact
microsecond repeats once). Without `--from`, `--since` counts back from now,
newest lines first; the relay allows a window of at most 7 days.

## Deploy runbook (founder go-ahead needed for every step on VM 630)

Do not start before the founder says so. Nothing here changes OpenObserve
itself, its data or the existing CrUX relay.

### 0. What the founder has to do or approve

| Step | Why only the founder |
|---|---|
| Approve the deploy window on VM 630 | it is the host of OpenObserve and holds its root password |
| Create the vault secret `oo-log-relay-token` (value: 32 random bytes, 64 hex characters) | creating a secret is outside what an agent may do alone |
| Add the vault consumer allowlist for the agent roles (see 3) | consumer allowlists are vault policy |
| Tailnet ACL: allow the agent machines to `100.90.206.116/32:tcp/18084` | tailnet policy is the founder's; the CrUX relay got its grant the same way (`proxmox_florin_server#553`, with a policy test) |
| Confirm port `18084` is free on VM 630 (`ss -ltn` there) | it is chosen next to the CrUX relay's 18083, not allocated by the registry (0own is outside `allocate-port`) |

### 1. Build the image (builder, no VM 630 access needed)

Build on an approved AMD64 builder, as for the CrUX relay, and load it onto
VM 630 so VM 630 needs no registry login:

```sh
cd bin/oo-relay
docker buildx build --platform linux/amd64 --provenance=false -t oo-relay:<git-short-sha> --load .
docker save oo-relay:<git-short-sha> | ssh <vm630> docker load
```

### 2. Token and password files on VM 630 (root, once)

```sh
install -d -m 0755 /opt/oo-relay
cd /opt/openobserve/secrets
# Token(s): one per line, 64 hex each. The value comes from the vault secret,
# never from the shell history: read it through the approved vault path.
umask 077
# (write oo_log_relay_tokens from the vault value; do not echo or paste it)
chgrp 65532 oo_log_relay_tokens
chmod 0640 oo_log_relay_tokens
# The root password file already exists. The CrUX relay already gave it
# group 65532 and mode 0640; check, do not repeat blindly:
ls -ln openobserve_root_password oo_log_relay_tokens   # expect: root 65532, -rw-r-----
```

Group 65532 has no host account and mode 0640 gives nothing to other users
(same reasoning as the CrUX relay runbook). Several lines in the token file are
allowed so a rotation can overlap: add the new token, move callers, remove the
old line, restart the relay.

### 3. Token distribution to agents

The token is a fleet secret like the Scanopy Viewer key
(`scanopy-inventory-reader-api-key`: held in `go-fleet-secrets`, allowlisted to
named consumers, fetched into memory by the tool that needs it):

1. The founder creates vault secret `oo-log-relay-token`.
2. The consumer allowlist names the agent roles that may read it. Which role
   names exist is vault policy (`audit fleet-auth-consumers` cross-checks a
   service's declared `auth.reads_secrets` against a secret's consumer list);
   this repository does not hold that list, so the founder or the vault owner
   adds the roles.
3. Put the token where the agents' tooling reads it, without it ever being
   printed:
   - for `OO_RELAY_VIA=bastion`: on CT 108 as `/root/.config/oo-relay/token`
     (mode 0600, root), written from the vault value on the builder;
   - for direct use from a tailnet machine: a mode-0600 file named by
     `OO_RELAY_TOKEN_FILE`, written from the vault value by the operator.

   Agents need no password and no token in prompts, env files or repos.

### 4. Start the relay (VM 630)

```sh
cd /opt/oo-relay
cp <repo>/bin/oo-relay/compose.yaml .
echo 'OO_RELAY_IMAGE=oo-relay:<git-short-sha>' > .env
docker compose up -d
docker compose logs --tail=20 oo-relay      # expect: "listening on :8080 (N token(s))"
```

The container runs as `65532:65532`, read-only root filesystem, all
capabilities dropped, 128 MB, 0.5 CPU, bound to the tailnet address
`100.90.206.116:18084` only. If it exits with `permission denied`, the group
step in 2 was skipped (fail closed).

### 5. Verify

```sh
# from a machine allowed by the ACL; the token is read from the file, not typed
curl -fsS http://100.90.206.116:18084/healthz
OO_RELAY_URL=http://100.90.206.116:18084 OO_RELAY_TOKEN_FILE=<file> \
  bin/oo logs domainscope-api --host 0docker_docker_prod --since 2d --grep '[ext-traits-backfill]' --limit 500
```

Also check that a request without the token returns `401` and that a body with
an unknown field (for example `"sql"`) returns `400`.

### Rollback

`docker compose down` in `/opt/oo-relay` removes only this relay. OpenObserve,
its data and the CrUX relay are untouched. Revoke by deleting the vault secret
and the token file; to rotate, replace the line in the token file and restart.

## Limits and residual risk

- The relay mounts the OpenObserve root password (OSS offers no lesser
  credential), so VM 630 compromise is still the root-password risk. The
  mitigations are the narrow interface, the tailnet-only bind, the separate
  token, and a non-root, read-only, capability-free container. Revisit if
  OpenObserve moves to an edition with a Viewer role.
- One token covers all agents; per-agent attribution is the caller address in
  the relay log, not an identity. Rotate if it leaks.
- Log lines are stored as written. Masking known credential shapes on the way out
  is a second line of defence, not a guarantee.
- `contains` is matched before masking, so a caller can confirm that a string
  occurs in the logs. This is within the read access the token grants.
- The relay serves only hosts that ship to `docker_logs` (see
  `docs/observability/CENTRAL-OPENOBSERVE.md`). Missing rows are a collection
  gap, not a clean run.
