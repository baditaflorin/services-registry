# ADR-0027 — Fleet authentication canonical flow

* **Status**: Accepted
* **Date**: 2026-05-18
* **Authors**: fleet-agent, baditaflorin
* **Tags**: auth, keystore, vault, fleet-runner, safehttp

## Context

The 2026-05-17 bootstrap of `go-pentest-leak-bounty-policy` failed because
`go-fleet-dns-sync`'s reconcile ticker had silently logged 0 syncs for
>24h. `/health` was green while every outbound vault read returned 403
"not in consumers list" because `FLEET_API_KEY` defaulted to the public
demo `default_token`, and `actor=demo` isn't on the consumers allowlist
for any production secret. The operator manually POSTed to Hetzner's
Cloud Zones API to unblock and back-filled this ADR + the tooling and
guards below.

Adjacent failures uncovered during the cleanup pass:

* **Wrong default `SecretsURL`** — `go-fleet-dns-sync` and
  `go-fleet-preflight` defaulted the secrets URL to the internal docker
  hostname `http://go-fleet-secrets:18140`, which skips the gateway. The
  gateway is what does the `X-API-Key → keystore /verify → X-Auth-User`
  translation; bypassing it means `go-fleet-secrets` sees no
  `X-Auth-User` header and 401s every read, no matter how valid the key.
* **safehttp blocks the gateway hop** — the gateway resolves to
  `10.10.10.10` (RFC1918). go-common/safehttp's SSRF defense refuses
  RFC1918 by default, so the corrected `SecretsURL` was still blocked
  until `SAFEHTTP_ALLOW_PRIVATE_IPS=10.10.10.10` was set in compose.
* **Demo-token defaults at startup** — multiple services defaulted
  outbound auth keys (and one admin token) to the literal
  `"default_token"`. The Go binary accepted the value at boot, the
  ticker failed at run time, no log surfaced because the http client
  just got a 403 it didn't know how to interpret.

This ADR captures the canonical end-to-end auth flow for any service
that calls another fleet service, plus the tooling and guards that
enforce it.

## Decision

### The canonical flow

```
caller service
    └── outbound HTTPS via safehttp.Client (SSRF-defended)
        └── https://<callee>.0exec.com  (gateway URL — RFC1918, 10.10.10.10)
            └── nginx auth_request → http://go-apikey-service/verify  (keystore lookup)
                └── 200 + X-Auth-User=<caller-slug>, X-Auth-Scope=<scope>
            └── nginx forwards to callee container with X-Auth-User header injected
                └── callee's go-common/server keystore middleware:
                    if X-Auth-User present → pass through (gateway already vouched)
                    else → reject with 401
                └── handler runs; consumer-list checks (in go-fleet-secrets etc.)
                    are gated on X-Auth-User
```

Five primitives the flow depends on, each owned by a specific surface:

| Primitive | Where it lives | What it does |
|---|---|---|
| **Service principal** | `service.yaml` `id:` + `auth:` blocks | Identity + declared scope of fleet calls. |
| **Keystore-issued key** | `go-apikey-service` SQLite at `/data/keys.db` | Maps `ak_*`/`fb_*` value → `(user, scope, exp)`. |
| **`FLEET_API_KEY` in container** | `/opt/services/<slug>/.env` (mode 600) on the dockerhost | The actual key, refused-empty/refused-default_token at startup. |
| **`go-common/apikey.MustResolveCritical`** | Service `main.go` | Fail-fast at boot if `FLEET_API_KEY` is empty, `default_token`, or has unknown prefix. |
| **Gateway keystore middleware** | nginx + `go-common/server.WithKeystoreAuth` | Translates `X-API-Key` query/header into `X-Auth-User` for downstream services. |

### The canonical bootstrap

Use `fleet-runner key provision <slug>` — it is the **only** correct way
to wire a new service into the fleet-auth flow. The verb is atomic over
three steps that previously had to be sequenced by hand:

```
fleet-runner key provision go-fleet-dns-sync
  ├── apikey.Client.Issue(user=go-fleet-dns-sync, scope=…, never_expires=true)
  │       returns ak_…
  ├── ssh → dockerhost → idempotent sed-or-append on
  │   /opt/services/go-fleet-dns-sync/.env  (mode 600, FLEET_API_KEY=ak_…)
  └── ssh → dockerhost → docker compose up -d   (container recreates with new env)
```

Rerunning the verb with the same slug **rotates the key in place** rather
than appending a shadowed second line. The verb prints the new key to
stdout and a redacted summary to stderr, so
`KEY=$(fleet-runner key provision foo)` works.

Required env on the caller side:

