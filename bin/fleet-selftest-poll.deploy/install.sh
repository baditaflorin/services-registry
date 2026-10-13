#!/bin/sh
# Install fleet-selftest-poll on Builder CT108 (the 0docker vantage; the textfile it writes is scraped by the
# 0docker Prometheus through node_exporter). Idempotent. Run from a fresh checkout of services-registry/bin.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
install -m 0755 "$here/../fleet-selftest-poll.py" /usr/local/bin/fleet-selftest-poll
install -m 0644 "$here/fleet-selftest-poll.service" /etc/systemd/system/fleet-selftest-poll.service
install -m 0644 "$here/fleet-selftest-poll.timer" /etc/systemd/system/fleet-selftest-poll.timer
[ -e /etc/default/fleet-selftest-poll ] || install -m 0644 "$here/fleet-selftest-poll.defaults" /etc/default/fleet-selftest-poll
systemctl daemon-reload
systemctl enable --now fleet-selftest-poll.timer
systemctl start fleet-selftest-poll.service
systemctl --no-pager status fleet-selftest-poll.service | head -5
