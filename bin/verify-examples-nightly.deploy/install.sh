#!/bin/sh
# Install the nightly example check on Builder CT108 (it holds the keystore admin env and reaches the docker hosts).
# Idempotent. Run from a fresh checkout of services-registry, as root, ON the builder.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
install -m 0755 "$here/../verify-examples-run.sh" /usr/local/bin/verify-examples-run
install -m 0755 "$here/../verify-examples-metrics.py" /usr/local/bin/verify-examples-metrics
install -m 0755 "$here/verify-examples-nightly.sh" /usr/local/bin/verify-examples-nightly
install -m 0644 "$here/verify-examples-nightly.service" /etc/systemd/system/verify-examples-nightly.service
install -m 0644 "$here/verify-examples-nightly.timer" /etc/systemd/system/verify-examples-nightly.timer
mkdir -p /var/lib/prometheus-node-exporter/textfile
systemctl daemon-reload
systemctl enable --now verify-examples-nightly.timer
systemctl --no-pager list-timers verify-examples-nightly.timer | head -3
