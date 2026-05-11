#!/usr/bin/env bash
# Refresh the upstream snapshots in sources/ so bin/build.py has fresh inputs.
# Run from anywhere; uses the GH CLI for auth-aware fetches.
set -euo pipefail
cd "$(dirname "$0")/.."

gh api repos/baditaflorin/go_services_dashboard/contents/config/services.json \
  --jq '.content' | tr -d '\n' | base64 -D > sources/0crawl-services.json

gh api repos/baditaflorin/go-catalog-service/contents/main.go \
  --jq '.content' | tr -d '\n' | base64 -D > sources/0exec-catalog-main.go

gh api repos/baditaflorin/hub_scrapetheworld_org/contents/static/app.js \
  --jq '.content' | tr -d '\n' | base64 -D > sources/hub-app.js

echo "sources refreshed:"
wc -l sources/*
