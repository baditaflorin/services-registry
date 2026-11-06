#!/usr/bin/env bash
# Switch ONE Woodpecker agent to (or back from) the shared cache override, without
# killing running pipelines.
#
#   apply-agent.sh --compose-dir DIR --compose-file FILE --service NAME --project NAME
#                  [--override FILE] [--max-wait SECONDS] [--stable SECONDS]
#                  [--rollback] [--dry-run]
#
# WHY THIS EXISTS: recreating the agent container kills every step container it is
# running (they exit 137) and the workflows fail as "canceled". A first dry run of
# this pilot did exactly that to a busy host. So this script:
#
#   1. renders the compose file with the override and refuses unless it manages
#      exactly one service and sets the volumes (skipped with --rollback);
#   2. WAITS until no pipeline step container is running on this host (label
#      wp_uuid, which every Woodpecker step container carries; it does not match a
#      different label by mistake) and stays that way for --stable seconds;
#      if that does not happen within --max-wait it exits 3 and changes NOTHING;
#   3. recreates only the agent service;
#   4. waits for the agent to be healthy and, if it is not, rolls back on its own.
#
# The wait narrows the race but cannot close it (a job may be assigned between the
# last check and the stop). To close it, first hold the agent with `no_schedule` in
# Woodpecker (an operator action in the Woodpecker UI or API, which needs an admin
# token, so it is not done by this script), wait for its running workflows to end,
# run this script, then clear the hold. The runbook has the exact sequence.
#
# --rollback recreates the agent from the base compose file alone (same waiting).
# --dry-run prints what it would do and runs no docker command that changes state.
set -euo pipefail

COMPOSE_DIR=""
COMPOSE_FILE=""
SERVICE=""
PROJECT=""
OVERRIDE="woodpecker-agent-ci-cache.override.yml"
MAX_WAIT=1800
STABLE=20
ROLLBACK=0
DRY_RUN=0
POLL="${CI_CACHE_POLL_SECONDS:-2}"
HEALTH_WAIT="${CI_CACHE_HEALTH_WAIT_SECONDS:-90}"

die() { echo "apply-agent.sh: $*" >&2; exit "${2:-2}"; }
log() { echo "apply-agent.sh: $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compose-dir) COMPOSE_DIR="${2:-}"; shift 2 ;;
    --compose-file) COMPOSE_FILE="${2:-}"; shift 2 ;;
    --service) SERVICE="${2:-}"; shift 2 ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --override) OVERRIDE="${2:-}"; shift 2 ;;
    --max-wait) MAX_WAIT="${2:-}"; shift 2 ;;
    --stable) STABLE="${2:-}"; shift 2 ;;
    --rollback) ROLLBACK=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "${COMPOSE_DIR}" && "${COMPOSE_DIR}" == /* ]] || die "--compose-dir (absolute) is required"
[[ "${COMPOSE_FILE}" =~ ^[A-Za-z0-9_.-]+$ ]] || die "--compose-file must be a plain file name"
[[ "${SERVICE}" =~ ^[A-Za-z0-9_-]+$ ]] || die "--service is required (letters, digits, _ -)"
[[ "${PROJECT}" =~ ^[A-Za-z0-9_-]+$ ]] || die "--project is required (letters, digits, _ -)"
[[ "${OVERRIDE}" =~ ^[A-Za-z0-9_.-]+$ ]] || die "--override must be a plain file name"
[[ "${MAX_WAIT}" =~ ^[0-9]+$ && "${STABLE}" =~ ^[0-9]+$ ]] || die "--max-wait and --stable must be integers"
[[ -f "${COMPOSE_DIR}/${COMPOSE_FILE}" ]] || die "${COMPOSE_DIR}/${COMPOSE_FILE} does not exist"
command -v docker >/dev/null 2>&1 || die "docker is not installed on this host"

cd "${COMPOSE_DIR}"
base=(docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}")
with=("${base[@]}" -f "${OVERRIDE}")
if (( ROLLBACK )); then target=("${base[@]}"); else target=("${with[@]}"); fi

if (( ! ROLLBACK )); then
  [[ -f "${OVERRIDE}" ]] || die "${COMPOSE_DIR}/${OVERRIDE} does not exist (run install-host.sh --apply first)"
  services="$("${with[@]}" config --services)"
  [[ "${services}" == "${SERVICE}" ]] || die "compose with the override manages '${services//$'\n'/ }', expected exactly '${SERVICE}'"
  "${with[@]}" config | grep -q 'WOODPECKER_BACKEND_DOCKER_VOLUMES:' \
    || die "the override does not set WOODPECKER_BACKEND_DOCKER_VOLUMES"
  log "compose renders exactly one service (${SERVICE}) with the cache volumes"
fi

running() { docker ps -q --filter label=wp_uuid | wc -l | tr -d ' '; }

if (( DRY_RUN )); then
  log "dry run: would wait for ${STABLE}s without any step container (label wp_uuid; now: $(running) running), up to ${MAX_WAIT}s"
  log "dry run: would run: ${target[*]} up -d ${SERVICE}"
  exit 0
fi

# 2. wait for a real idle window
start="$(date +%s)"
idle_since=""
while :; do
  now="$(date +%s)"
  if [[ "$(running)" == "0" ]]; then
    [[ -n "${idle_since}" ]] || idle_since="${now}"
    if (( now - idle_since >= STABLE )); then break; fi
  else
    idle_since=""
  fi
  if (( now - start > MAX_WAIT )); then
    die "no ${STABLE}s idle window within ${MAX_WAIT}s ($(running) step container(s) running); nothing was changed" 3
  fi
  sleep "${POLL}"
done
log "host idle for ${STABLE}s; recreating ${SERVICE}"

# 3. recreate only the agent
"${target[@]}" up -d "${SERVICE}"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "${COMPOSE_DIR}/.ci-cache-$( (( ROLLBACK )) && echo rollback || echo cutover )"

# 4. health, with automatic rollback
cid="$("${target[@]}" ps -q "${SERVICE}")"
healthy=0
deadline=$(( $(date +%s) + HEALTH_WAIT ))
while (( $(date +%s) <= deadline )); do
  state="$(docker inspect "${cid}" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || echo gone)"
  if [[ "${state}" == "healthy" || "${state}" == "running" ]]; then healthy=1; break; fi
  sleep "${POLL}"
done
if (( ! healthy )); then
  if (( ROLLBACK )); then die "agent is not healthy after the rollback (state: ${state}); investigate on the host" 4; fi
  log "agent not healthy within ${HEALTH_WAIT}s (state: ${state}); rolling back"
  "${base[@]}" up -d "${SERVICE}"
  die "rolled back to the base compose file because the agent did not become healthy" 4
fi
log "agent is up ($( (( ROLLBACK )) && echo 'without' || echo 'with' ) the cache override)"
if (( ! ROLLBACK )); then
  log "verify on the next step container: docker inspect \"\$(docker ps -q --filter label=wp_uuid | head -1)\" --format '{{range .HostConfig.Binds}}{{println .}}{{end}}'"
fi
