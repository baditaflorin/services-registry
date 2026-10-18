#!/bin/bash
cd /root/gocached-spike
run() { # run <label> <server-url> <network-args>
  local label="$1" server="$2" net="${3:---network host}"
  docker run --rm $net --cpus 4 --memory 6g -v /root/gocached-spike/bin:/opt:ro -v /root/gocached-spike/work:/w \
    -e GOMODCACHE=/w/mod -e "GOCACHEPROG=/opt/ci-gocache --cache-server=$server --cache-dir=/tmp/lc --verbose" -w /w/gc golang:1.25 bash -c '
    s=$(date +%s); go build ./... ; b=$?; go test ./... >/tmp/t.log 2>&1; t=$?; e=$(date +%s)
    echo "RESULT '"$label"' seconds=$((e-s)) build_rc=$b test_rc=$t peak_mib=$(( $(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0) / 1048576 ))"' 2>&1 | grep -E 'RESULT|ci-gocache:'
}
S="http://127.0.0.1:18335"
echo "--- server warm (it already holds go-common from the earlier run)"
run T1-warm-fresh-client "$S"
echo "--- server DOWN (connection refused)"
docker stop gocached-spike >/dev/null
run T2-server-down "$S"
echo "--- server BLACK-HOLED (SYNs dropped, nothing refuses): unrouted address on the default bridge"
run T3-black-hole "http://10.255.255.1:18335" "--network bridge"
echo "--- server killed MID-BUILD (empty server so the build is long; stop it 8 s in)"
docker rm -f gocached-spike >/dev/null 2>&1; rm -rf /root/gocached-spike/data/*
docker run -d --name gocached-spike --cpus 2 --memory 2g -p 127.0.0.1:18335:31364 -p 127.0.0.1:18337:31365 -v /root/gocached-spike/bin:/opt:ro -v /root/gocached-spike/data:/data debian:bookworm-slim /opt/gocached --cache-dir=/data --listen=:31364 --debug-listen=:31365 --max-size-gb=20 >/dev/null
sleep 3
( sleep 8; docker stop gocached-spike >/dev/null ) &
run T4-killed-mid-build "$S"
wait
docker start gocached-spike >/dev/null; sleep 3
echo "--- recovery: server back, empty data; a fresh client fills it again"
run T5-after-restart-fill "$S"
run T6-warm-again "$S"
echo ALL-DONE
