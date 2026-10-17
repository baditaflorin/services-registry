#!/usr/bin/env bash
# Prepare ONE Woodpecker agent host for the shared CI cache pilot.
#
#   install-host.sh [--root /var/cache/woodpecker-ci] [--min-free-gb 40] [--apply]
#
# Without --apply it only checks and prints what it would do. With --apply it
# creates the three cache directories. It never edits the agent's compose file,
# never restarts the agent and never touches credentials; it prints the exact
# commands for those steps so a person applies them deliberately.
#
# Checks: Docker present, the cache root's filesystem has enough free space, the
# agent really uses the Docker backend. See
# docs/runbooks/woodpecker-ci-go-cache-pilot.md for the whole procedure.
set -euo pipefail

ROOT="${WOODPECKER_CI_CACHE_ROOT:-/var/cache/woodpecker-ci}"
MIN_FREE_GB="${CI_CACHE_MIN_FREE_GB:-40}"
APPLY=0

die() { echo "install-host.sh: $*" >&2; exit 2; }
note() { echo "install-host.sh: $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --min-free-gb) MIN_FREE_GB="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "${ROOT}" == /* ]] || die "--root must be an absolute path"
[[ "${MIN_FREE_GB}" =~ ^[0-9]+$ ]] || die "--min-free-gb must be an integer"

command -v docker >/dev/null 2>&1 || die "docker is not installed on this host; run this on the agent host, not on a laptop"

# Free space on the filesystem that will hold the root (walk up to an existing parent).
probe="${ROOT}"
while [[ ! -d "${probe}" && "${probe}" != "/" ]]; do probe="$(dirname "${probe}")"; done
free_kb="$(df -Pk "${probe}" | awk 'NR==2 {print $4}')"
free_gb=$(( ${free_kb:-0} / 1024 / 1024 ))
note "cache root ${ROOT} lives on the filesystem of ${probe}: ${free_gb} GB free (need ${MIN_FREE_GB} GB)"
if (( free_gb < MIN_FREE_GB )); then
  die "not enough free disk (${free_gb} GB < ${MIN_FREE_GB} GB); pick another --root or free space first"
fi

if (( APPLY )); then
  for d in go-build go-mod npm; do
    install -d -m 0755 "${ROOT}/${d}"
  done
  note "created ${ROOT}/{go-build,go-mod,npm} (root owned, mode 0755: step containers run as root)"
else
  note "dry run: would create ${ROOT}/{go-build,go-mod,npm}; re-run with --apply"
fi

cat <<EOF

Next steps (run them yourself, on this host):

  1. Put this in the agent's .env (next to its docker-compose.yml):
       WOODPECKER_CI_CACHE_ROOT=${ROOT}

  2. Copy templates/woodpecker-agent-ci-cache.override.yml next to the base
     compose file and recreate ONLY the agent:
       docker compose -f docker-compose.yml -f woodpecker-agent-ci-cache.override.yml \\
         up -d woodpecker-cross-host-agent

  3. Prove the setting took effect (a step container must list the three binds):
       docker inspect \$(docker ps -q --filter label=com.woodpecker-ci.step | head -1) \\
         --format '{{range .HostConfig.Binds}}{{println .}}{{end}}'

  4. Keep it under its cap (run daily, e.g. from cron or a systemd timer):
       scripts/ci-cache/prune.sh --root ${ROOT}

  Roll back: stop passing the override file and recreate the agent. Delete
  ${ROOT} whenever you like; the next run is a cold run.
EOF
