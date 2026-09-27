# Service lifecycle

`overrides.json` is the canonical source for per-service lifecycle metadata.
Use `lifecycle.status` with `active` or `sunset`. A sunset entry requires
`sunset_at` and a plain-language `reason`; an optional `successor` names a
validated replacement. The generated `services.json` and public mirror retain
sunset records as tombstones so catalogs can explain why a product disappeared.
Active-only ID and TRL work queues omit sunset records; the names and minimal
catalog views retain the lifecycle marker for human-facing explanations.

The generated deploy catalog also retains the lifecycle object. This is
intentional: deploy tools need the record to refuse a sunset target and print
the date and reason. `services.mcp.json` excludes sunset entries, even if an
older readiness audit still says `mcp_ready: true`. The assigned host port stays
in `services.ports.json` while the old workload exists, preventing accidental
reuse during retirement.

The runner rejects a sunset target before compose or image deployment. Its
NGINX renderer can turn the service hostname into an explicit HTTP 410 response
with a JSON retirement notice. Removing the runtime container, DNS, or route is
a separate operational action: confirm caller impact and record the rollback
plan before changing those live resources.
