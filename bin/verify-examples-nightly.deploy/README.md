# verify-examples-nightly

Runs `bin/verify-examples-run.sh --local` every night at 02:40 UTC (+ up to 10 min) on Builder CT108.

- Mints one 1 h key scoped to the service hostnames (use limit 2 calls per service + 50), calls every service's `example_path`,
  revokes the key and proves it refused. Details: the header of `bin/verify-examples-run.sh`.
- Keeps the result in `/var/lib/verify-examples/services.examples.json` (the committed `services.examples.json` is refreshed by a pull request).
- Writes `/var/lib/prometheus-node-exporter/textfile/fleet_examples.prom` (`fleet_example_state{service,state}`,
  `fleet_examples_services{state}`, `fleet_examples_regressed`, `fleet_examples_run_success`, last-run timestamp).
  Alert on `fleet_examples_regressed > 0` (a service that was ok yesterday answers a broken state today) and on
  `fleet_examples_run_success == 0` or a stale timestamp (> 36 h).
- Publishes: when a state changed and the run looks healthy (at least 80% of the services that were ok still are, or are gated/no-data),
  `verify-examples-publish.py` opens a `bot/examples-*` pull request refreshing `services.examples.json` and merges it when CI is green
  (`fleet-runner ci wait --merge squash`). The hub reads that file. Opt out: `touch /var/lib/verify-examples/NO_PUBLISH`.
- Journal: `journalctl -u verify-examples-nightly` lists the regressed ids and the changed states.
- Stop it without touching the unit: `touch /var/lib/verify-examples/PAUSE`.
- Exit 3 = the key could not be shown revoked (it still expires within 1 h): the unit fails on purpose.

Install: `sh bin/verify-examples-nightly.deploy/install.sh` on the builder. Test: `python3 -m unittest bin/test_verify_examples_metrics.py`.
