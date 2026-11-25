#!/usr/bin/env bash
# Run from the hypervisor so the shared disk's host PSI is checked too.
set -euo pipefail
psi="${BUILDKIT_GC_HOST_PSI_FILE:-/proc/pressure/io}"
[[ -r "$psi" ]] || { echo 'host I/O pressure unavailable' >&2; exit 1; }
io=$(awk '$1 == "full" {for(i=2;i<=NF;i++) if ($i ~ /^avg10=/) {sub(/^avg10=/,"",$i); print $i; exit}}' "$psi")
[[ "$io" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo 'invalid host I/O pressure' >&2; exit 1; }
if ! awk -v io="$io" 'BEGIN {exit !(io < 5)}'; then
  echo "Host I/O full avg10 ${io}%; deferring prune"
  exit 0
fi
# The QEMU guest agent reports the child exit code in JSON, while qm itself
# exits 0 even when the child fails. Preserve that failure in systemd.
result=$(qm guest exec 610 --timeout 840 -- /usr/local/sbin/0own-buildkit-gc)
python3 -c 'import json,sys
try:
 d=json.load(sys.stdin)
 print(d.get("out-data", ""), end="")
 print(d.get("err-data", ""), end="", file=sys.stderr)
 if not d.get("exited") or d.get("exitcode") != 0:
  print("guest cleanup did not complete successfully", file=sys.stderr); sys.exit(1)
except (ValueError, TypeError) as e:
 print("invalid guest-agent result: " + str(e), file=sys.stderr); sys.exit(1)' <<<"$result"
