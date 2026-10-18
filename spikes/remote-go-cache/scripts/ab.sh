#!/bin/bash
# One build+test of go-common per case, each in a fresh container with a fresh local cache.
cd /root/gocached-spike
run() { # run <label> <extra docker args> -- <GOCACHEPROG value or empty>
  local label="$1" prog="$2" extra="${3:-}"
  local env=(-e GOMODCACHE=/w/mod)
  if [ -n "$prog" ]; then env+=(-e "GOCACHEPROG=$prog"); else env+=(-e GOCACHE=/tmp/gc-$label); fi
  docker run --rm --network host --cpus 4 --memory 6g $extra -v /root/gocached-spike/bin:/opt:ro -v /root/gocached-spike/work:/w "${env[@]}" -w /w/gc golang:1.25 bash -c '
    s=$(date +%s); go build ./... ; b=$?; go test ./... >/tmp/t.log 2>&1; t=$?; e=$(date +%s)
    echo "RESULT '"$label"' seconds=$((e-s)) build_rc=$b test_rc=$t peak_mib=$(( $(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0) / 1048576 ))"
    grep -c "(cached)" /tmp/t.log | sed "s/^/  cached test packages: /"' 2>&1 | grep -E 'RESULT|cacher:|cached test|closing' 
}
S="http://127.0.0.1:18335"
P() { echo "/opt/go-cacher --cache-server=$S --cache-dir=/tmp/lc-$1 --verbose"; }
run R0-baseline-no-cache ""
run R1-remote-empty-fill "$(P r1)"
run R2-remote-warm-fresh-client "$(P r2)"
run R3-same-again-local-warm "$(P r2)" 
echo "--- failure tests"
docker stop gocached-spike >/dev/null
run R4-server-DOWN-fail-open "/opt/go-cacher --cache-server=$S --cache-dir=/tmp/lc-r4 --verbose"
docker start gocached-spike >/dev/null; sleep 4
( sleep 6; docker stop gocached-spike >/dev/null ) &
run R5-server-killed-mid-build "/opt/go-cacher --cache-server=$S --cache-dir=/tmp/lc-r5 --verbose"
wait; docker start gocached-spike >/dev/null
echo ALL-DONE