```
APIKEY_SERVICE_URL=http://10.10.10.20:18021   # dockerhost host_port of go-apikey-service
APIKEY_SERVICE_ADMIN_TOKEN=<value from fleet-state/OPS.md>
FLEET_BASTION=root@0docker.com                # default
FLEET_DOCKERHOST=ubuntu_vm@10.10.10.20        # default
```

### service.yaml `auth:` block (declared scope)

Every service that participates in fleet auth declares its scope:

```yaml
auth:
  # Secrets this service reads from go-fleet-secrets. Cross-checked
  # against the secret's consumers list by `audit fleet-auth-scope`.
  reads_secrets:
    - hcloud_token
  # Fleet siblings called via gateway URL.
  calls_services:
    - go-fleet-secrets
    - go-fleet-resolver-quorum
  # External domains (informational; useful for egress policy review).
  calls_external:
    - api.hetzner.cloud
```

A service that polls fleet-wide (e.g. `go-fleet-selftest-aggregator`)
sets `calls_services: ["*"]` — the audit verb skips per-edge checks for
wildcard scopes.

### Required compose pattern

```yaml
environment:
  # Refuse-empty guard. Containers fail at compose-interpolation time
  # if FLEET_API_KEY isn't set in /opt/services/<slug>/.env, so an
  # accidental rollback to "no .env" can't silently demote to
  # default_token.
  - FLEET_API_KEY=${FLEET_API_KEY:?FLEET_API_KEY must be a keystore-issued key for principal=<slug>; demo default_token rejected}

  # Public-fleet gateway URL for go-fleet-secrets. Do NOT use the
  # docker-internal hostname http://go-fleet-secrets:18140 — that
  # skips the keystore-verify middleware and reads always 401.
  - SECRETS_URL=${SECRETS_URL:-https://go-fleet-secrets.0exec.com}

  # The gateway is on RFC1918 (10.10.10.10). safehttp's SSRF block
  # would refuse the hop without this allowlist entry. Keep the
  # entry narrowed to just the gateway IP — don't widen it.
  - SAFEHTTP_ALLOW_PRIVATE_IPS=${SAFEHTTP_ALLOW_PRIVATE_IPS:-10.10.10.10}
```

### Required main.go guard

```go
import "github.com/baditaflorin/go-common/apikey"

// At Reconciler / Handler construction:
FleetAPIKey: apikey.MustResolveCritical("<slug>", "FLEET_API_KEY"),
```

`MustResolveCritical` fatal-exits with a regex-greppable log line if
`FLEET_API_KEY` is empty, `default_token`, or has an unknown prefix:

```
apikey.critical_key_missing slug=<slug> env=FLEET_API_KEY reason=<reason> fix=`fleet-runner key provision <slug>` docs=https://github.com/baditaflorin/services-registry/blob/main/RUNBOOK-UNATTENDED.md#service-principals
```

For admin tokens (write-endpoint gates, not outbound auth), use the
per-repo inline helper pattern shown in `go-fleet-priority-queue/main.go`
(`mustResolveAdminToken`). Refuses unset and `default_token`. Extract
to go-common once there are 3+ callers.

### Audit at rest

```
fleet-runner audit fleet-auth-scope [--json] [--severity warn|fail]
```

Shape-only survey of `FLEET_API_KEY` across every running container on
the dockerhost. Flags:

* `default_token` — **fail**. Container will silently 401 against vault.
* `unset` — **warn**. Only matters if the service does outbound auth.
* `unknown_prefix` — **warn**. Likely a misconfigured paste.
* `ak_*` / `fb_*` — info (suppressed from the table).

Exits non-zero when the worst observed severity meets or exceeds
`--severity` (default `warn`). Wire it into CI / pre-deploy gates.

What this audit does **not** yet do — and what `audit fleet-auth-scope`
v2 should add:

* Cross-check `auth.reads_secrets` against the consumers list on
  go-fleet-secrets per declared secret.
* Cross-check `auth.calls_services` against the observed call graph
  from go-fleet-graph.
* Verify a service's principal is reachable via the gateway with its
  current key (live round-trip).

## Consequences

**Positive**

* Bootstrap a new service in one verb (`fleet-runner key provision`)
  instead of a three-step ceremony that historically lost steps.
* Drift at rest is detectable (`audit fleet-auth-scope`). No more "key
  silently wrong for 24h, ticker logged 0 syncs."
* Demo `default_token` cannot reach production code paths: the compose
  `:?` guard refuses interpolation, `MustResolveCritical` refuses
  startup, and the audit verb flags it post-hoc if both somehow miss.

**Negative**

* `FLEET_API_KEY` rotation requires a container recreate. Compose's
  default behaviour is to recreate when env differs, so re-running
  `fleet-runner key provision` is enough — but services with long-lived
  in-memory state (e.g. an open reconcile transaction) take an outage
  during the swap. Acceptable trade-off; fleet services are designed
  stateless to keep these costs low.

