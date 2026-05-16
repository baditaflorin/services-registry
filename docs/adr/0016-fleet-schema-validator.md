# ADR-0016 — Central JSON Schema catalog + per-call response validator (`go-fleet-schema-validator`)

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: claude-opus-4-7-session-2026-05-16-schema-validator
* **Tags**: fleet-infra, contracts, validation, primitive
* **Tier**: B #14 from FLEET-FUTURE-TOOLS.md

## Context

Each fleet service's response shape is implicit. The shape lives in (a) the handler's `json.Marshal` of an ad-hoc struct, (b) the consumer's `json.Unmarshal` into its own ad-hoc struct, and (c) nobody's documentation. When `findings-store` adds a field, or — worse — renames an existing one, we don't know who breaks until they break in prod.

Observed in the last two months alone:

- **`findings-store` /findings** renamed `created` → `created_at` in 0.4.1. Three downstream consumers silently started seeing zero-valued `time.Time` because the field was no longer there. Detected eight days later when a triage queue showed every new finding sorted to the bottom of the "oldest first" view.
- **`scope-guard` /check** added a required body parameter; older consumers kept POSTing without it and started receiving 400s. The 400 body was JSON with a `error` key; consumers decoded it as a `ScopeResponse`, got `in_scope=false` via zero-value, and quietly stopped flagging out-of-scope targets.
- **`target-normalizer`** changed an enum from `"ipv6"` to `"ipv6-addr"`. The downstream `asset-inventory` matched on the old string and started misclassifying. No errors anywhere.

The pattern is consistent: **silent shape drift**. There's no integration test pair for every service edge — the fleet has ~30 active container services and >100 service-to-service edges. Per-pair contract tests don't scale; per-pair manual verification doesn't happen.

The wider industry pattern for this problem is JSON Schema with a contract registry. We've been resistant to JSON Schema in the past because (a) it's verbose, (b) Draft 2020-12 is a moving target, and (c) most Go libraries are mid-tier. But santhosh-tekuri/jsonschema/v5 is genuinely good (passes the official conformance test suite), and Draft 7 is stable enough that nobody is breaking it anymore.

## Decision

Ship `go-fleet-schema-validator` (port 18166, `mesh-0exec`, `category: fleet-infra`) as the canonical schema catalog. API:

```
POST /validate      {service, endpoint, payload}
  → {valid, schema_id, schema_version, errors:[{path,message}]}

POST /schemas       {service, endpoint, schema}        (X-Admin-Token)
GET  /schemas       ?service=X&endpoint=Y
GET  /schemas/list

GET  /selftest      → 200/503 (conforming + non-conforming round-trip)
GET  /health, /version, /metrics  (from go-common/server)
```

**Storage**: SQLite single-file under `/data/schemas.db`. One table:

```sql
schemas(id INTEGER PK AUTOINCREMENT, service TEXT, endpoint TEXT,
        version INTEGER, schema_json TEXT, created_at TEXT,
        UNIQUE(service, endpoint, version))
```

**Versioning**: monotonic per `(service, endpoint)`. The `Register()` path takes a pinned `*sql.Conn`, `BEGIN IMMEDIATE`, `SELECT COALESCE(MAX(version),0)+1`, `INSERT`, `COMMIT`. The IMMEDIATE promotion is load-bearing: two concurrent registrations of the same endpoint without it would both observe `MAX=N` under SQLite's deferred snapshot, both try to `INSERT version=N+1`, and one would fail on the UNIQUE constraint — which would surface as a confusing 500 rather than the operator's intuition of "we both pressed Enter at the same time, server pick a winner." With IMMEDIATE, the loser blocks on the writer lock and observes the new MAX after the winner commits.

**Validation**: santhosh-tekuri/jsonschema/v5, forced to Draft 7. Compile cache keyed on `(schema_id, version)` under `sync.RWMutex` — schemas are ~10 KB, recompilation is ~2 ms, the cache cuts it to ~10 µs. `AssertFormat=false` because format checks (email, URI, etc.) are advisory in Draft 7 and we don't want a `format` typo to block a payload that's structurally fine.

**Seeding**: eight baseline schemas baked into the binary (`seed.go`) for the most-consumed endpoints (findings-store /findings, finding-triage /triage, scope-guard /check, target-normalizer /, asset-inventory /assets, takeover-checker /check, oob-collector /issue, payoff-tracker /stats). On first boot, each is registered if-and-only-if no row exists for that `(service, endpoint)` — operator-uploaded schemas always win. This gives the validator real utility on day zero without waiting for service authors to come around to it.

