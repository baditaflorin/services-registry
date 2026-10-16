#!/bin/bash
# Weekly rollout of the canonical CLAUDE.md to the fleet (see docs/operations/doc-drift.md). Run by a LaunchAgent; safe to run by hand.
# Uses its own clone of services-registry (kept at origin/main), so the tool that runs is always the merged one.
set -u
# This file lives in the clone the script resets to origin/main, and bash reads a script while it runs: run from a private copy.
if [ -z "${ROLLOUT_WEEKLY_COPY:-}" ]; then
  cp "$0" "${TMPDIR:-/tmp}/rollout-weekly.$$.sh" && ROLLOUT_WEEKLY_COPY=1 exec bash "${TMPDIR:-/tmp}/rollout-weekly.$$.sh" "$@"
fi
W="${FLEET_ROLLOUT_DIR:-$HOME/.local/share/fleet-claude-rollout}"
mkdir -p "$W"
# one run at a time (a run that was killed leaves a lock with a dead pid: that one is taken over)
LOCK="$W/.run.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  old="$(cat "$LOCK/pid" 2>/dev/null)"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then echo "another rollout is running (pid $old); nothing to do"; exit 0; fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 2
fi
echo $$ > "$LOCK/pid"; trap 'rm -rf "$LOCK"' EXIT
# launchd gives a bare PATH; gh lives in different places on different Macs (FLEET_ROLLOUT_EXTRA_PATH adds more)
export PATH="${FLEET_ROLLOUT_EXTRA_PATH:+$FLEET_ROLLOUT_EXTRA_PATH:}$HOME/.local/bin:$HOME/Documents/GitHub/.tools:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=20"
# The extra repositories (not services in the registry) are listed in fleet-state, rollout/extra_repos.txt: the source of truth, so the list survives the
# loss of this work directory. The local file is a cache and the fallback when fleet-state cannot be reached or has no list yet.
sync_extra_repos() {
  local fs="$W/fleet-state" tmp="$W/extra_repos.txt.fetched"
  [ -d "$fs/.git" ] || git clone -q git@github.com:baditaflorin/fleet-state.git "$fs" 2>/dev/null || { echo "extra_repos: fleet-state not available, using the local file"; return 0; }
  git -C "$fs" fetch -q origin 2>/dev/null || { echo "extra_repos: fetch of fleet-state failed, using the local file"; return 0; }
  if git -C "$fs" show origin/master:rollout/extra_repos.txt > "$tmp" 2>/dev/null && grep -q -v -E '^[[:space:]]*(#|$)' "$tmp"; then
    mv "$tmp" "$W/extra_repos.txt"; echo "extra_repos: synced from fleet-state ($(grep -c -v -E '^[[:space:]]*(#|$)' "$W/extra_repos.txt") names)"
  else
    rm -f "$tmp"; echo "extra_repos: no list in fleet-state yet, using the local file"
  fi
}
[ -d "$W/services-registry/.git" ] || git clone -q git@github.com:baditaflorin/services-registry.git "$W/services-registry" || exit 2
git -C "$W/services-registry" fetch -q origin && git -C "$W/services-registry" checkout -q -B main origin/main || exit 2
sync_extra_repos
echo "=== fleet CLAUDE.md rollout $(date -u +%FT%TZ) (registry $(git -C "$W/services-registry" rev-parse --short origin/main))"
python3 "$W/services-registry/bin/rollout_claude_md.py" weekly --registry "$W/services-registry" --work "$W" --extra "$W/extra_repos.txt"
rc=$?
echo "=== exit $rc"
exit $rc
