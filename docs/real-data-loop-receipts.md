# Real-data loop receipts

`real-data-loop-receipts.json` is the public, metadata-only ledger for
evidence-backed service improvement loops. The canonical validator is:

```bash
fleet-runner real-data-loops --registry /root/workspace/services-registry --ref origin/main --all --strict
```

The ledger never contains a raw production record, customer content, API key,
private database query, internal hostname, or complete request/response body.
Keep the retained private sample in the approved operational evidence store and
place only its SHA-256 digest, sample size, timestamp, and a safe provenance
description in this public file.

## Receipt statuses

- `verified` is the only status that counts as a real-data improvement loop.
  It requires a known container service, a retained real sample, baseline
  metric, code commit, regression test, deployed image digest, and a strict
  same-metric improvement after release.
- `evaluated-no-change` records an honest live-data evaluation that found no
  justified code change. It is useful evidence, but is not an improvement loop.
- `blocked` records an evaluation that could not safely proceed, including a
  broken route, unavailable dependency, or insufficient input provenance. It
  is not an improvement loop.

All receipts require a concise `finding` so an operator can understand the
result without opening a private artifact.

## Required verified-loop shape

```json
{
  "id": "2026-09-13-example-service-db-correctness",
  "service_id": "example-service",
  "status": "verified",
  "recorded_at": "2026-09-13T12:00:00Z",
  "finding": "Blank canonical URLs were classified as success.",
  "input": {
    "kind": "production-db-sample",
    "provenance": "Retained private sample; public metadata only.",
    "sample_sha256": "64 lower-or-upper hexadecimal characters",
    "sample_count": 50,
    "captured_at": "2026-09-13T11:00:00Z"
  },
  "baseline": {"name": "valid_result_rate", "value": 0.72, "unit": "ratio"},
  "change": {
    "repository": "https://github.com/baditaflorin/go_example_service",
    "commit": "full 40-character git SHA",
    "summary": "Reject blank canonical URLs before classification.",
    "regression_test": "go test ./..."
  },
  "deployment": {
    "version": "1.2.3",
    "image_digest": "sha256:64 hexadecimal characters",
    "verified_at": "2026-09-13T12:30:00Z"
  },
  "verification": {
    "metric": {"name": "valid_result_rate", "value": 0.92, "unit": "ratio"},
    "direction": "higher-is-better",
    "method": "Repeat the retained sample after deployment."
  }
}
```

Use `lower-is-better` for metrics such as error rate or latency. A verified
receipt must improve strictly; equal or worse results are rejected by the
validator and never counted.
