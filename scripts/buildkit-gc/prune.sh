#!/usr/bin/env bash
# Prune only BuildKit's own unused cache, never Docker volumes or CI caches.
set -euo pipefail
container="${BUILDKIT_GC_CONTAINER:-buildx_buildkit_own-amd64-builder0}"
psi="${BUILDKIT_GC_PSI_FILE:-/proc/pressure/io}"
max_io="${BUILDKIT_GC_MAX_IO_FULL:-5}"
keep_mb="${BUILDKIT_GC_KEEP_MB:-80000}"
keep_age="${BUILDKIT_GC_KEEP_DURATION:-168h}"
lock="${BUILDKIT_GC_LOCK:-/run/lock/0own-buildkit-gc.lock}"

[[ "$container" =~ ^[a-zA-Z0-9_.-]+$ ]] || { echo 'invalid container name' >&2; exit 2; }
[[ "$max_io" =~ ^[0-9]+([.][0-9]+)?$ && "$keep_mb" =~ ^[1-9][0-9]*$ && "$keep_age" =~ ^[1-9][0-9]*h$ ]] || { echo 'invalid GC limits' >&2; exit 2; }
exec 9>"$lock"
command -v flock >/dev/null || { echo 'flock is unavailable' >&2; exit 1; }
flock -n -E 75 9 || { status=$?; if [[ "$status" == 75 ]]; then echo 'another BuildKit prune is running; deferring'; exit 0; fi; exit "$status"; }
[[ -r "$psi" ]] || { echo 'I/O pressure unavailable; refusing prune' >&2; exit 1; }
io=$(awk '$1 == "full" {for(i=2;i<=NF;i++) if ($i ~ /^avg10=/) {sub(/^avg10=/,"",$i); print $i; exit}}' "$psi")
[[ "$io" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo 'invalid I/O pressure; refusing prune' >&2; exit 1; }
if ! awk -v io="$io" -v limit="$max_io" 'BEGIN {exit !(io < limit)}'; then
  echo "I/O full avg10 ${io}% >= ${max_io}%; deferring prune"
  exit 0
fi
running=$(docker ps --filter name='^/wp_' --format '{{.Names}}')
if [[ -n "$running" ]]; then
  echo 'Woodpecker steps running; deferring prune'
  exit 0
fi
[[ "$(docker inspect --format '{{.State.Running}}' "$container")" == true ]] || { echo 'BuildKit container is not running' >&2; exit 1; }
echo "Pruning unused BuildKit cache older than ${keep_age}, retaining ${keep_mb} MB; I/O full avg10 ${io}%"
docker exec "$container" buildctl prune --keep-duration "$keep_age" --keep-storage "$keep_mb"
