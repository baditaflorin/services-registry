#!/usr/bin/env bash
# Keep the shared CI caches on one Woodpecker agent host under a disk cap.
#
#   prune.sh --root /var/cache/woodpecker-ci [--go-build-max-mb N]
#            [--go-mod-max-mb N] [--npm-max-mb N] [--mod-min-age-days D] [--prom FILE] [--dry-run]
#
# Safe to run while pipelines run. A missing Go build-cache or npm entry is just a
# cache miss. This script never touches Docker images or volumes, and it never
# deletes in-progress temp files (tmp*, *.tmp, *.temp, *.partial, or a file whose
# mtime is under 30 seconds). The Go module cache is different (a build reads
# extracted module directories), so it is only pruned by age, never below
# --mod-min-age-days (default 14).
#
# go-build and npm, when over their cap, are trimmed by size: oldest first by
# atime, then mtime, down to a low-water mark of 80% of the cap, so the next
# day's writes have some room. A cache already at or under its cap is left alone
# even if it is above that 80% mark. Age-only deletion cannot enforce the cap
# when every file is from the last day.
#
# A cache that is still over its cap after everything the script may delete is a
# failure, not a quiet log line: the script prints ALERT on stderr and exits 3.
# --prom FILE writes a node_exporter textfile (replaced atomically, never in a
# dry run) with the size, the cap and an over-cap flag of each cache and the
# time of the run, for templates/prometheus-ci-cache-prune.rules.yml.
#
# Portable on GNU/Linux (find -printf, du -sk). Exit status: 0 ok, 2 usage or
# refused root, 3 a cache is still over its cap.
set -euo pipefail

