# Label-quality contracts

`quality_contract` is private registry metadata for services that make a
classification or extraction claim. It is intentionally absent from
`services-public.json`: a public caller needs an API contract, not internal
test commands, dataset pinning, or unresolved label gaps.

## Meaning

The field is optional and deny-by-default:

- no field means **not assessed**, not "good enough";
- `declared` means an owner named a dataset and intended regression command,
  but it is not yet a release signal;
- `operational` means the repository runs `regression_command` in Woodpecker
  against a dataset fixture committed in the repository (taken from
  `dataset_url` as of `dataset_revision`; the test does not fetch
  `dataset_url`), and the command has a dated pass record.

An operational contract must state which of `precision`, `recall`, `f1`,
`coverage`, `false_positive_rate`, or `false_negative_rate` it actually
measures. Do not substitute endpoint availability or unit-test count for any
of those metrics.

## Dataset rules

Use a public, stable source and record an immutable revision, release date, or
checksum. Keep customer pages and production findings out of the golden set;
their role is to produce privacy-safe, manually reviewed candidates for the
next labelled revision. A failed golden regression fails the Woodpecker
push/PR pipeline and the `fleet-runner deploy` preflight (`go test ./...`),
which blocks the deploy; it is not a required check on `main` and does not
gate tag creation. A dataset refresh is a labelled-data change and needs
review just like code.

For services with several jurisdictions, publish per-country slices and report
coverage by slice. Aggregate accuracy can otherwise hide a long-tail country
regression behind high-volume English-language examples.

## Release evidence

A quality release should record: dataset revision, command, CI URL, metrics
before and after, and representative FP/FN cases with sensitive data removed.
As of 2026-10-14 the only service with an operational contract
(country-iso-matcher) has no GitHub Releases, and its `CHANGELOG.md` entries
give no dataset revision, regression command or CI URL (0.1.2 records only the
corpus growth from 183 to 249 countries); the CI URL exists only as the
automatic Woodpecker commit status on the tagged commit. The contract is not
permission to weaken precision merely to increase coverage; both deltas must be
explicit.
