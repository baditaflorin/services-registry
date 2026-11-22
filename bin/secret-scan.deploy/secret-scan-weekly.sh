#!/bin/sh
# Weekly fleet secret scan, run ON the builder by secret-scan-weekly.timer.
# Compares with the accepted baseline (repo, rule, file: line moves do not count) and reports only NEW findings.
# State in /var/lib/secret-scan: baseline.tsv (accepted), last/ (latest run), new.txt. PAUSE file stops it.
set -u
STATE=${SECRET_SCAN_STATE:-/var/lib/secret-scan}
BIN=${SECRET_SCAN_BIN:-/usr/local/bin/secret-scan}
PROM=${SECRET_SCAN_PROM:-/var/lib/prometheus-node-exporter/textfile/fleet_secret_scan.prom}
mkdir -p "$STATE"
[ -e "$STATE/PAUSE" ] && { echo "secret-scan-weekly: paused"; exit 0; }
rm -rf "$STATE/last"
"$BIN" "$STATE/last" 2>&1 | tail -3
ok=1; [ -d "$STATE/last/json" ] || ok=0
cut -f1-3 "$STATE/last/summary.tsv" | sort -u > "$STATE/last.keys"
if [ ! -s "$STATE/baseline.tsv" ]; then cp "$STATE/last.keys" "$STATE/baseline.tsv"; echo "secret-scan-weekly: no baseline yet: this run is now the accepted baseline"; fi
sort -u "$STATE/baseline.tsv" -o "$STATE/baseline.tsv"
comm -13 "$STATE/baseline.tsv" "$STATE/last.keys" > "$STATE/new.txt"
total=$(wc -l < "$STATE/last.keys"); new=$(wc -l < "$STATE/new.txt")
mkdir -p "$(dirname "$PROM")"
cat > "$PROM.tmp" <<EOF
# TYPE fleet_secret_scan_findings gauge
fleet_secret_scan_findings $total
# TYPE fleet_secret_scan_new_findings gauge
fleet_secret_scan_new_findings $new
# TYPE fleet_secret_scan_run_success gauge
fleet_secret_scan_run_success $ok
# TYPE fleet_secret_scan_last_run_timestamp_seconds gauge
fleet_secret_scan_last_run_timestamp_seconds $(date +%s)
EOF
mv "$PROM.tmp" "$PROM"
if [ "$new" -gt 0 ]; then
  echo "secret-scan-weekly: $new NEW finding(s) (repo, rule, file; values are never printed):"
  cat "$STATE/new.txt"
fi
exit 0
