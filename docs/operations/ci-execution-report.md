# CI execution host report

The report classifies recent Woodpecker pipelines by the physical host that ran
an agent. Keep generated agent names and known stable agent IDs mapped in
`ci-execution-report.json`. Unknown agent IDs should be marked `unattributed`
until their host is verified. The 0exec default is also `unattributed`, so a
missing mapping cannot incorrectly credit a host.

On Builder LXC 108, run:

```sh
ci-execution-report \
  --config /etc/ci-execution-report/config.json \
  --limit 100 --include-active
```

The command reads each Woodpecker token from its configured root-managed token
file and does not print credential values. Use `--json` for machine-readable
output. `ci.0exec.com` and `ci.0mcp.com` are separate control planes; their
agent IDs and host mappings are independent.
