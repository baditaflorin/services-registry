#!/usr/bin/env bash
# Prepare ONE Woodpecker agent host for the shared CI cache pilot.
#
#   install-host.sh [--root /var/cache/woodpecker-ci] [--min-free-gb 40]
#                   [--compose-dir DIR --compose-file FILE --service NAME [--project NAME]]
#                   [--apply]
#
# Without --apply it only checks and prints what it would do. With --apply it
# creates the three cache directories and, when --compose-dir is given, writes the
# compose override for THAT host's agent service. Hosts differ (the file may be
# compose.yml, the service woodpecker-agent, the project own-woodpecker-agent), so
# the service name is validated against the compose file: a typo can never create a
# second, bogus service. It never edits the agent's own compose file, never restarts
# the agent and never touches credentials; it prints the exact commands for those
# steps so a person applies them deliberately.
#
# Checks: Docker present, the cache root's filesystem has enough free space. See
# docs/runbooks/woodpecker-ci-go-cache-pilot.md for the whole procedure.
set -euo pipefail

ROOT="${WOODPECKER_CI_CACHE_ROOT:-/var/cache/woodpecker-ci}"
MIN_FREE_GB="${CI_CACHE_MIN_FREE_GB:-40}"
COMPOSE_DIR=""
COMPOSE_FILE="docker-compose.yml"
SERVICE="woodpecker-cross-host-agent"
PROJECT=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/../../templates/woodpecker-agent-ci-cache.override.yml"
OVERRIDE_NAME="woodpecker-agent-ci-cache.override.yml"
APPLY=0

die() { echo "install-host.sh: $*" >&2; exit 2; }
note() { echo "install-host.sh: $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --min-free-gb) MIN_FREE_GB="${2:-}"; shift 2 ;;
    --compose-dir) COMPOSE_DIR="${2:-}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:-}"; shift 2 ;;
    --service) SERVICE="${2:-}"; shift 2 ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --template) TEMPLATE="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "${ROOT}" == /* ]] || die "--root must be an absolute path"
[[ "${MIN_FREE_GB}" =~ ^[0-9]+$ ]] || die "--min-free-gb must be an integer"
[[ "${SERVICE}" =~ ^[A-Za-z0-9_-]+$ ]] || die "--service must be a compose service name (letters, digits, _ -)"
[[ -z "${PROJECT}" || "${PROJECT}" =~ ^[A-Za-z0-9_-]+$ ]] || die "--project must be a compose project name"
[[ "${COMPOSE_FILE}" =~ ^[A-Za-z0-9_.-]+$ ]] || die "--compose-file must be a plain file name inside --compose-dir"

if [[ -n "${COMPOSE_DIR}" ]]; then
  [[ "${COMPOSE_DIR}" == /* ]] || die "--compose-dir must be an absolute path"
  [[ -f "${COMPOSE_DIR}/${COMPOSE_FILE}" ]] || die "${COMPOSE_DIR}/${COMPOSE_FILE} does not exist"
  grep -qE "^  ${SERVICE}:[[:space:]]*$" "${COMPOSE_DIR}/${COMPOSE_FILE}" \
    || die "service '${SERVICE}' is not defined in ${COMPOSE_DIR}/${COMPOSE_FILE}; an override naming it would create a second, bogus service"
  [[ -f "${TEMPLATE}" ]] || die "override template not found: ${TEMPLATE} (pass --template)"
fi

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

# The template names the registry's example service; render it for this host's real one.
render_override() { sed "s/^  woodpecker-cross-host-agent:/  ${SERVICE}:/" "${TEMPLATE}"; }

if (( APPLY )); then
  for d in go-build go-mod npm; do
    install -d -m 0755 "${ROOT}/${d}"
  done
  note "created ${ROOT}/{go-build,go-mod,npm} (root owned, mode 0755: step containers run as root)"
  if [[ -n "${COMPOSE_DIR}" ]]; then
    target="${COMPOSE_DIR}/${OVERRIDE_NAME}"
    if [[ -e "${target}" ]] && ! render_override | cmp -s - "${target}"; then
      die "${target} already exists and differs; refusing to overwrite it"
    fi
    render_override > "${target}.tmp"
    chmod 0600 "${target}.tmp"
    mv "${target}.tmp" "${target}"
    note "wrote ${target} for service '${SERVICE}' (mode 0600)"
  fi
else
  note "dry run: would create ${ROOT}/{go-build,go-mod,npm}; re-run with --apply"
  if [[ -n "${COMPOSE_DIR}" ]]; then
    note "dry run: would write ${COMPOSE_DIR}/${OVERRIDE_NAME} for service '${SERVICE}'"
  fi
fi

# Commands printed for a person to run, with this host's real names.
pflag=""
if [[ -n "${PROJECT}" ]]; then pflag="-p ${PROJECT} "; fi
cdprefix=""
if [[ -n "${COMPOSE_DIR}" ]]; then cdprefix="cd ${COMPOSE_DIR} && "; fi
cf="${COMPOSE_FILE}"
if [[ -z "${COMPOSE_DIR}" ]]; then
  note "no --compose-dir given: the commands below use the registry template's names; pass --compose-dir/--compose-file/--service/--project to get this host's"
fi

applyflags="--compose-dir ${COMPOSE_DIR:-<dir>} --compose-file ${cf} --service ${SERVICE} --project ${PROJECT:-<project>}"

cat <<EOF

Next steps (run them yourself, on this host). Recreating the agent kills every step it
is running, so the agent is held first and only then switched:

  1. Check what compose will run (look for the three binds on ${SERVICE}):
       ${cdprefix}docker compose ${pflag}-f ${cf} -f ${OVERRIDE_NAME} config | grep -A1 BACKEND_DOCKER_VOLUMES

  2. HOLD the agent in Woodpecker so it takes no new work (an admin action in the Woodpecker
     UI under Admin > Agents > this agent > "No schedule", or through its API; this script
     cannot do it because it needs an admin token). Then wait until it has no running
     workflow:   fleet-runner woodpecker queue --plane 0exec   (its name must be absent
     from the RUNNING table).

  3. Switch the agent. This waits for a real idle window on the host (no container with the
     label wp_uuid), changes nothing if none comes, recreates ONLY the agent, and rolls back
     on its own if the agent is not healthy:
       scripts/ci-cache/apply-agent.sh ${applyflags}

  4. Release the hold (clear "No schedule") and note the cutover time it wrote to
     ${COMPOSE_DIR:-<dir>}/.ci-cache-cutover (UTC) for the report.

  5. Prove the mounts reached the steps: a step container started after the switch must
     list the three binds:
       docker inspect "\$(docker ps -q --filter label=wp_uuid | head -1)" \\
         --format '{{range .HostConfig.Binds}}{{println .}}{{end}}'

  6. Keep it under its cap (run daily, from cron or a systemd timer; ready-made units:
     templates/woodpecker-ci-cache-prune.service and .timer):
       scripts/ci-cache/prune.sh --root ${ROOT}

  Roll back (same hold first):  scripts/ci-cache/apply-agent.sh ${applyflags} --rollback
  Then delete ${ROOT} whenever you like. The next run is a cold run.
EOF
