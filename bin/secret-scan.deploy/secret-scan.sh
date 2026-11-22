#!/usr/bin/env bash
# Scan every repo's origin/main tree with gitleaks (pinned image). Run ON Builder CT108.
# usage: secret-scan.sh OUTDIR [repo ...]     (no repos = every real checkout under /root/workspace; symlinks are skipped)
# Output: OUTDIR/json/<repo>.json (values redacted), OUTDIR/summary.tsv (repo, rule, file, line, fingerprint), OUTDIR/errors.txt
set -u
OUT=${1:?outdir}; shift
IMG=${SECRET_SCAN_IMAGE:-ghcr.io/gitleaks/gitleaks:v8.30.1}
CFG=${SECRET_SCAN_CONFIG:-/usr/local/share/secret-scan/gitleaks.toml}
WS=${SECRET_SCAN_WORKSPACE:-/root/workspace}
mkdir -p "$OUT/json"; : > "$OUT/summary.tsv"; : > "$OUT/errors.txt"
if [ $# -gt 0 ]; then repos=("$@"); else repos=($(ls "$WS")); fi
tmp=$(mktemp -d /root/ss.XXXXXX); trap 'rm -rf "$tmp"' EXIT
n=0
for r in "${repos[@]}"; do
  [ -L "$WS/$r" ] && continue
  [ -e "$WS/$r/.git" ] || continue
  rm -rf "$tmp/t"; mkdir -p "$tmp/t"
  ref=origin/main; git -C "$WS/$r" rev-parse -q --verify $ref >/dev/null || ref=origin/master
  git -C "$WS/$r" rev-parse -q --verify $ref >/dev/null || { echo "$r no origin main" >>"$OUT/errors.txt"; continue; }
  git -C "$WS/$r" archive $ref 2>/dev/null | tar -x -C "$tmp/t" 2>/dev/null || { echo "$r archive failed" >>"$OUT/errors.txt"; continue; }
  nice -n 15 docker run --rm --memory 768m --cpus 1 -v "$tmp/t:/src:ro" -v "$CFG:/cfg.toml:ro" -v "$OUT/json:/out" "$IMG" \
    detect --source /src --no-git --config /cfg.toml --redact --no-banner --exit-code 0 -r "/out/$r.json" >/dev/null 2>>"$OUT/errors.txt"
  f="$OUT/json/$r.json"
  [ -s "$f" ] && python3 - "$r" "$f" >>"$OUT/summary.tsv" <<'P'
import json,sys
r=sys.argv[1]
for x in json.load(open(sys.argv[2])) or []:
    print("\t".join([r,x.get("RuleID",""),x.get("File","").replace("/src/",""),str(x.get("StartLine","")),x.get("Fingerprint","")]))
P
  n=$((n+1)); [ $((n%50)) -eq 0 ] && echo "scanned $n" >&2
done
echo "done: $n repos, $(wc -l < "$OUT/summary.tsv") findings" >&2
