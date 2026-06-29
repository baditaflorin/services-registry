#!/usr/bin/env bash
# setup-lxc-108.sh — idempotent bootstrap for Builder LXC 108 on 0docker.com.
#
# Run this whenever LXC 108 is rebuilt or cloned to a new container. It
# installs the PATH shims and env-file stubs that make `fleet-runner` work
# from both inside the LXC and via `pct exec 108 -- bash -lc "fleet-runner …"`.
#
# Usage:
#   # Option A: from your workstation (SSH access to bastion required)
#   ssh root@0docker.com 'pct exec 108 -- bash -s' \
#     < bin/setup-lxc-108.sh
#
#   # Option B: directly on Builder LXC 108
#   bash <(curl -fsSL https://raw.githubusercontent.com/baditaflorin/services-registry/main/bin/setup-lxc-108.sh)
#
# What it does:
#   1. Symlinks /usr/local/bin/fleet-runner into /usr/bin/ and /usr/sbin/
#      so `pct exec 108 -- bash -lc "fleet-runner …"` finds it without
#      needing the full path. (pct exec does not load /etc/environment and
#      the LXC's login profiles don't set PATH before bash's command lookup,
#      so /usr/local/bin isn't usable by name — only /usr/bin and /usr/sbin
#      are reliably in the default PATH that pct exec inherits.)
#   2. Stubs /root/.fleet-runner.env if it doesn't already exist.
#
# Idempotent: safe to re-run; existing correct symlinks are left in place.

set -euo pipefail

FLEET_RUNNER_BIN=/usr/local/bin/fleet-runner

# ---------------------------------------------------------------------------
# 1. Verify fleet-runner binary exists at the canonical path
# ---------------------------------------------------------------------------
if [ ! -f "$FLEET_RUNNER_BIN" ]; then
  echo "ERROR: $FLEET_RUNNER_BIN not found." >&2
  echo "Install fleet-runner first (build from go_fleet_runner or copy the binary)." >&2
  exit 1
fi
echo "[ok] $FLEET_RUNNER_BIN exists"

# ---------------------------------------------------------------------------
# 2. PATH shims — symlink into /usr/bin/ and /usr/sbin/
#    /usr/local/bin is in the POSIX default PATH but is NOT reliably found
#    when bash is launched by `pct exec` without a full login environment.
#    /usr/bin and /usr/sbin are always available.
# ---------------------------------------------------------------------------
for target_dir in /usr/bin /usr/sbin; do
  target="$target_dir/fleet-runner"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$FLEET_RUNNER_BIN" ]; then
    echo "[ok] $target already symlinked"
  else
    ln -sf "$FLEET_RUNNER_BIN" "$target"
    echo "[+]  created $target -> $FLEET_RUNNER_BIN"
  fi
done

# ---------------------------------------------------------------------------
# 3. Smoke test — verify the shims work in a non-login bash (same as pct exec)
# ---------------------------------------------------------------------------
if bash -c "fleet-runner --help" 2>&1 | grep -q "fleet-runner v"; then
  echo "[ok] fleet-runner reachable by name from bash -c"
else
  echo "ERROR: fleet-runner still not found after symlinking" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. /root/.fleet-runner.env stub
#    fleet-runner verbs that talk to the keystore (key provision/revoke/list,
#    audit fleet-auth-scope) and the Hetzner DNS step in deploy need these.
#    Tokens live in private fleet-state/OPS.md — fill them in after setup.
#    Format MUST use `export KEY=VAL` lines (not bare KEY=VAL) so child
#    processes inherit the vars; see services-registry/CLAUDE.md note.
# ---------------------------------------------------------------------------
ENV_FILE=/root/.fleet-runner.env
if [ -f "$ENV_FILE" ]; then
  echo "[ok] $ENV_FILE already exists (not overwriting)"
else
  cat > "$ENV_FILE" <<'EOF'
# fleet-runner env — fill in from private fleet-state/OPS.md
# Source this file before running admin verbs:
#   source /root/.fleet-runner.env
# Or add to /root/.bashrc:
#   [ -f ~/.fleet-runner.env ] && source ~/.fleet-runner.env
export HCLOUD_TOKEN=""
export HETZNER_TOKEN=""       # kept as alias; same value as HCLOUD_TOKEN
export APIKEY_SERVICE_URL=""
export APIKEY_SERVICE_ADMIN_TOKEN=""
export FLEET_SECRETS_ADMIN_TOKEN=""
EOF
  chmod 600 "$ENV_FILE"
  echo "[+]  created $ENV_FILE stub (fill in tokens from fleet-state/OPS.md)"
fi

# ---------------------------------------------------------------------------
# 5. Auto-source .fleet-runner.env in /root/.bashrc if not already present
# ---------------------------------------------------------------------------
BASHRC=/root/.bashrc
MARKER='[ -f ~/.fleet-runner.env ] && source ~/.fleet-runner.env'
if grep -qF "$MARKER" "$BASHRC" 2>/dev/null; then
  echo "[ok] .fleet-runner.env already auto-sourced in $BASHRC"
else
  echo "" >> "$BASHRC"
  echo "# fleet-runner env (tokens for keystore / Hetzner DNS)" >> "$BASHRC"
  echo "$MARKER" >> "$BASHRC"
  echo "[+]  added auto-source of .fleet-runner.env to $BASHRC"
fi

echo ""
echo "Builder LXC 108 setup complete."
echo "Next steps:"
echo "  1. Fill in tokens in $ENV_FILE (see fleet-state/OPS.md)"
echo "  2. Test: fleet-runner health"
