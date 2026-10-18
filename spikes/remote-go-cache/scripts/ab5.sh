#!/bin/bash
cd /root/gocached-spike
docker start gocached-spike >/dev/null 2>&1; docker network connect gcnet gocached-spike 2>/dev/null; sleep 2
for D in 30 80 200; do
  docker run --rm --network gcnet --cap-add NET_ADMIN --cpus 4 --memory 6g -v /root/gocached-spike/bin:/opt:ro -v /root/gocached-spike/work:/w \
    -e GOMODCACHE=/w/mod -e "GOCACHEPROG=/opt/ci-gocache --cache-server=http://gocached-spike:31364 --cache-dir=/tmp/lc" -w /w/gc golang:1.25 bash -c '
    apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq iproute2 >/dev/null 2>&1
    tc qdisc add dev eth0 root netem delay '"$D"'ms
    s=$(date +%s); go build ./... ; b=$?; go test ./... >/tmp/t.log 2>&1; t=$?; e=$(date +%s)
    echo "RESULT guard added_delay_ms='"$D"' seconds=$((e-s)) build_rc=$b test_rc=$t"' 2>&1 | grep -E 'RESULT|ci-gocache:'
done
echo ALL-DONE
