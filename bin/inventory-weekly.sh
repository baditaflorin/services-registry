#!/bin/bash
# Weekly Proxmox inventory check (see bin/inventory_weekly.py). Run by a LaunchAgent; safe to run by hand.
# Keeps its own clones of services-registry (the tool) and fleet-state (the data and the docs) under $FLEET_ROLLOUT_DIR.
set -u
if [ -z "${INVENTORY_WEEKLY_COPY:-}" ]; then
  cp "$0" "${TMPDIR:-/tmp}/inventory-weekly.$$.sh" && INVENTORY_WEEKLY_COPY=1 exec bash "${TMPDIR:-/tmp}/inventory-weekly.$$.sh" "$@"
fi
W="${FLEET_ROLLOUT_DIR:-$HOME/.local/share/fleet-claude-rollout}"
export PATH="${FLEET_ROLLOUT_EXTRA_PATH:+$FLEET_ROLLOUT_EXTRA_PATH:}$HOME/.local/bin:$HOME/Documents/GitHub/.tools:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=20"
mkdir -p "$W"
[ -d "$W/services-registry/.git" ] || git clone -q git@github.com:baditaflorin/services-registry.git "$W/services-registry" || exit 2
[ -d "$W/fleet-state/.git" ] || git clone -q git@github.com:baditaflorin/fleet-state.git "$W/fleet-state" || exit 2
git -C "$W/services-registry" fetch -q origin && git -C "$W/services-registry" checkout -q -B main origin/main || exit 2
echo "=== inventory check $(date -u +%FT%TZ) (registry $(git -C "$W/services-registry" rev-parse --short origin/main))"
python3 "$W/services-registry/bin/inventory_weekly.py" --fleet-state "$W/fleet-state" --report "$W/inventory-report.json"
rc=$?
echo "=== exit $rc"
exit $rc