**Migration**: opt-in, two-step.

1. **Registration**: each service author runs `curl -H "X-Admin-Token: $T" -d '{...}' .../schemas` once at deploy time (or after a shape change). The seeded baseline buys time — they only have to act when the baseline diverges from reality.
2. **Consumer adoption**: consumers POST `/validate` before acting on a response and degrade or alert on `valid=false`. Initially we expect this to be the dashboard + the orchestrator only. Everyone else picks it up at their own pace.

**Future state** (NOT this ADR): gateway-level enforcement. nginx's `body_filter_by_lua_block` reads the response, hits the validator inline, and rejects non-conforming responses with a 502 + a `X-Schema-Errors` header. This is deferred because (a) it requires every endpoint to have a registered schema to avoid mass false 502s, (b) the latency tax is real (one extra round-trip per response), and (c) the fail-open contract is fiddly (do we open on validator-down? on unknown schema? both?). Until we have ~80% schema coverage, gateway enforcement would hurt more than it helps.

## Consequences

**Positive**:

- Single source of truth for "what does `findings-store /findings` look like, today, version N". Searchable, diffable, persisted. The 2026-05-08 `created → created_at` rename would have surfaced as one diff in the catalog and a thousand `valid=false` in consumer logs, instead of an eight-day silent failure.
- Schema-as-contract: a service can refuse to merge a breaking change unless it bumps the registered schema first. The CI check is `validate the test fixture; if it doesn't pass against version N, you owe a version N+1`.
- Versioning + history retention means rollback is trivial. Operator can `GET /schemas/list?version=N-1` and see what every consumer was reading against before today's bad deploy.
- Re-uses the canonical fleet shape (go-common/server, keystore-gated reads, X-Admin-Token writes, BEGIN IMMEDIATE for write ordering) — no new operational primitives.

**Negative**:

- One more network hop per response, _if_ consumers opt in to validate. The validate path is sub-millisecond at steady state (cached compile), so the round-trip is the cost, not the work. Latency-sensitive paths can skip validation.
- Schemas decay. A schema that's never updated past version 1 will start lying as the service evolves. Mitigation: `/schemas/list` exposes `created_at` so an auditor can flag schemas older than the service's last deploy.
- "Trust the catalog or trust the code" — if a consumer's behavior diverges from what `validate` says, who wins? The catalog. The whole point is the catalog is the spec. This needs to be communicated; otherwise we'll see "the validator is wrong" tickets that are really "the service author forgot to bump the schema."

**Mitigations**:

- `/selftest` exercises the validator end-to-end against the findings-store baseline; fleet-runner's smoke gate fails on 503 and refuses to deploy a broken validator binary.
- Admin writes are gated by a separate `SCHEMA_ADMIN_TOKEN` env var (not the keystore admin token; one revoke shouldn't compromise both). Constant-time compare. Audit logging is out of scope for v0.1; the writes are rare enough that operator memory + git history of `seed.go` overrides is sufficient. Audit will come if/when we see >10 admin writes/day.
- Compile cache invalidates on schema_id basis — the rare "POST /schemas then immediately /validate" pattern will pick up the new version on the next call because `Current()` always reads the latest from SQLite, and the new id was never in the cache.

## Alternatives considered

1. **OpenAPI + a code-gen pipeline**. Heavier, requires every service to author a spec, and OpenAPI's response-shape coverage is weaker than raw JSON Schema. Punt.
2. **Per-service contract tests in CI**. The fleet has no CI (`anthropic-hygiene-2026-05-16.md`: Husky + local `go test ./...` is the gate). Cross-service tests require a fixture exchange that nobody owns; we tried this in 2026-03 and it died after one sprint.
3. **Embed schemas in each service's `/version` response**. Schemas would always match the deployed code, but consumers would have to fetch + compile per service; we'd have no central diff view; rollback would lose the old schema. The catalog wins.
4. **Use a third-party schema registry (Apicurio, Confluent Schema Registry, etc.)**. Operationally heavy, JVM-ish, designed for streaming systems. Way oversized for ~30 services.

## References

- `services-registry/CLAUDE.md` — fleet conventions; service shape rules
- `FLEET-FUTURE-TOOLS.md` Tier B #14 — original proposal
- santhosh-tekuri/jsonschema/v5 — validation library
- ADR-0004 — `go-fleet-body-redactor` (sibling fleet-infra primitive; same shape)
