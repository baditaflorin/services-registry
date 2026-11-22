#!/bin/sh
# Nightly example check, run ON the builder (CT108) by verify-examples-nightly.timer.
# Runs verify-examples-run.sh --local (a 1 h scoped key, revoked at the end), keeps the result in
# /var/lib/verify-examples/services.examples.json and writes the Prometheus textfile. Regressions (ok -> broken) go to the journal.
set -u
STATE=/var/lib/verify-examples
PROM=${VERIFY_EXAMPLES_PROM:-/var/lib/prometheus-node-exporter/textfile/fleet_examples.prom}
OUT=$STATE/services.examples.json
mkdir -p "$STATE"
[ -e "$STATE/PAUSE" ] && { echo "verify-examples-nightly: paused ($STATE/PAUSE exists)"; exit 0; }
[ -s "$OUT" ] && cp "$OUT" "$STATE/previous.json"
/usr/local/bin/verify-examples-run --local --out "$OUT" --ref origin/main
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
  /usr/local/bin/verify-examples-metrics --new "$OUT" --out "$PROM" --failed
  echo "verify-examples-nightly: the run failed (exit $rc)" >&2
  exit "$rc"
fi
reg=$(/usr/local/bin/verify-examples-metrics --new "$OUT" --old "$STATE/previous.json" --out "$PROM")
[ -n "$reg" ] && echo "verify-examples-nightly: REGRESSED (ok -> broken): $(echo "$reg" | tr '\n' ' ')" >&2
exit "$rc"     # 3 = the key could not be shown revoked: the unit fails so it is noticed
