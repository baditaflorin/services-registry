#!/usr/bin/env bash
# gen-etc-hosts.sh — write a split-horizon /etc/hosts block for every
# fleet FQDN, all pointed at the internal gateway IP.
#
# Why: Proxmox NAT-hairpins are flaky. Hosts inside the 10.10.10.0/24
# mesh that resolve a fleet domain (e.g. go-html-proxy.0exec.com) via
# public DNS get the bastion's public IP (176.9.123.221). Then connecting
# back via that public IP to NAT-loopback to the gateway (10.10.10.10)
# often dies with "tlsv1 alert internal error" — depending on the
# bridge/hairpin config. Symptom: fleet-runner smoke / state-snapshot
# show TLS internal errors from the builder LXC even though external
# clients work fine.
#
# Fix: tell the LXC (and any other internal builder) to resolve every
# fleet FQDN directly to 10.10.10.10 via /etc/hosts. Bypasses public
# DNS and the broken hairpin in one stroke.
#
# Idempotent. Reads services.json (one directory up by default; or
# via --registry-url) and (re)writes the block delimited by markers
# in /etc/hosts. Outside the block is untouched.
#
# Usage:
#   sudo ./scripts/gen-etc-hosts.sh                            # local services.json
#   sudo ./scripts/gen-etc-hosts.sh --gateway 10.10.10.10      # override target
#   sudo ./scripts/gen-etc-hosts.sh --registry-url <url>       # fetch from URL
#   sudo ./scripts/gen-etc-hosts.sh --dry-run                  # print only

set -euo pipefail

GATEWAY="${GATEWAY:-10.10.10.10}"
REGISTRY_URL="${REGISTRY_URL:-}"
HOSTS_FILE="${HOSTS_FILE:-/etc/hosts}"
DRY_RUN=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGISTRY_LOCAL="$SCRIPT_DIR/../services.json"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gateway) GATEWAY="$2"; shift 2 ;;
        --registry-url) REGISTRY_URL="$2"; shift 2 ;;
        --hosts-file) HOSTS_FILE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,/^set -e/p' "$0" | grep -E "^# " | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

# Pick the registry source — URL beats local file if both are valid.
fetch_registry() {
    if [[ -n "$REGISTRY_URL" ]]; then
        curl -fsSL "$REGISTRY_URL"
    elif [[ -f "$REGISTRY_LOCAL" ]]; then
        cat "$REGISTRY_LOCAL"
    else
        echo "ERROR: no services.json. Pass --registry-url or run from inside services-registry/." >&2
        exit 1
    fi
}

# Extract fleet FQDNs (0exec.com, 0crawl.com) from services.json.
fqdns=$(fetch_registry | python3 -c "
import json, sys
d = json.load(sys.stdin)
out = set()
for e in d:
    u = e.get('url', '')
    if u.startswith('http') and ('.0exec.com' in u or '.0crawl.com' in u):
        host = u.split('//', 1)[1].split('/', 1)[0]
        out.add(host)
for h in sorted(out):
    print(h)
")

count=$(echo "$fqdns" | wc -l | tr -d ' ')
if [[ "$count" -eq 0 ]]; then
    echo "ERROR: registry produced 0 FQDNs — refusing to proceed" >&2
    exit 1
fi

MARKER_BEGIN="# BEGIN fleet-split-horizon (managed by services-registry/scripts/gen-etc-hosts.sh)"
MARKER_END="# END fleet-split-horizon"

# Render the new block.
new_block=$(printf '%s\n' "$MARKER_BEGIN")
new_block+=$'\n'"# $count fleet FQDNs → $GATEWAY; bypasses broken NAT hairpin from inside the mesh."
new_block+=$'\n'"# Regenerated from services-registry/services.json — do not hand-edit, re-run the script."
while IFS= read -r fq; do
    [[ -z "$fq" ]] && continue
    new_block+=$'\n'"$GATEWAY"$'\t'"$fq"
done <<< "$fqdns"
new_block+=$'\n'"$MARKER_END"

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "# would write to $HOSTS_FILE:"
    echo "$new_block"
    exit 0
fi

# Strip any existing block, then append the fresh one.
current=$(cat "$HOSTS_FILE")
if grep -qF "$MARKER_BEGIN" "$HOSTS_FILE"; then
    current=$(awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
        $0 == b { skip = 1 }
        !skip { print }
        $0 == e { skip = 0; next }
    ' "$HOSTS_FILE")
fi

# Drop trailing blank lines from `current`, then add one blank line + the block.
current=$(printf '%s' "$current" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}')
printf '%s\n\n%s\n' "$current" "$new_block" > "$HOSTS_FILE"

echo "wrote $count FQDNs → $GATEWAY in $HOSTS_FILE"
