#!/usr/bin/env bash
# trl-audit.sh — produce an audit prompt for one service's TRL.
#
# Usage:
#   bin/trl-audit.sh <slug>            # audit a specific service
#   bin/trl-audit.sh --next            # pick the next service without TRL data
#   bin/trl-audit.sh --next --batch 5  # print 5 prompts back-to-back (sized for a single haiku conversation)
#
# Output is a self-contained prompt you paste into a haiku agent. The agent
# returns a 5-line JSON block that you append to overrides.json as the slug's
# TRL fields, then re-run `python3 bin/generate.py`.
#
# Cost discipline: one repo at a time keeps the prompt under ~6K input tokens.
# A run of 200 repos at ~6K in / ~300 out on Haiku 4.5 is roughly $1-2 total.
# Don't bulk-clone — fetch via gh api on demand.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICES_JSON="$ROOT/services.json"

next_untrl() {
  jq -r --arg n "${1:-1}" '
    [ .[] | select(.trl == null) | .id ] | .[0:($n|tonumber)] | .[]
  ' "$SERVICES_JSON"
}

emit_prompt_for_slug() {
  local slug="$1"
  local entry=$(jq -r --arg s "$slug" '.[] | select(.id == $s)' "$SERVICES_JSON")
  if [ -z "$entry" ] || [ "$entry" = "null" ]; then
    echo "ERROR: slug not found in services.json: $slug" >&2
    return 1
  fi

  local repo_url=$(echo "$entry" | jq -r '.repo_url')
  local repo=$(echo "$repo_url" | sed -E 's|https?://github.com/||; s|/+$||')
  local desc=$(echo "$entry" | jq -r '.description')
  local cat=$(echo "$entry" | jq -r '.category')

  # Fetch a small evidence set: file list + main entry-points.
  local files=$(gh api "repos/$repo/contents" --jq '[.[] | select(.type=="file") | .name] | join(",")' 2>/dev/null || echo "(repo not accessible)")
  local main_body=""
  for f in main.go handler.go score.go extract.go detect.go scan.go README.md; do
    local body=$(gh api "repos/$repo/contents/$f" --jq '.content' 2>/dev/null | base64 -d 2>/dev/null | head -c 8000 || true)
    if [ -n "$body" ]; then
      main_body+="\n--- $f (first 8KB) ---\n$body\n"
    fi
  done

  cat <<EOF
========================================================================
TRL audit for: $slug
Repo: $repo_url
Category: $cat
Description: $desc
Files in repo: $files
========================================================================

You are auditing a Go microservice in the baditaflorin fleet. Each service is a small HTTP API that takes a target URL (or other input) and returns structured JSON. The TRL scale we use:

- TRL 1-3 (toy): single regex, one file, no tests, no edge cases
- TRL 4-5 (developing): curated lists or gazetteers, multi-step logic, some tests
- TRL 6-7 (real): RFC-compliant parsing, evidence trails, verdicts, real test coverage
- TRL 8-9 (production): battle-tested, cross-checks, comprehensive

CPU-only constraint: no LLM calls, no paid APIs. Only smart pattern matching, gazetteers, free public datasets.

Read the code below and return EXACTLY a JSON object (no prose, no markdown fences) with these fields:

{
  "trl": <integer 1-9>,
  "trl_evidence": "<one-sentence justification, ≤180 chars>",
  "trl_ceiling": <integer 1-9 OR null if no ceiling>,
  "trl_ceiling_reason": "<one-sentence reason ≤180 chars, OR null>"
}

Set trl_ceiling only if there's a fundamental CPU-only blocker (e.g., needs DOM, needs paid feed, needs auth state, needs ML model). Otherwise null.

CODE:
$(printf '%b' "$main_body")

Return JSON only.
========================================================================
EOF
}

if [ "${1:-}" = "--next" ]; then
  shift
  n=1
  if [ "${1:-}" = "--batch" ]; then
    n="${2:-5}"
  fi
  for slug in $(next_untrl "$n"); do
    emit_prompt_for_slug "$slug"
    echo
  done
elif [ -n "${1:-}" ]; then
  emit_prompt_for_slug "$1"
else
  echo "usage: $0 <slug> | --next [--batch N]" >&2
  exit 1
fi
