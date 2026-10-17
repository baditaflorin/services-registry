#!/bin/sh
# Install fleet-version-pin-sync on Builder CT108 (it needs the LAN docker hosts, the authenticated `gh` and the
# GHCR-authenticated docker CLI that only the builder has). Idempotent. Run from a fresh checkout of services-registry.
# The timer is enabled; the first run happens at the next slot (or: systemctl start fleet-version-pin-sync.service).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
install -m 0755 "$here/../fleet-version-pin-sync.py" /usr/local/bin/fleet-version-pin-sync
install -m 0644 "$here/fleet-version-pin-sync.service" /etc/systemd/system/fleet-version-pin-sync.service
install -m 0644 "$here/fleet-version-pin-sync.timer" /etc/systemd/system/fleet-version-pin-sync.timer
[ -e /etc/default/fleet-version-pin-sync ] || install -m 0644 "$here/fleet-version-pin-sync.defaults" /etc/default/fleet-version-pin-sync
systemctl daemon-reload
systemctl enable --now fleet-version-pin-sync.timer
systemctl --no-pager list-timers fleet-version-pin-sync.timer | head -3
