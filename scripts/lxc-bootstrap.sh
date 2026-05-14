#!/usr/bin/env bash
# lxc-bootstrap.sh — bring a fresh Builder LXC (or any new ops machine
# that needs the fleet-runner toolchain) from zero to "fleet-runner
# deploy" working. Run as root on the target.
#
# Idempotent. Re-running is fine and is the recommended way to refresh
# after a fleet-runner / hosts / SSH key change.
#
# What it does, in order:
#
#   1. Ensure `git`, `go`, `python3`, `docker`, `docker-buildx`, `curl`
#      are installed (apt). Skips if already present.
#   2. Clone (or pull) `services-registry`, `go_fleet_runner`, and
#      `go-common` into $WORKDIR (default /root/workspace).
#   3. Build fleet-runner from source and install to /usr/local/bin/.
#   4. Generate /etc/hosts split-horizon block via gen-etc-hosts.sh
#      so this builder bypasses NAT hairpin (FQDN → internal gateway).
#   5. Generate an ed25519 SSH key if absent, print the pubkey, and
#      remind the operator which hosts to authorize it at (bastion,
#      gateway, dockerhost — those are the three hops fleet-runner
#      uses for deploy / state-snapshot).
#   6. Optionally run `fleet-runner clone-missing` so every service
#      workspace is pulled.
#
# This script CARRIES NO SECRETS. Public key fingerprints go to
# operator stdout for them to install at the three hops (which is the
# only sensitive step). Private key never leaves the box.
#
# Usage (on a fresh Builder LXC):
#
#   curl -fsSL https://raw.githubusercontent.com/baditaflorin/services-registry/main/scripts/lxc-bootstrap.sh | bash
#
# Or, if you already cloned services-registry:
#
#   sudo ./services-registry/scripts/lxc-bootstrap.sh
#
# Environment overrides:
#   WORKDIR              default: /root/workspace
#   GITHUB_ORG           default: baditaflorin
#   GATEWAY              default: 10.10.10.10  (for the /etc/hosts block)
#   SKIP_PACKAGES        skip apt installs                (default: 0)
#   SKIP_CLONE_MISSING   skip the final `fleet-runner clone-missing`  (default: 0)

set -euo pipefail

WORKDIR="${WORKDIR:-/root/workspace}"
GITHUB_ORG="${GITHUB_ORG:-baditaflorin}"
GATEWAY="${GATEWAY:-10.10.10.10}"
SKIP_PACKAGES="${SKIP_PACKAGES:-0}"
SKIP_CLONE_MISSING="${SKIP_CLONE_MISSING:-0}"

log() { printf '\n=== %s ===\n' "$*"; }

# ---- 1. Packages -----------------------------------------------------
log "1. Packages"
if [[ "$SKIP_PACKAGES" == "0" ]]; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "WARN: apt-get not found — this script targets Debian/Ubuntu LXCs."
        echo "Install git / python3 / docker / golang manually and re-run with SKIP_PACKAGES=1."
        exit 1
    fi
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        git python3 curl ca-certificates jq \
        golang docker.io docker-buildx
else
    echo "skipped (SKIP_PACKAGES=1)"
fi

mkdir -p "$WORKDIR"

# ---- 2. Clone or refresh the three repos needed for bootstrap --------
log "2. Workspace repos"
for repo in services-registry go_fleet_runner go-common; do
    dir="$WORKDIR/$repo"
    if [[ -d "$dir/.git" ]]; then
        echo "  refresh $repo"
        git -C "$dir" pull --rebase --quiet || echo "    (pull failed, leaving as-is)"
    else
        echo "  clone $repo"
        git clone -q "https://github.com/$GITHUB_ORG/$repo.git" "$dir"
    fi
done

# ---- 3. Build + install fleet-runner ---------------------------------
log "3. fleet-runner"
cd "$WORKDIR/go_fleet_runner"
go build -o /tmp/fleet-runner-new .
cp /tmp/fleet-runner-new /usr/local/bin/fleet-runner
chmod +x /usr/local/bin/fleet-runner
echo "  installed: $(/usr/local/bin/fleet-runner --version 2>/dev/null || head -c 80 < <(/usr/local/bin/fleet-runner --help 2>&1 | head -1))"

# ---- 4. /etc/hosts split-horizon -------------------------------------
log "4. /etc/hosts split-horizon for $GATEWAY"
"$WORKDIR/services-registry/scripts/gen-etc-hosts.sh" --gateway "$GATEWAY"

# ---- 5. SSH key for fleet-runner's three hops ------------------------
log "5. SSH key"
SSH_KEY="${HOME}/.ssh/id_ed25519"
mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
if [[ ! -f "$SSH_KEY" ]]; then
    ssh-keygen -t ed25519 -N "" -C "fleet-runner@$(hostname)" -f "$SSH_KEY" >/dev/null
    echo "  generated $SSH_KEY"
else
    echo "  reuse $SSH_KEY"
fi

pubkey=$(cat "${SSH_KEY}.pub")
cat <<HOWTO

  Public key — install this at all three fleet hops so fleet-runner
  deploy / state-snapshot can SSH without prompting:

      $pubkey

  Install commands (run from a host that can already reach each):

      # bastion
      ssh root@0docker.com "echo '$pubkey' >> ~/.ssh/authorized_keys"
      # gateway (via the bastion jump host)
      ssh -J root@0docker.com florin@10.10.10.10 "echo '$pubkey' >> ~/.ssh/authorized_keys"
      # dockerhost (via the bastion jump host)
      ssh -J root@0docker.com ubuntu_vm@10.10.10.20 "echo '$pubkey' >> ~/.ssh/authorized_keys"

  After install, verify:

      ssh root@0docker.com whoami
      ssh -J root@0docker.com florin@10.10.10.10 hostname
      ssh -J root@0docker.com ubuntu_vm@10.10.10.20 hostname

  Rotation: re-run this script; an existing key is reused.

HOWTO

# ---- 6. Populate workspace clones ------------------------------------
log "6. fleet-runner clone-missing"
if [[ "$SKIP_CLONE_MISSING" == "0" ]]; then
    /usr/local/bin/fleet-runner clone-missing --workers 8 || echo "  (clone-missing finished with some failures — re-run if needed)"
else
    echo "skipped (SKIP_CLONE_MISSING=1)"
fi

log "DONE"
echo "Verify the toolchain:"
echo "  fleet-runner converge"
echo "  fleet-runner state snapshot"
echo ""
echo "Common next steps:"
echo "  fleet-runner port-heal --reconcile --apply       # self-heal port drift"
echo "  fleet-runner scaffold-service-yaml --missing     # write missing manifests"
