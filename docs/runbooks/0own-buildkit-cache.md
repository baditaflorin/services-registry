# 0own BuildKit cache cleanup

The Buildx container uses its own persistent volume, separate from the Docker
daemon's builder cache and the Woodpecker language caches. `docker system prune`
does not bound this volume. The 0own timer considers a BuildKit-native prune
every six hours. BuildKit decides which records are unused; the invocation
retains records younger than seven days and aims to keep 80,000 MB of cache.
This is a target, not a hard disk cap. Active records and recent data may leave
usage above the target.

The helper defers if the guest's I/O full-stall average is 5% or greater or a
Woodpecker step is already running. Missing or malformed PSI and Docker/GC
errors fail the unit; a deferral is logged and retried at the next timer. The
step check is an observation, not a lock: work can start immediately after it.
BuildKit manages its own records, but cleanup may still add disk load to a
concurrent build. The host uses a shared HDD mirror, and the guest PSI does not
measure all hypervisor I/O. Monitor host latency and disable the timer if the
cleanup hurts workloads. Do not increase the separate Woodpecker shared-cache
prune frequency without the admission protocol described in
`woodpecker-ci-go-cache-pilot.md`.

Install from a reviewed, tested source revision on the builder:

```sh
install -m 0755 scripts/buildkit-gc/prune.sh /usr/local/sbin/0own-buildkit-gc
install -m 0644 templates/0own-buildkit-gc.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now 0own-buildkit-gc.timer
systemctl list-timers 0own-buildkit-gc.timer
journalctl -u 0own-buildkit-gc.service --since -1d
```

To stop further runs: `systemctl disable --now 0own-buildkit-gc.timer`. A
running prune is bounded by the unit timeout. This service never deletes Docker
volumes or shared language caches. Reclaimed cache may need to be downloaded or
rebuilt. Use `docker exec buildx_buildkit_own-amd64-builder0 buildctl du` to
inspect BuildKit's reported usage, but avoid running it repeatedly on the HDD
mirror because inventory itself can generate I/O.