ROOT=""
PROM=""
# A working day's go-build cache measured 14 to 27 GB on the pilot host (11-2026); 8 GB could not be
# reached by age alone, so the prune ended above its cap every night.
GO_BUILD_MAX_MB=28672
GO_MOD_MAX_MB=6144
NPM_MAX_MB=3072
MOD_MIN_AGE_DAYS=14
# After a cap breach, delete until this fraction of the cap remains (integer percent).
LOW_WATER_PCT=80
# Skip files written this recently: they may still be mid-rename.
FRESH_SECONDS=30
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
    --prom) PROM="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
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
[[ -z "${PROM}" || "${PROM}" == /* ]] || die "--prom must be an absolute path"
for v in "${GO_BUILD_MAX_MB}" "${GO_MOD_MAX_MB}" "${NPM_MAX_MB}" "${MOD_MIN_AGE_DAYS}"; do
  [[ "${v}" =~ ^[0-9]+$ ]] || die "limits and ages must be non-negative integers (got '${v}')"
done

size_kb() { # size_kb <dir> -> kilobytes as reported by du, 0 if missing
  [[ -d "$1" ]] || { echo 0; return; }
  local kb
  kb="$(du -sk "$1" 2>/dev/null | awk '{print $1}')"
  echo "${kb:-0}"
}

size_mb() { # size_mb <dir> -> megabytes rounded UP (so "over the cap" never hides a partial MB), 0 if missing
  local kb
  kb="$(size_kb "$1")"
  echo $(( (kb + 1023) / 1024 ))
}

# List regular cache files oldest-first (atime, then mtime). Skip temp/in-progress
# names and files whose mtime is under FRESH_SECONDS. Never follows symlinks, never
# crosses a filesystem boundary. Output: "<atime> <mtime> <kb> <path>" per line.
list_prunable_files() {
  local dir="$1" cutoff="$2"
  # -P: do not follow symlinks. -type f: regular files only (not a planted symlink).
  find -P "${dir}" -xdev -type f \
    ! -name 'tmp*' ! -name '*.tmp' ! -name '*.temp' ! -name '*.partial' \
    ! -name 'trim.txt' ! -name 'README' \
    -printf '%A@\t%T@\t%k\t%p\n' 2>/dev/null \
  | awk -F '\t' -v cutoff="${cutoff}" 'NF>=4 && $2+0<=cutoff {print}' \
  | sort -n -k1,1 -k2,2
}

# trim_to_size <dir> <max_mb>: if the directory is over the cap, delete the oldest
# eligible files until it is at or under 80% of the cap (the low-water mark). A
# directory already at or under the cap is left alone.
trim_to_size() {
  local dir="$1" max_mb="$2"
  local cap_kb target_kb target_mb current_kb current now cutoff pass
  [[ -d "${dir}" ]] || return 0
  # The tree is writable by containers: never follow a symlinked cache directory.
  if [[ -L "${dir}" ]]; then log "refusing symlinked ${dir}"; return 0; fi
  cap_kb=$(( max_mb * 1024 ))
  target_kb=$(( max_mb * 1024 * LOW_WATER_PCT / 100 ))
  target_mb=$(( max_mb * LOW_WATER_PCT / 100 ))
  current_kb="$(size_kb "${dir}")"
  current="$(size_mb "${dir}")"
  if (( current_kb <= cap_kb )); then
    log "${dir}: ${current} MB <= ${max_mb} MB, nothing to do"
    return 0
  fi

  now="$(date +%s)"
  cutoff=$(( now - FRESH_SECONDS ))

  if (( DRY_RUN )); then
    local n=0 would_kb=0 atime mtime k path remain
    remain="${current_kb}"
    while IFS=$'\t' read -r atime mtime k path; do
      [[ -n "${path}" ]] || continue
      (( remain <= target_kb )) && break
      n=$(( n + 1 ))
      would_kb=$(( would_kb + k ))
      remain=$(( remain - k ))
    done < <(list_prunable_files "${dir}" "${cutoff}")
    log "${dir}: ${current} MB > ${max_mb} MB; would delete ${n} file(s) (~${would_kb} KB) down to the ${target_mb} MB low-water mark (80% of cap) (dry run)"
    return 0
  fi

  # A few passes: concurrent writers and 1K-block vs du differences can leave a
  # remainder after the first estimated pass. Stop as soon as we are at or under
  # the low-water mark, or when a pass deletes nothing more.
  for pass in 1 2 3; do
    current_kb="$(size_kb "${dir}")"
    current="$(size_mb "${dir}")"
    if (( current_kb <= target_kb )); then
      break
    fi
    now="$(date +%s)"
    cutoff=$(( now - FRESH_SECONDS ))
    local deleted=0 deleted_kb=0 remain atime mtime k path
    remain="${current_kb}"
    while IFS=$'\t' read -r atime mtime k path; do
      [[ -n "${path}" ]] || continue
      (( remain <= target_kb )) && break
      # A container could have replaced the file with a symlink since we listed it.
      [[ -f "${path}" && ! -L "${path}" ]] || continue
      rm -f -- "${path}" || true
      deleted=$(( deleted + 1 ))
      deleted_kb=$(( deleted_kb + k ))
      remain=$(( remain - k ))
    done < <(list_prunable_files "${dir}" "${cutoff}")
    find -P "${dir}" -xdev -mindepth 1 -depth -type d -empty -delete 2>/dev/null || true
    current="$(size_mb "${dir}")"
    log "${dir}: trimmed by size (oldest first, pass ${pass}): deleted ${deleted} file(s) (~${deleted_kb} KB); now ${current} MB (cap ${max_mb} MB, low-water ${target_mb} MB)"
    if (( deleted == 0 )); then
      break
    fi
  done

  current="$(size_mb "${dir}")"
  current_kb="$(size_kb "${dir}")"
  if (( current_kb > cap_kb )); then
    log "${dir}: still ${current} MB after size trim (oldest first, skipped temp and files newer than ${FRESH_SECONDS}s); cap ${max_mb} MB"
  fi
}

# The module cache is read-only on disk (Go makes it so) and a running build reads
# the extracted directories, so only remove what is old, and make it writable first.
prune_modcache() {
  local dir="$1" max_mb="$2" current
  [[ -d "${dir}" ]] || return 0
  if [[ -L "${dir}" ]]; then log "refusing symlinked ${dir}"; return 0; fi
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
    [[ -L "${entry}" ]] && continue  # a container could swap a directory for a symlink
    chmod -R u+w "${entry}" 2>/dev/null || true
    rm -rf "${entry}" || true
  done < <(find "${dir}" -type d -name '*@v*' -mtime +"${MOD_MIN_AGE_DAYS}" -prune -print0 2>/dev/null)
  if [[ -d "${dir}/cache/download" ]]; then
    while IFS= read -r -d '' entry; do
      [[ -L "${entry}" ]] && continue
      chmod u+w "${entry}" 2>/dev/null || true
      rm -f "${entry}" || true
    done < <(find "${dir}/cache/download" -type f -mtime +"${MOD_MIN_AGE_DAYS}" -print0 2>/dev/null)
  fi
  current="$(size_mb "${dir}")"
  log "${dir}: pruned entries older than ${MOD_MIN_AGE_DAYS} day(s); now ${current} MB (cap ${max_mb} MB)"
}

trim_to_size "${ROOT}/go-build" "${GO_BUILD_MAX_MB}"
prune_modcache "${ROOT}/go-mod" "${GO_MOD_MAX_MB}"
trim_to_size "${ROOT}/npm" "${NPM_MAX_MB}"

go_build_mb="$(size_mb "${ROOT}/go-build")"
go_mod_mb="$(size_mb "${ROOT}/go-mod")"
npm_mb="$(size_mb "${ROOT}/npm")"
log "done: go-build ${go_build_mb} MB, go-mod ${go_mod_mb} MB, npm ${npm_mb} MB"

# A dry run deletes nothing, so it has no say on whether a cap can be reached.
if (( DRY_RUN )); then exit 0; fi

cache_rows() { # one "<name> <size_mb> <cap_mb>" row per cache, from the sizes measured after the prune
  echo "go-build ${go_build_mb} ${GO_BUILD_MAX_MB}"
  echo "go-mod ${go_mod_mb} ${GO_MOD_MAX_MB}"
  echo "npm ${npm_mb} ${NPM_MAX_MB}"
}

# write_prom: the textfile is replaced atomically (a scrape never sees half a file). node_exporter
# only reads *.prom, so the dot-prefixed temp file is invisible to it. A file that cannot be written
# is a warning, not a prune failure: the stale-run rule catches a metric that stops arriving.
write_prom() {
  local dir tmp name size cap over
  dir="$(dirname "${PROM}")"
  if [[ ! -d "${dir}" || ! -w "${dir}" ]]; then
    echo "prune.sh: WARNING: cannot write ${PROM}: ${dir} is not a writable directory" >&2
    return 0
  fi
  tmp="$(mktemp "${dir}/.ci-cache-prune.XXXXXX")" || { echo "prune.sh: WARNING: cannot create a temp file in ${dir}" >&2; return 0; }
  {
    echo "# HELP ci_cache_prune_last_run_timestamp_seconds Unix time when prune.sh last finished a real (not dry) run."
    echo "# TYPE ci_cache_prune_last_run_timestamp_seconds gauge"
    echo "ci_cache_prune_last_run_timestamp_seconds $(date +%s)"
    echo "# HELP ci_cache_prune_size_bytes Size of a shared CI cache after the prune, rounded up to a whole MB."
    echo "# TYPE ci_cache_prune_size_bytes gauge"
    while read -r name size cap; do echo "ci_cache_prune_size_bytes{cache=\"${name}\"} $(( size * 1048576 ))"; done < <(cache_rows)
    echo "# HELP ci_cache_prune_cap_bytes Disk cap the prune aims for."
    echo "# TYPE ci_cache_prune_cap_bytes gauge"
    while read -r name size cap; do echo "ci_cache_prune_cap_bytes{cache=\"${name}\"} $(( cap * 1048576 ))"; done < <(cache_rows)
    echo "# HELP ci_cache_prune_over_cap 1 when the cache is still above its cap after pruning everything the script may delete."
    echo "# TYPE ci_cache_prune_over_cap gauge"
    while read -r name size cap; do
      over=0; if (( size > cap )); then over=1; fi
      echo "ci_cache_prune_over_cap{cache=\"${name}\"} ${over}"
    done < <(cache_rows)
  } > "${tmp}"
  chmod 0644 "${tmp}" && mv -f "${tmp}" "${PROM}" || { rm -f "${tmp}"; echo "prune.sh: WARNING: could not replace ${PROM}" >&2; }
}

if [[ -n "${PROM}" ]]; then write_prom; fi

OVER=""
while read -r name size cap; do
  if (( size > cap )); then OVER="${OVER} ${name}"; fi
done < <(cache_rows)
if [[ -n "${OVER}" ]]; then
  echo "prune.sh: ALERT: still over the cap after deleting everything allowed:${OVER}; exiting 3 so the unit shows as failed" >&2
  exit 3
fi
