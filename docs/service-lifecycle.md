# Service lifecycle

`overrides.json` is the canonical source for per-service lifecycle metadata.
Use `lifecycle.status` with `active`, `sunset` or `on_demand`. A sunset entry requires
`sunset_at` and a plain-language `reason`; an optional `successor` names a
validated replacement. The generated `services.json` and public mirror retain
sunset records as tombstones so catalogs can explain why a product disappeared.
Active-only ID and TRL work queues omit sunset records; the names and minimal
catalog views retain the lifecycle marker for human-facing explanations.

The generated deploy catalog also retains the lifecycle object, so anything
reading it sees the sunset marker. `fleet-runner deploy` itself reads the full
`services.json` to refuse a sunset target and print the date and reason.
`services.mcp.json` excludes sunset entries, even if an
older readiness audit still says `mcp_ready: true`. The assigned host port stays
in `services.ports.json` for as long as the sunset record is kept in the
registry, even after the old workload has been removed, preventing accidental
reuse of the port.

The runner rejects a sunset target before compose or image deployment. Its
NGINX renderer can turn the service hostname into an explicit HTTP 410 response
with a JSON retirement notice. Removing the runtime container, DNS, or route is
a separate operational action: confirm caller impact and record the rollback
plan before changing those live resources.

## on_demand: stopped for now, wanted later

`on_demand` is for a service that is deliberately not running at the moment but
is not retired: a pentest dataset sync that is not needed this month, a pilot
that is paused. It is not `sunset`: nothing is retired, no 410 notice is
written, and the entry stays in the ID, TRL and MCP views.

- Required: `on_demand_since` (date) and `reason`. Optional: `review_by`
  (date), the day somebody decides to bring it back or retire it.
- The entry, port, route and DNS stay. The port stays in `services.ports.json`.
- Edge probe, selftest poll, example verification and version-pin sync skip it,
  so a stopped service raises no `FleetServiceEdgeDown`. The edge probe still
  reports it as skipped with `lifecycle="on_demand"`.
- To bring it back: set `status` to `active` (drop the on_demand fields) in a
  pull request, then deploy it with `fleet-runner deploy`.
- Stopping the container itself is a separate, deliberate action. `restart:
  always` and the daily compose rewrite will start it again until fleet-runner
  learns to leave an `on_demand` service stopped; the fleet-runner change is
  tracked apart from this one.
