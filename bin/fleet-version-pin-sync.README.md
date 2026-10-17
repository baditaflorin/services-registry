# fleet-version-pin-sync

Keeps each container service's registry `version` (hand-pinned in `overrides.json`, copied into `services.json`
by `generate.py`) equal to the version the service actually runs.

## Why it exists

`fleet-runner bump-version` updates a service's `service.yaml`, its `Version` constant and its CHANGELOG, never
the registry pin, so every release leaves the pin behind. On 2026-10-16, 157 of 387 container services had a stale
pin and 96 had none (#797, #800). Readers that care: go-fleet-preflight (`version` check, and the `image` check,
which HEADs `ghcr.io/baditaflorin/<id or repo>:<version>`) and `fleet-runner deploy-pool`, which refuses a service
without a version and whose placement treats a red image check as blocking.

## The rule

| running vs repo main | pin | action |
|---|---|---|
| equal, higher than the pin | exists | raised, **only when ghcr.io has that image** (never turn the image check red) |
| equal | missing | added (no version is already the worse failure) |
| equal | same | ok |
| different (undeployed release, deploy from a branch, composite runner reporting its own version) | any | left alone |
| lower than the pin | any | left alone (never lowered; CI rejects it too) |

The running version is `GET /version` on the LAN docker hosts for compose services, else the public URL.

## A run

Fresh checkout of main in `/var/lib/fleet-version-pin-sync/repo` → audit → with `--apply`: pins set in
`overrides.json` (in place, every other byte kept) → `generate.py --only <ids>` → assert services.json changed
only `version` in exactly those rows → version-regression check + unit tests → branch `bot/version-pin-sync-<ts>`,
pull request, wait for Woodpecker on that head, squash-merge pinned to it. A red CI leaves the pull request open and
fails the unit; no new pull request is opened while one is open.

Metrics (`fleet_version_pins{state}` = ok / stale / missing / skipped, `fleet_version_pin_sync_success`,
`..._applied`, `..._last_run_timestamp_seconds`) go to the node_exporter textfile.

## Deploy (Builder CT108)

```sh
sh bin/fleet-version-pin-sync.deploy/install.sh   # idempotent; config in /etc/default/fleet-version-pin-sync
```

Report only, by hand: `fleet-version-pin-sync --json` (no `--apply`). Stop it: `touch /var/lib/fleet-version-pin-sync/PAUSE`.
Tests: `python3 -m unittest bin/test_fleet_version_pin_sync.py`.
