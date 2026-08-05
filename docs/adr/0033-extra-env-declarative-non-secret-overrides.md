# ADR-0033 — `extra_env:` for declarative, non-secret service.yaml overrides

* **Status**: Accepted
* **Date**: 2026-08-05
* **Authors**: claude (paired with @baditaflorin)
* **Tags**: deploy, fleet-runner, service.yaml, go-fleet-pipe, go-fleet-webhook

## Context

`go-fleet-pipe` and `go-fleet-webhook` both build the public URL they
hand back to callers (`pipe-push`'s read link, `webhook-new-bin`'s
capture link) from the incoming HTTP request: an explicit `BaseURL`
override wins if set, else `X-Forwarded-Host`/`X-Forwarded-Proto` when
present, else the raw request `Host`.

That derivation is correct when a caller reaches the service through
the real public reverse proxy (`pipe.0exec.com` / `webhook.0exec.com`),
which sets `X-Forwarded-Host` properly. It silently breaks when
`go-fleet-mcp-gateway` calls either service directly at its internal
dockerhost address (`10.10.10.20:18272` / `:18276`) to implement the
`fleet-pipe.pipe-push` / `fleet-webhook.webhook-new-bin` MCP tools —
no forwarded-host header is set, so the derivation falls through to
the raw `Host`, and the tool hands the caller a private-network URL.
For `pipe-read` that link is merely unopenable outside the fleet
network; for a webhook capture bin it's a functional bug — the entire
point of a bin is that a real third party (Stripe, GitHub, a CI
runner) can POST to it, and they cannot reach `10.10.10.20`.

