# Shared remote Go build cache: spike

Status: SPIKE, finished. Nothing from it is deployed: the throwaway server, network,
binaries and data on the test host were removed afterwards. Findings below; they decide
a design, they are not a rollout.

## What it is
Go's `GOCACHEPROG` lets the go command use an external cache program. The spike ran the
upstream `gocached` server (github.com/bradfitz/go-tool-cache, pinned to commit
`eaec5e0c47d232087f025c184336e50375fc4b35`, BSD-3) and a small client of our own,
`client/` (ci-gocache), that fails open and guards against a slow link.

Why our own client: upstream's `go-cacher` command does not enable the library's
`BestEffortHTTP` mode and uses Go's default HTTP client with no timeouts.

## Results (one build host, `go-common` build + test on `golang:1.25`, a brand-new client
per case, 1565 cache lookups per build; "uncached" is 48 s and 1080 MiB peak)

| Case | Seconds | Result |
|---|---|---|
| warm shared cache, loopback | 3 | 94% faster; 1562 of 1565 lookups hit; 583 MiB |
| filling an empty server | 47 | the 2189 uploads cost nothing noticeable |
| stock upstream client, server DOWN | 1 | **build failed (exit 1)** |
| ci-gocache, server down | 44 | built and tested fine |
| ci-gocache, server black-holed | 45 | built and tested fine |
| ci-gocache, server killed 8 s into a build | 44 | built and tested fine |
| ci-gocache, server restarted empty then refilled | 43, then 3 | recovered by itself |

Latency (delay added inside the client container's own network namespace only):

| Added round trip | No guard | With slow-response guard (50 ms) |
|---|---|---|
| 0 ms | 3 s | 3 s |
| 5 ms | 7 s | not run |
| 30 ms | 35 s | 38 s (breaker tripped once) |
| 80 ms | **80 s (slower than uncached)** | 44 s |
| 200 ms | not run | 46 s |

Each lookup costs roughly one round trip, so a shared cache over a slow link is worse
than none; the break-even is near 45 ms. The guard counts a response that takes longer
than `--slow-threshold` to START (time to headers, so big downloads are not penalised) as a
failure, so a slow server is bypassed like a dead one.

Measured round trips from the build hosts to the tailnet hub: 3 ms, 4-7 ms, 5 ms and
about 29 ms. Host-to-host paths were not measurable (the tailnet access rules hide the
hosts from each other).

## Verdict for the design
- A shared cache pays off for hosts within roughly 20 ms of the server and is harmless
  everywhere else, provided the client is ci-gocache (fail-open, timeouts, breaker, slow
  guard). Keep the per-host local layer (the client already reads local disk first).
- Open items: real host-to-host latency; production placement and capacity of the server;
  its monitoring (the server exposes a debug/metrics listener); auth and the "only trusted
  builds write the global namespace" claim; vetting and pinning the server's dependency
  tree (it pulls in tailscale.com) and building it in our own CI.

## Contents
- `client/`: ci-gocache (main.go, breaker.go, 8 unit tests, go.mod/go.sum). Depends only on
  the pinned library and lz4. `cd client && go test -race ./...`.
- `scripts/`: the benchmark and failure-mode scripts that were run (ab.sh baseline and
  stock client, ab2.sh failure modes, ab4.sh latency, ab5.sh latency with the guard).
