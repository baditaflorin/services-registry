# ADR-0032 — Fleet-wide MCP support: agent contract, gateway, registry flag

* **Status**: Accepted
* **Date**: 2026-08-05
* **Authors**: claude-sonnet-5 (with florin)
* **Tags**: mcp, agent, go-common, gateway, registry

## Context

AI agents (Claude, Codex, others) are a first-class caller class for
fleet services, but the fleet has no native [Model Context
Protocol](https://modelcontextprotocol.io) surface — an agent that
wants to call a fleet service has to be told, out of band, the URL,
auth shape, and input schema for each one it wants to use. `go-common`
already shipped the `agent` package (v0.73.0, `WithAgent` /
`WithAgentFromEmbed` → `GET /agent.json`) as a machine-readable
contract explicitly designed "intentionally close to the MCP tool
object... so a bridge can translate 1:1 without remapping" — but
nothing built that bridge yet, and zero fleet services had adopted the
contract.

Two topology options: every service speaks MCP directly, or one
central gateway aggregates. Given ~250 services and the fleet's
existing single-gateway/single-keystore architecture (`services-registry/CLAUDE.md` — "Both
container meshes are gated by the same keystore"), a gateway matches
how the fleet already thinks about auth and discovery: one thing to
add to an agent's MCP config, not 250.

## Decision

Four-repo rollout, landed in order:

1. **`go-common` v0.74.0** — `server.WithMCP()`. Mounts `GET/POST /mcp`
   (Streamable HTTP, `github.com/modelcontextprotocol/go-sdk` pinned
   `v1.5.0`) on the same `s.Mux` a service already uses. Each
   `agent.Tool` in the service's contract becomes one MCP tool;
   `tools/call` replays in-process against the service's own HTTP
   route (`httptest.NewRecorder`, no network hop) using the tool's
   `Method`/`Path` (new, additive `agent.Tool` fields, default `GET
   "/"`) — so the MCP surface can't drift from the real handler. No
   new auth: `/mcp` sits behind the same `WithKeystoreAuth` middleware
   chain as every other route.

2. **`go-fleet-mcp-gateway`** (new repo, `fleet-mcp-gateway.0exec.com`,
   port 18313) — periodically (`REGISTRY_REFRESH`, default 5m) scans
   `services.json`, probes every `kind: container` service's
   `/agent.json`, and aggregates every tool found into one MCP server
   namespaced `"<service-id>.<tool-name>"`. `tools/call` forwards to
   the real upstream service using the **caller's own**
   `Authorization`/`X-API-Key` header — the gateway holds no fleet API
   key itself, so a keystore revoke kills gateway access exactly like
   direct access. `GET /gateway/tools` exposes the current aggregation
   for debugging and for the registry audit below.

3. **`services-registry`** — `mcp_ready` (+ `mcp_tool_count`,
   `mcp_assessed_at`) as a recognized per-service field
   (`schema/v1.json`, `bin/generate.py`'s override-passthrough list,
   new `services.mcp.json` slice). Populated by
   `bin/audit_mcp_ready.py`, which reads the gateway's own `GET
   /gateway/tools` rather than re-probing 447 services independently —
   the gateway is already the authoritative live-discovery process;
   mirroring its findings into the registry keeps there being exactly
   one source of truth instead of two that can disagree.

4. **hub** (`hub.scrapetheworld.org`) — a "MCP" directory chip +
   filter facet, live-verified the same way the existing "Reachable" /
   OpenAPI chips are (a probe, not just trusting the registry flag),
   plus a copy-paste MCP client config snippet defaulting to the
   gateway URL.

A service opts in with two lines and an `agent.json`:

```go
srv := server.New(cfg,
    server.WithAgentFromEmbed(agentFS, "agent.json"),
    server.WithMCP(),
)
```

## Consequences

**Positive**: one MCP endpoint for the whole fleet; auth reuses the
existing keystore verbatim (zero new secrets, zero new token type);
per-service `/mcp` still available directly (Phase 1) for anyone who
wants to bypass the gateway; the gateway can't lie about what's
MCP-ready because it's driving the registry flag, not the other way
around.

**Negative / Mitigations**: the gateway is a new single point of
aggregation (not of auth — see above) for agents using the combined
view; if it's down, per-service `/mcp` still works directly.
Onboarding a service still requires writing a real `agent.json` (the
`DefaultTool` auto-contract is a floor, not a substitute for an
accurate input schema) — this is intentional friction: a wrong schema
is worse than no schema, an agent will call it wrong either way but
a declared contract at least fails predictably.

## Migration path

Existing services: nothing changes until a service opts in. Opt-in is
`server.WithAgent(...)`/`WithAgentFromEmbed(...)` + `server.WithMCP()`
in `main.go`, ship an `agent.json`, bump the `go-common` dep, deploy.
The gateway picks it up on its next `REGISTRY_REFRESH` (≤5 min) with
zero registry changes required — `mcp_ready` is a read-only mirror of
what the gateway already found, set by the audit script, never
hand-written.

## Alternatives considered

### A — Every service speaks MCP directly, no gateway

Rejected as the *only* interface (Phase 1 still ships `WithMCP()` for
this): would need 250 separate MCP config entries for an agent to use
the whole fleet, and doesn't match the fleet's existing
single-gateway auth model. Kept as a complementary option, not the
primary one.

### B — Hand-roll the MCP wire protocol in `go-common`

Rejected: the Streamable HTTP transport (session semantics, SSE,
JSON-RPC framing) is exactly the kind of thing a spec-maintained SDK
should own. `github.com/modelcontextprotocol/go-sdk` is official,
Google-co-maintained, and the stable `v1.5.0` release covers
Streamable HTTP + OAuth. Pinned rather than `@latest` per the fleet's
supply-chain-lag convention.

### C — `mcp_ready` set by hand per service.yaml, like most other fields

Rejected: every other registry field sourced from a service's own
`service.yaml`/`overrides.json` is a *claim*, verified later (if at
all) by a separate audit. `mcp_ready` is cheap to verify continuously
(the gateway is already doing the probing as its core job) — deriving
it from the gateway's own findings means the flag can never drift
false-positive the way, e.g., stale TRL claims have (see ADR-0031's
2026-08-05 update, discovered during this same rollout).

## References

- `go-common` `agent` package + `WithAgent`/`WithAgentFromEmbed`
  (v0.73.0), `server.WithMCP()` (v0.74.0)
- `go-fleet-mcp-gateway` — new repo, port 18313
- `services-registry/bin/audit_mcp_ready.py`, `schema/v1.json`
  (`mcp_ready`/`mcp_tool_count`/`mcp_assessed_at`),
  `services.mcp.json`
- ADR-0031 (2026-08-05 update) — the `fleet-backup` port-collision
  incident hit while deploying the gateway; unrelated to the MCP
  design itself but discovered in the same rollout
