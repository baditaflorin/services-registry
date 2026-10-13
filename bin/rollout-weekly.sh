#!/bin/bash
# Weekly rollout of the canonical CLAUDE.md to the fleet (see docs/operations/doc-drift.md). Run by a LaunchAgent; safe to run by hand.
# Uses its own clone of services-registry (kept at origin/main), so the tool that runs is always the merged one.
set -u
W="${FLEET_ROLLOUT_DIR:-$HOME/.local/share/fleet-claude-rollout}"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=20"
mkdir -p "$W"
[ -d "$W/services-registry/.git" ] || git clone -q git@github.com:baditaflorin/services-registry.git "$W/services-registry" || exit 2
git -C "$W/services-registry" fetch -q origin && git -C "$W/services-registry" checkout -q -B main origin/main || exit 2
echo "=== fleet CLAUDE.md rollout $(date -u +%FT%TZ) (registry $(git -C "$W/services-registry" rev-parse --short origin/main))"
python3 "$W/services-registry/bin/rollout_claude_md.py" weekly --registry "$W/services-registry" --work "$W" --extra "$W/extra_repos.txt"
rc=$?
echo "=== exit $rc"
exit $rc
