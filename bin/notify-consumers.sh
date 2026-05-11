#!/usr/bin/env bash
# Tell the consumers (catalog.0exec.com, hub.scrapetheworld.org) that the
# registry has changed so they re-fetch instead of waiting for their cache
# TTL. Runs locally — no GitHub Actions, no webhook. Intended for use right
# after `git push` on this repo (e.g. via a post-push helper alias).
#
# Auth model:
#   catalog.0exec.com/refresh — the catalog already exposes this; it's
#       unauthenticated because it only triggers a re-poll of its own data
#       and is rate-limited by nginx.
#   hub.scrapetheworld.org/api/refresh-directory — protected by the
#       REFRESH_TOKEN env var on the hub. Set HUB_REFRESH_TOKEN in your
#       shell or in `~/.config/services-registry.env` and source it before
#       running this script.
#
# Usage:
#   bin/notify-consumers.sh
#   HUB_REFRESH_TOKEN=… bin/notify-consumers.sh
set -euo pipefail

# Load token from a local config file if present (not committed; see README).
if [[ -f "$HOME/.config/services-registry.env" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.config/services-registry.env"
fi

CATALOG_URL="${CATALOG_URL:-https://catalog.0exec.com/refresh}"
HUB_URL="${HUB_URL:-https://hub.scrapetheworld.org/api/refresh-directory}"

notify() {
  local label="$1" url="$2" auth_header="${3:-}"
  local code
  if [[ -n "$auth_header" ]]; then
    code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H "$auth_header" "$url" || echo 000)
  else
    code=$(curl -sS -o /dev/null -w '%{http_code}' "$url" || echo 000)
  fi
  case "$code" in
    20[0-9]) echo "  $label  HTTP $code  ok" ;;
    *)       echo "  $label  HTTP $code  FAIL" ; return 1 ;;
  esac
}

echo "notifying consumers of registry change…"

# Catalog: GET /refresh (idempotent; no auth in the existing implementation).
notify "catalog  " "$CATALOG_URL"

# Hub: POST /api/refresh-directory with Bearer token.
if [[ -z "${HUB_REFRESH_TOKEN:-}" ]]; then
  echo "  hub      skipped (set HUB_REFRESH_TOKEN to enable)"
else
  notify "hub      " "$HUB_URL" "Authorization: Bearer $HUB_REFRESH_TOKEN"
fi
