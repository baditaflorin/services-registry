#!/usr/bin/env bash
# Controlled before/after benchmark of the CI cache on THIS host, no Woodpecker API
# needed. It runs the fleet's canonical Go CI commands (go build ./... && go test
# ./...) in the same image the pipelines use, three ways:
#
#   cold        empty caches                      = what every run costs today
#   warm        the caches the cold run filled     = a re-run of the same commit
#   warm-other  caches filled by a DIFFERENT repo  = a repo's first run after the
#               fleet has already built its shared dependencies (needs --other-src)
#
# and records wall seconds and the container's peak memory for each.
#
#   bench.sh --src /path/to/checkout [--other-src /path/to/other/checkout]
#            [--image golang:1.25] [--cmd 'go build ./... && go test ./...']
#            [--dry-run]
#
# The benchmark uses throw-away cache directories under a temp dir, so it never
# touches the pilot's real cache. Peak memory is read from the container's cgroup
# (memory.peak on cgroup v2, max_usage_in_bytes on v1); "na" if the kernel has
# neither. Run it on the agent host (it needs docker), from a checkout you already
# have (it never needs GitHub credentials). The checkout is copied first because
# `go test` can write files.
set -euo pipefail

SRC=""
OTHER=""
IMAGE="golang:1.25"
CMD='go build ./... && go test ./...'
DRY_RUN=0

die() { echo "bench.sh: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) SRC="${2:-}"; shift 2 ;;
    --other-src) OTHER="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    --cmd) CMD="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "${SRC}" ]] || die "--src is required"
[[ -d "${SRC}" ]] || die "--src ${SRC} is not a directory"
[[ -f "${SRC}/go.mod" ]] || die "--src ${SRC} has no go.mod (point it at a Go module's root)"
if [[ -n "${OTHER}" ]]; then
  [[ -f "${OTHER}/go.mod" ]] || die "--other-src ${OTHER} has no go.mod"
fi
if (( ! DRY_RUN )); then
  command -v docker >/dev/null 2>&1 || die "docker is required (run this on the agent host, or use --dry-run)"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ci-cache-bench.XXXXXX")"
cleanup() {
  local rc=$?  # keep the script's own exit status; the trap must not mask a failure
  rm -rf "${WORK}" 2>/dev/null || { chmod -R u+w "${WORK}" 2>/dev/null || true; rm -rf "${WORK}" || true; }
  exit "${rc}"
}
trap cleanup EXIT

# One run: run_case <label> <checkout> <cache_dir>
RESULTS=()
run_case() {
  local label="$1" checkout="$2" cache="$3"
  local copy="${WORK}/src-${label}"
  mkdir -p "${cache}/go-build" "${cache}/go-mod" "${cache}/npm"
  local inner
  inner="${CMD}; rc=\$?; echo PEAK_BYTES=\$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || cat /sys/fs/cgroup/memory/memory.max_usage_in_bytes 2>/dev/null || echo na); exit \$rc"
  local -a docker_cmd=(docker run --rm
    -v "${copy}:/src" -w /src
    -v "${cache}/go-build:/root/.cache/go-build"
    -v "${cache}/go-mod:/go/pkg/mod"
    "${IMAGE}" sh -c "${inner}")
  if (( DRY_RUN )); then
    echo "[dry-run] ${label}: cp -R ${checkout} ${copy}"
    echo "[dry-run] ${label}: ${docker_cmd[*]}"
    RESULTS+=("${label}|na|na|na")
    return 0
  fi
  cp -R "${checkout}" "${copy}"
  local start end out rc=0
  start="$(date +%s)"
  out="$("${docker_cmd[@]}" 2>&1)" || rc=$?
  end="$(date +%s)"
  local peak
  peak="$(printf '%s\n' "${out}" | sed -n 's/^PEAK_BYTES=//p' | tail -1)"
  [[ -n "${peak}" ]] || peak="na"
  if [[ "${peak}" =~ ^[0-9]+$ ]]; then peak="$(( peak / 1024 / 1024 ))"; fi
  RESULTS+=("${label}|$(( end - start ))|${peak}|${rc}")
  if (( rc != 0 )); then
    echo "bench.sh: ${label} run failed (exit ${rc}); last lines:" >&2
    printf '%s\n' "${out}" | tail -15 >&2
  fi
}

CACHE_A="${WORK}/cache-a"
run_case cold "${SRC}" "${CACHE_A}"
run_case warm "${SRC}" "${CACHE_A}"
if [[ -n "${OTHER}" ]]; then
  # fill a cache from the OTHER repo, then build SRC against it
  CACHE_B="${WORK}/cache-b"
  run_case prime-other "${OTHER}" "${CACHE_B}"
  run_case warm-other "${SRC}" "${CACHE_B}"
fi

printf '\n%-12s %10s %14s %6s\n' case seconds peak_mem_MiB exit
for row in "${RESULTS[@]}"; do
  IFS='|' read -r label secs peak rc <<<"${row}"
  printf '%-12s %10s %14s %6s\n' "${label}" "${secs}" "${peak}" "${rc}"
done
echo
echo "image=${IMAGE} cmd='${CMD}'"
echo "Read it as: cold is today's cost; warm is a re-run of the same commit; warm-other is a repo's first run once the fleet has warmed the shared dependencies."
