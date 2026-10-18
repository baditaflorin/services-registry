# Shared remote Go build cache: spike

Status: SPIKE (results below are filled in as the runs finish). Nothing here is deployed
anywhere except a throwaway container on one build host that was removed afterwards.

What it is: Go's `GOCACHEPROG` lets the go command use an external cache program. This
spike runs the upstream `gocached` server (github.com/bradfitz/go-tool-cache, pinned to
commit `eaec5e0c47d232087f025c184336e50375fc4b35`) and a small client of our own,
`client/` (ci-gocache), that **fails open**: if the server is down, slow, black-holed or
returns errors, a lookup is just a cache miss and the build carries on.

Why our own client: upstream's `go-cacher` command does not enable the library's
`BestEffortHTTP` mode and uses Go's default HTTP client without timeouts. Measured: with
the server down the stock client made the build fail (exit 1).

`client/` depends only on the pinned library and lz4. `scripts/` are the benchmark and
failure-mode scripts that were run on the host.
