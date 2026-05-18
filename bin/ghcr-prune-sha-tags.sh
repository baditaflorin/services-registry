#!/usr/bin/env bash
# ghcr-prune-sha-tags.sh — delete old git-short-sha image tags from
# GitHub Container Registry.
#
# Why this exists: ADR-0028 pushes a :<short-sha> tag on every
# fleet-runner deploy build. Without retention, GHCR storage grows
# monotonically — one new manifest per build per service. GHCR
# retention policies aren't settable via the API (org-level only,
# UI-only); per-package version DELETE is. This script implements
# the "keep N most recent sha tags per package, prune the rest"
# strategy, plus protections against deleting :<semver> + :latest +
# :rollback (the non-content-addressed tags ADR-0028 keeps for
# discoverability).
#
# Defaults to DRY-RUN. Pass --apply to actually delete.
#
# Auth: requires `gh` CLI authenticated with a PAT that has
# `read:packages` + `delete:packages` scope. Run `gh auth status` to
# verify before this script.

set -euo pipefail

ORG="${ORG:-baditaflorin}"
KEEP_LAST_N="${KEEP_LAST_N:-10}"     # keep this many most-recent sha tags per package
MIN_AGE_DAYS="${MIN_AGE_DAYS:-7}"     # never delete a sha tag younger than this
APPLY="${APPLY:-false}"               # set to "true" or pass --apply to delete
PACKAGE_FILTER="${PACKAGE_FILTER:-}"  # optional substring filter (e.g. "go-fleet" to limit scope)

# Tags that this script MUST NEVER delete. These are the human-readable
# convention pins ADR-0028 keeps alongside the canonical :<sha>.
PROTECTED_REGEX='^(latest|rollback|v?[0-9]+\.[0-9]+\.[0-9]+([+-].+)?)$'

# A 7-40 char lowercase hex string is the git short-sha shape ADR-0028
# pushes. Anything matching this AND not matching PROTECTED_REGEX is a
# candidate for pruning.
SHA_REGEX='^[0-9a-f]{7,40}$'

usage() {
  cat <<EOF
ghcr-prune-sha-tags.sh — prune git-short-sha tags from GHCR

Usage:
  $(basename "$0") [--apply]

Env (override at invocation):
  ORG=$ORG                            GitHub org
  KEEP_LAST_N=$KEEP_LAST_N            Keep this many most-recent sha tags per package
  MIN_AGE_DAYS=$MIN_AGE_DAYS          Never delete a sha tag younger than this
  PACKAGE_FILTER=${PACKAGE_FILTER:-(none)}  Substring filter on package name (case-insensitive)
  APPLY=${APPLY}                     Set to "true" (or pass --apply) to actually delete

Examples:
  # Dry-run across the whole org
  $(basename "$0")

  # Prune only go-fleet-* packages, dry-run
  PACKAGE_FILTER=go-fleet $(basename "$0")

  # Actually delete eligible tags
  $(basename "$0") --apply

Requires \`gh\` CLI authenticated with read:packages + delete:packages.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; usage; exit 1 ;;
  esac
done

# Sanity checks.
if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI not installed. https://cli.github.com/" >&2
  exit 2
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh CLI not authenticated. Run 'gh auth login --scopes read:packages,delete:packages'" >&2
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq not installed (needed to parse gh API output)." >&2
  exit 2
fi

# Cutoff timestamp for MIN_AGE_DAYS — anything updated after this is too
# new to delete. ISO-8601 in UTC.
if date --version >/dev/null 2>&1; then
  CUTOFF=$(date -u -d "${MIN_AGE_DAYS} days ago" +%Y-%m-%dT%H:%M:%SZ)
else
  # macOS BSD date
  CUTOFF=$(date -u -v"-${MIN_AGE_DAYS}d" +%Y-%m-%dT%H:%M:%SZ)
fi
echo "ORG=$ORG  KEEP_LAST_N=$KEEP_LAST_N  MIN_AGE_DAYS=$MIN_AGE_DAYS  CUTOFF=$CUTOFF  APPLY=$APPLY"
[ -n "$PACKAGE_FILTER" ] && echo "PACKAGE_FILTER=$PACKAGE_FILTER"
echo

# List all container packages owned by the org.
echo "Fetching package list for org $ORG …"
packages=$(gh api --paginate "/orgs/${ORG}/packages?package_type=container" \
  | jq -r '.[].name')

total_deleted=0
total_kept=0
total_pkgs=0

while IFS= read -r pkg; do
  [ -z "$pkg" ] && continue
  if [ -n "$PACKAGE_FILTER" ]; then
    case "${pkg,,}" in *"${PACKAGE_FILTER,,}"*) ;; *) continue ;; esac
  fi
  total_pkgs=$((total_pkgs + 1))

  echo "=== $pkg ==="

  # List all versions; each version has an 'id', 'updated_at', and 'metadata.container.tags'.
  versions_json=$(gh api --paginate "/orgs/${ORG}/packages/container/${pkg}/versions" 2>/dev/null \
    || { echo "  (failed to fetch versions for $pkg)"; continue; })

  # Build a list of (id, updated_at, tag) tuples for sha-tagged versions only.
  candidates=$(echo "$versions_json" | jq -r --arg sha "$SHA_REGEX" --arg prot "$PROTECTED_REGEX" '
    .[]
    | . as $v
    | (.metadata.container.tags // [])
    | map(select(test($sha) and (test($prot) | not)))
    | map({id: $v.id, updated_at: $v.updated_at, tag: .})
    | .[]
    | "\(.updated_at)\t\(.id)\t\(.tag)"
  ' | sort -r)
  # ^ sort -r → newest first (ISO-8601 sorts lexically)

  count=0
  while IFS=$'\t' read -r updated_at id tag; do
    [ -z "$updated_at" ] && continue
    count=$((count + 1))
    if [ "$count" -le "$KEEP_LAST_N" ]; then
      total_kept=$((total_kept + 1))
      continue
    fi
    if [ "$updated_at" \> "$CUTOFF" ]; then
      total_kept=$((total_kept + 1))
      continue
    fi
    if [ "$APPLY" = true ]; then
      if gh api -X DELETE "/orgs/${ORG}/packages/container/${pkg}/versions/${id}" >/dev/null 2>&1; then
        echo "  - deleted :${tag} (id=${id}, updated=${updated_at})"
        total_deleted=$((total_deleted + 1))
      else
        echo "  ! delete failed :${tag} (id=${id})"
      fi
    else
      echo "  - would delete :${tag} (id=${id}, updated=${updated_at})"
      total_deleted=$((total_deleted + 1))
    fi
  done <<< "$candidates"
done <<< "$packages"

echo
if [ "$APPLY" = true ]; then
  echo "Done. Deleted $total_deleted sha-tagged versions across $total_pkgs packages. Kept $total_kept (within KEEP_LAST_N or MIN_AGE_DAYS)."
else
  echo "Dry-run. Would delete $total_deleted sha-tagged versions across $total_pkgs packages. Would keep $total_kept."
  echo "Re-run with --apply to actually delete."
fi
