# CI resource profile (`ci-resources.json`)

How much memory (and CPU) a CI build of each repository needs, derived from measurements. Derived file: never
hand-edit it; `bin/ci_resources.py` writes it and `bin/generate.py` merges a summary into `services.json` as the
optional `ci_resources` field (kept out of `services-public.json`, like the other operational fields).

## What it measures

Prometheus `woodpecker_build_step_memory_peak_bytes_24h` has one series per step-container run: the peak memory of
that run. `woodpecker_build_step_cpu_seconds_24h` has the same labels (CPU seconds). Per run the script takes
`max_over_time(...[7d])`, groups by repository and step name, and records per step the run count, nearest-rank p95
and max of the peak, and the p95 CPU seconds. A repository's `mem_peak_p95_bytes` is the largest step p95; its
class comes from that number. Zero-byte samples (a container that exited before the first sample) are ignored.

| class | per-step memory ceiling | planned agent label |
|---|---|---|
| `xs` | 256 MiB | `mem=xs` |
| `s` | 512 MiB | `mem=s` |
| `m` | 4 GiB | `mem=m` |
| `l` | ~10 GiB | `mem=l` |

The class is a requirement a workflow can ask for ("this step needs at least class m"). Nothing is pinned to a
host; the scheduler may place it on any agent that offers that much. `confidence: low` = fewer than 20 measured
pipelines in the window. `oom_suspect: true` = some run reached 4.9 GiB or more: builders cap a container at 5 GiB
and a killed run reports a truncated peak, so the real need is at least that, probably higher.

A Woodpecker repository maps to registry ids by the last path segment of `repo_url` in `services.json`. Repositories
with no id (private and infrastructure repos such as `go-common`) are NOT named: this file lives in a public repository, so they appear only in the anonymous `unmapped` aggregate (count, runs, highest p95). The run prints the count, never the names.

## Refresh

```sh
ssh -f -N -L 19090:127.0.0.1:9090 0mcp_monitoring     # Prometheus is reachable only through this tunnel
python3 bin/ci_resources.py                            # summary only, writes nothing
python3 bin/ci_resources.py --write                    # rewrite ci-resources.json
python3 bin/ci_resources.py --check                    # offline: shape, classes, freshness (8 days), ids exist
```

Then open a PR; the next normal `bin/generate.py` run copies the profile into `services.json`. CI runs `--check`
for shape only (a freshness gate there would fail every unrelated PR); the freshness check is for the refresh job.
`--input metrics.json` runs the derivation offline on a saved instant-query result (this is what the tests use).

## Caveats

- The sampler reads container memory every 30 s, so a short spike between two samples is missed: treat the peak as
  a lower bound. Add headroom, do not size to the number.
- OOM-killed runs report a truncated peak (cap 5 GiB on most builders); see `oom_suspect`.
- The window is 7 days: a repository that did not build in that time has no entry, and a rarely built one has
  `confidence: low`.
- The sampler runs on `docker-build-lv3`, `0own-build-worker`, `infer-builder-ci` and, since 2026-10-29,
  `0pipe-build`. Builds on any other host are not measured. The same repository can have different peaks on
  different hosts; the profile pools them.
