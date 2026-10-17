#!/usr/bin/env bash
# Keep the shared CI caches on one Woodpecker agent host under a disk cap.
#
#   prune.sh --root /var/cache/woodpecker-ci [--go-build-max-mb N]
#            [--go-mod-max-mb N] [--npm-max-mb N] [--mod-min-age-days D] [--dry-run]
#
# Safe to run while pipelines run: it only deletes whole files or whole module
# directories that have not been touched for a while, oldest first, and a missing
# cache entry is just a cache miss for the Go build cache and for npm. The Go
# module cache is different (a build reads extracted module directories), so it
# is only pruned by age, never below --mod-min-age-days (default 14).
#
# Go itself already trims build cache entries unused for 5 days; this script is
# the hard cap on top of that, and the only pruning for the module and npm caches.
#
# Portable on purpose (find -mtime, du -sk, no GNU-only flags) so it can be tested
# on a laptop. Exit status: 0 ok, 2 usage or refused root.
set -euo pipefail

ROOT=""
GO_BUILD_MAX_MB=8192
GO_MOD_MAX_MB=6144
NPM_MAX_MB=3072
MOD_MIN_AGE_DAYS=14
DRY_RUN=0

die() { echo "prune.sh: $*" >&2; exit 2; }
log() { echo "prune.sh: $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --go-build-max-mb) GO_BUILD_MAX_MB="${2:-}"; shift 2 ;;
    --go-mod-max-mb) GO_MOD_MAX_MB="${2:-}"; shift 2 ;;
    --npm-max-mb) NPM_MAX_MB="${2:-}"; shift 2 ;;
    --mod-min-age-days) MOD_MIN_AGE_DAYS="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# Never run rm against a surprising path.
[[ -n "${ROOT}" ]] || die "--root is required"
[[ "${ROOT}" == /* ]] || die "--root must be an absolute path"
case "${ROOT}" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/cache)
    die "refusing to prune ${ROOT}: it is not a dedicated cache directory" ;;
esac
[[ -d "${ROOT}" ]] || die "${ROOT} does not exist"
for v in "${GO_BUILD_MAX_MB}" "${GO_MOD_MAX_MB}" "${NPM_MAX_MB}" "${MOD_MIN_AGE_DAYS}"; do
  [[ "${v}" =~ ^[0-9]+$ ]] || die "limits and ages must be non-negative integers (got '${v}')"
done

size_mb() { # size_mb <dir> -> megabytes rounded UP (so "over the cap" never hides a partial MB), 0 if missing
  [[ -d "$1" ]] || { echo 0; return; }
  local kb
  kb="$(du -sk "$1" 2>/dev/null | awk '{print $1}')"
  echo $(( (${kb:-0} + 1023) / 1024 ))
}

# trim_by_age <dir> <max_mb>: delete files older than 30, 14, 7, 3, 1, then 0 days,
# stopping as soon as the directory is under the cap. A missing file is a cache miss.
trim_by_age() {
  local dir="$1" max_mb="$2" days current
  [[ -d "${dir}" ]] || return 0
  current="$(size_mb "${dir}")"
  if (( current <= max_mb )); then
    log "${dir}: ${current} MB <= ${max_mb} MB, nothing to do"
    return 0
  fi
  for days in 30 14 7 3 1 0; do
    if (( DRY_RUN )); then
      local n
      n="$(find "${dir}" -type f -mtime +"${days}" 2>/dev/null | wc -l | tr -d ' ')"
      log "${dir}: ${current} MB > ${max_mb} MB; would delete ${n} file(s) older than ${days} day(s) (dry run)"
      continue
    fi
    find "${dir}" -type f -mtime +"${days}" -delete 2>/dev/null || true
    find "${dir}" -mindepth 1 -type d -empty -delete 2>/dev/null || true
    current="$(size_mb "${dir}")"
    log "${dir}: deleted files older than ${days} day(s); now ${current} MB (cap ${max_mb} MB)"
    if (( current <= max_mb )); then return 0; fi
  done
  (( DRY_RUN )) || log "${dir}: still ${current} MB after deleting everything older than 0 days; cap ${max_mb} MB"
}

# The module cache is read-only on disk (Go makes it so) and a running build reads
# the extracted directories, so only remove what is old, and make it writable first.
prune_modcache() {
  local dir="$1" max_mb="$2" current
  [[ -d "${dir}" ]] || return 0
  current="$(size_mb "${dir}")"
  if (( current <= max_mb )); then
    log "${dir}: ${current} MB <= ${max_mb} MB, nothing to do"
    return 0
  fi
  if (( DRY_RUN )); then
    log "${dir}: ${current} MB > ${max_mb} MB; would delete module directories and downloads older than ${MOD_MIN_AGE_DAYS} day(s) (dry run)"
    return 0
  fi
  # Extracted modules look like <host>/<owner>/<repo>@v1.2.3 and are read-only, so each
  # one is made writable and removed in that order (two -exec batches in one find do
  # not guarantee the chmod runs before the rm).
  local entry
  while IFS= read -r -d '' entry; do
    chmod -R u+w "${entry}" 2>/dev/null || true
    rm -rf "${entry}" || true
  done < <(find "${dir}" -type d -name '*@v*' -mtime +"${MOD_MIN_AGE_DAYS}" -prune -print0 2>/dev/null)
  if [[ -d "${dir}/cache/download" ]]; then
    while IFS= read -r -d '' entry; do
      chmod u+w "${entry}" 2>/dev/null || true
      rm -f "${entry}" || true
    done < <(find "${dir}/cache/download" -type f -mtime +"${MOD_MIN_AGE_DAYS}" -print0 2>/dev/null)
  fi
  current="$(size_mb "${dir}")"
  log "${dir}: pruned entries older than ${MOD_MIN_AGE_DAYS} day(s); now ${current} MB (cap ${max_mb} MB)"
}

trim_by_age "${ROOT}/go-build" "${GO_BUILD_MAX_MB}"
prune_modcache "${ROOT}/go-mod" "${GO_MOD_MAX_MB}"
trim_by_age "${ROOT}/npm" "${NPM_MAX_MB}"
log "done: go-build $(size_mb "${ROOT}/go-build") MB, go-mod $(size_mb "${ROOT}/go-mod") MB, npm $(size_mb "${ROOT}/npm") MB"