Both services already ship the fix as an env var
(`PIPE_PUBLIC_BASE_URL` / `WEBHOOK_PUBLIC_BASE_URL`, wired into each
repo's `docker-compose.yml` via `${VAR:-}`), but neither var was ever
set on the dockerhost. The immediate remediation was a hand-edited
`/opt/services/<slug>/.env` on `10.10.10.20`, which
[`materializeServiceEnv`](../../../go_fleet_runner/deploy_secrets.go)
already preserves across redeploys (the same "operator-written
entries" mechanism that predates `secrets:` for `FLEET_API_KEY` — see
[ADR-0025](0025-vault-integrated-admin-tokens.md)). That's a real fix,
but it's invisible: nothing in either repo's git history says the
override exists or why, so the next dockerhost rebuild or a fresh
service bootstrap silently loses it, and no code review ever saw the
value get set.

The fleet already has a declarative, version-controlled path for
per-service env at deploy time — `secrets:`, vault-backed, built for
credentials (ADR-0025). There was no equivalent for values that are
**not secret** (a public base URL, a feature flag, a numeric tuning
knob) but still deserve to live in git next to the code that depends
on them, get reviewed in a PR, and survive a from-scratch dockerhost
bootstrap without an operator's memory being the only record.

## Decision

Per-repo `service.yaml` gains an optional `extra_env:` block: a flat
map of env var name to literal value.

```yaml
extra_env:
  PIPE_PUBLIC_BASE_URL: https://pipe.0exec.com
```

`materializeServiceEnv` (deploy_secrets.go) gains a new source between
vault secrets and shared proxy env, in this precedence order:

1. **Vault secrets** (`secrets:`, ADR-0025) — highest priority, per-
   service-scoped truth.
2. **`extra_env:`** (this ADR) — declared, non-secret, version-
   controlled config.
3. **Shared Webshare `proxy.env`** (`proxy_egress: true`, FLEET.md §6)
   — fleet-wide bulk vars.
4. **Preserved existing dockerhost `.env` entries** not covered by 1-3
   — the historical escape hatch, kept for anything not yet migrated.

Earlier sources win on key collision. `extra_env:` values are written
in sorted-key order for diffability, same as every other source in
this function.

**Guardrail**: before writing an `extra_env:` value, fleet-runner
checks the key against a secret-shaped substring list (`TOKEN`,
`SECRET`, `PASSWORD`, `PASSWD`, `APIKEY`, `API_KEY`, `PRIVATE_KEY`,
`CREDENTIAL`). A match aborts the deploy with an error pointing at
`secrets:` instead. This is a heuristic, not a guarantee — it exists
to catch the obvious "someone declared `DB_PASSWORD` in `extra_env:`
because it was easier" mistake at deploy time, in a repo whose
`service.yaml` is public git history, rather than relying on code
review alone to catch it after the fact. It does not replace the
"secrets live in `go-fleet-secrets`, NEVER in env on dockerhost" rule
in `CLAUDE.md` — it enforces the non-secret half of that boundary
mechanically.

Two services adopt it in this change: `go-fleet-pipe`
(`PIPE_PUBLIC_BASE_URL: https://pipe.0exec.com`) and `go-fleet-webhook`
(`WEBHOOK_PUBLIC_BASE_URL: https://webhook.0exec.com`) — replacing the
hand-placed dockerhost `.env` lines with the same values, now in git.

## Consequences

**Positive**
- Non-secret config becomes reviewable: a PR against `service.yaml`
  is the record of *what* changed and *why*, instead of an SSH session
  nobody wrote down.
- Survives a from-scratch dockerhost bootstrap — `extra_env:` values
  materialize on first deploy exactly like `secrets:` and
  `proxy_egress` already do; no manual post-bootstrap checklist step.
- One mental model for "how does an env var get onto this service":
  secret → `secrets:`, non-secret and worth tracking → `extra_env:`,
  shared bulk proxy config → `proxy_egress: true`, ad hoc/legacy →
  hand-edited `.env` (still supported, still lowest precedence).
- The secret-shaped-key guard fails the deploy loudly instead of
  quietly committing a credential to a version-controlled file.

**Negative**
- A fourth precedence tier is more to hold in your head than three.
  Mitigated by keeping the doc comment on `materializeServiceEnv`
  as the single source of truth for ordering, and by the printed
  `N vault + N extra_env + N proxy + N preserved` line at deploy time
  making the merge visible.
- The secret-shaped-key guard is a substring heuristic — it can both
  false-positive (a legitimately public var whose name happens to
  contain `KEY`, e.g. a cache-key prefix) and false-negative (a
  credential whose name doesn't match any fragment). Mitigated by:
  false positives are a one-line rename or a `secrets:` declaration
  either way works; false negatives are no worse than the pre-existing
  state (nothing caught them before this ADR).

**Mitigations**
- Services without an `extra_env:` block are unaffected — no new
  deploy path, no new dependency, identical behavior to before this
  ADR.

## Migration path

For a service with a hand-placed, non-secret dockerhost `.env` entry:

1. Confirm the value is genuinely not a secret (if it authenticates
   or authorizes anything, use `secrets:` instead — see ADR-0025).
2. Add it to `service.yaml`'s `extra_env:` block, commit, push.
3. `fleet-runner deploy <slug>` — the new value materializes into
   `/opt/services/<slug>/.env` alongside (or replacing) the hand-placed
   line; deploy log shows the updated `N vault + N extra_env + ...`
   count.
4. Confirm the running container picked it up (e.g. for a public-URL
   var, call the service and check the returned URL matches).

`go-fleet-pipe` and `go-fleet-webhook` migrate in this change, moving
`PIPE_PUBLIC_BASE_URL` / `WEBHOOK_PUBLIC_BASE_URL` from a hand-placed
dockerhost `.env` line (set 2026-08-05, same day, as an immediate
remediation) to a tracked `extra_env:` declaration.

## Alternatives considered

**A) Fix `go-fleet-mcp-gateway`'s outbound proxy to set
`X-Forwarded-Host`/`X-Forwarded-Proto` when it calls backend services
directly.** More architecturally "correct" — the existing
header-derivation path would then just work. Rejected for now because
it requires the gateway to know each backend's real public hostname
(the same information `extra_env:` would otherwise carry, just moved
into a third repo), and because `extra_env:` also fixes the case where
*any* other internal caller hits these services directly, not only the
MCP gateway. Not mutually exclusive with this ADR — worth doing later
if more services hit the same class of bug.

**B) A dedicated `fleet-runner env set <slug> KEY=VALUE` imperative
command**, mirroring `fleet-runner key provision <slug>` for vault
secrets. Rejected: that command exists for vault secrets because the
*value itself* needs generation/rotation machinery. A non-secret
literal has no such need — a git-tracked YAML map is strictly simpler
and gets code review for free, which an imperative CLI mutation
against a live dockerhost does not.

**C) Keep using hand-edited dockerhost `.env` files, just document the
convention better.** Rejected: the whole failure mode here was
"correctly implemented (preserved-entry merge already existed), still
invisible." Better docs on an inherently non-version-controlled
mechanism doesn't fix that; the fix is putting the value in version
control.

## References

- [ADR-0025](0025-vault-integrated-admin-tokens.md) — the `secrets:`
  precedent this ADR's `extra_env:` sits alongside; same
  `materializeServiceEnv` function, same deploy-time-merge shape.
- `FLEET.md` §6 — `proxy_egress:`, the other declarative
  `service.yaml` env source this one is now ordered against.
- `go_fleet_runner/deploy_secrets.go` — `materializeServiceEnv`,
  `looksLikeSecretEnvKey`.
- `go-fleet-pipe/service.yaml`, `go-fleet-webhook/service.yaml` — the
  two services that adopt `extra_env:` in this change.
- Incident: `fleet-pwgen` MCP server ecosystem discovery surfaced that
  `pipe-push` and `webhook-new-bin` were returning private
  (`10.10.10.20`) URLs to MCP callers — verified live by a rejected
  garbage-token connection test, an SSRF-guard probe of the gateway's
  own routes, and (post-fix) a real external POST to the new public
  webhook URL.
