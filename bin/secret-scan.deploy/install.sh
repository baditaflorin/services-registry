#!/bin/sh
# Install the weekly secret scan on Builder CT108. Idempotent. Run as root ON the builder from a fresh checkout of services-registry.
# First run: baseline.tsv does not exist, so that run's findings become the accepted baseline. Review them first with
#   /usr/local/bin/secret-scan /root/ss-review   (then triage summary.tsv), and only then start the unit.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
install -d /usr/local/share/secret-scan
install -m 0644 "$here/gitleaks.toml" /usr/local/share/secret-scan/gitleaks.toml
install -m 0755 "$here/secret-scan.sh" /usr/local/bin/secret-scan
install -m 0755 "$here/secret-scan-weekly.sh" /usr/local/bin/secret-scan-weekly
install -m 0644 "$here/secret-scan-weekly.service" /etc/systemd/system/secret-scan-weekly.service
install -m 0644 "$here/secret-scan-weekly.timer" /etc/systemd/system/secret-scan-weekly.timer
systemctl daemon-reload
systemctl enable --now secret-scan-weekly.timer
systemctl --no-pager list-timers secret-scan-weekly.timer | head -3