* The flow assumes the gateway is reachable. If nginx is down, every
  fleet-internal HTTPS call fails. The keystore /verify endpoint has
  a 15-minute per-service cache in `go-common/apikey.Cache` to absorb
  brief gateway outages, but a >15-min nginx outage takes the fleet
  down hard. This is a deliberate point of consolidation, not a bug.

**Mitigations**

* `go-fleet-selftest-aggregator` polls every service's `/selftest`
  hourly. With `/selftest?live=1` opt-in (added 2026-05-18), the
  aggregator catches "vault scope broken" within an hour instead of
  next-deploy.

* `fleet-runner deploy`'s smoke gate already runs `/selftest` post-roll.
  Flipping its probe to `?live=1` (after enough services support it)
  collapses the detection latency to <30s — the deploy itself fails
  if vault auth is broken, not a downstream batch hours later.

## Migration path

For each existing service that does outbound calls to a fleet sibling:

1. **Add `auth:` to `service.yaml`** — at minimum `reads_secrets` and
   `calls_services` (use `["*"]` for fleet-wide pollers). Commit + push.

2. **Add `MustResolveCritical` to `main.go`**:
   ```go
   FleetAPIKey: apikey.MustResolveCritical("<slug>", "FLEET_API_KEY"),
   ```

3. **Update `docker-compose.yml`** to:
   * `FLEET_API_KEY=${FLEET_API_KEY:?<msg>}`
   * `SECRETS_URL=${SECRETS_URL:-https://go-fleet-secrets.0exec.com}` (if reads vault)
   * `SAFEHTTP_ALLOW_PRIVATE_IPS=${SAFEHTTP_ALLOW_PRIVATE_IPS:-10.10.10.10}` (if reads vault)

4. **Provision the key**:
   ```
   APIKEY_SERVICE_URL=http://10.10.10.20:18021 \
   APIKEY_SERVICE_ADMIN_TOKEN=<from OPS.md> \
   fleet-runner key provision <slug>
   ```

5. **Add `<slug>` to the consumers list** of each secret in
   `auth.reads_secrets`:
   ```
   curl -X PATCH -H "X-Admin-Token: <SECRETS_ADMIN_TOKEN>" \
        -H "X-Auth-User: ops" \
        -d '{"consumers": ["existing-1", "existing-2", "<slug>"]}' \
        https://go-fleet-secrets.0exec.com/secrets/<secret-name>
   ```

6. **Verify**:
   ```
   fleet-runner audit fleet-auth-scope     # no fail rows for <slug>
   curl https://<slug>.0exec.com/selftest?live=1
   ```

## Alternatives considered

**Per-service ADMIN_TOKEN as the outbound auth**

Rejected: ADMIN_TOKEN gates write endpoints on the service ITSELF; it
isn't an identity for outbound calls. Reusing it conflates two distinct
concerns and breaks the X-Auth-User translation at the gateway.

**Internal docker hostname for go-fleet-secrets**

Rejected: skips the gateway's keystore /verify, so X-Auth-User never
gets injected, and consumer-list checks always 401. We tried this twice
(it was the silent default for >6 months); both times the failure was
invisible until a downstream batch broke.

**Issuing one shared key per fleet**

Rejected: defeats the consumers-list model. Every secret would need
the shared user on its allowlist, which collapses scoping back to
"anyone can read anything". The per-service-principal model is the
whole point.

**Storing FLEET_API_KEY in go-fleet-secrets itself**

Considered for ADR-0023 gap 2; rejected for the keystore's own key
(circular dependency: secrets need keystore-issued keys to read, but
the key to read secrets-itself can't itself live in secrets). Tokens
that are genuinely opaque-admin (ADMIN_TOKEN, signing keys) live in
secrets and are materialized via `deploy_secrets.go` at deploy time.
The fleet-auth chain stays in `.env` because it bootstraps everything
else.

## References

* `go-common/apikey/critical.go` — MustResolveCritical impl
* `go-common/middleware/auth_keystore.go` — gateway-side middleware
* `go-common/safehttp/safehttp.go` — SSRF defense + `SAFEHTTP_ALLOW_PRIVATE_IPS`
* `go_fleet_runner/key_provision.go` — provision verb
* `go_fleet_runner/audit_fleet_auth_scope.go` — audit verb
* `go-fleet-dns-sync@c905197` — first service to adopt MustResolveCritical
* `go-fleet-dns-sync@f4e49da` — first service to set SECRETS_URL + SAFEHTTP_ALLOW_PRIVATE_IPS correctly
* ADR-0023 — pipeline gaps from phase-1 bootstrap
* ADR-0024 — primitive-to-primitive pattern (internal vs gateway)
* ADR-0025 — vault-integrated admin tokens (the OTHER class of token)
* 2026-05-17 incident — silent ticker, default_token + missing consumers
