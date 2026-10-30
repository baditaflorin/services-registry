#!/usr/bin/env bash
# verify-examples-run.sh: run bin/verify-examples.py without a human, on the builder, with a short-lived key.
#
#   bin/verify-examples-run.sh [--only id1,id2] [--dry] [--ref REF] [--out FILE] [--direct-hosts A,B]
#
# What it does (the builder is LXC 108, reached with `ssh <builder> pct exec 108 -- bash -s`):
#   1. reads services.json and bin/verify-examples.py from REF (default origin/main) of the builder's clone with
#      `git show` (after a `git fetch`, which moves only remote-tracking refs: that clone has local edits, so it is
#      never checked out, reset or cleaned);
#   2. mints ONE key: `fleet-runner key issue examples-verifier --user examples-verifier --scope <hostnames of the
#      services that are checked> --ttl 1h --use-limit <services*2+50>` into a 0700 directory under /dev/shm (umask 077).
#      The keystore has no read-only flag; "read-only" is: the check sends GET only, the scope is the service
#      hostnames, the TTL is 1 h and the use limit is 2 calls per service plus 50. Nothing here widens any of them;
#   3. runs verify-examples.py with --api-key-file (the key is never on a command line, in an environment variable of
#      the check, or in any output);
#   4. ALWAYS (EXIT/INT/TERM/HUP trap, also when the run fails): revokes the key (`fleet-runner key revoke
#      examples-verifier` = the most recent active key of that slug, so the key is not on a command line either),
#      proves the revoked key is refused (HTTP 401) by the gateway and by the service itself, and shreds the files;
#   5. copies services.examples.json to --out (default: services.examples.json of the checkout this script is in) and
#      prints ONLY the tally, the services whose replicas diverge, and the states that changed against the file that
#      was there before. Never the key.
#
# --only   comma-separated service ids: the scope, the use limit and the run shrink to them.
# --dry    everything but the mint and the run: fetch, read REF, compute the targets, the scope and the use limit,
#          check the tools and the keystore on the builder, print what would be minted; changes nothing.
# --ref    a git ref of the builder's clone to read services.json and verify-examples.py from (a pushed branch works
#          after the fetch). Both files come from the SAME ref, so the check always matches the registry it reads.
# --direct-hosts   docker-host addresses probed for each compose service (default: verify-examples.py's own list).
#          At most 2: the use limit allows 2 calls per service.
#
# Environment: VERIFY_EXAMPLES_BUILDER (ssh target of the Proxmox host, default root@0docker.com),
#              VERIFY_EXAMPLES_CT (container id of the builder, default 108).
# Exit: 0 ok; 1 the run or a precondition failed; 2 usage; 3 the key could NOT be shown revoked (it still expires by
#       itself after 1 h and by its use limit: revoke by hand with `fleet-runner key revoke examples-verifier`).
# Concurrency: a lock on the builder; a second run stops at once.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILDER="${VERIFY_EXAMPLES_BUILDER:-root@0docker.com}"
CT="${VERIFY_EXAMPLES_CT:-108}"
REF="origin/main"
ONLY=""
DRY=0
OUT="$HERE/../services.examples.json"
DIRECT=""
PRINT_REMOTE=0

usage() { sed -n "2,35p" "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "verify-examples-run: $1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --only)         [ $# -ge 2 ] || die "--only needs a value" 2; ONLY="$2"; shift 2 ;;
    --dry)          DRY=1; shift ;;
    --ref)          [ $# -ge 2 ] || die "--ref needs a value" 2; REF="$2"; shift 2 ;;
    --out)          [ $# -ge 2 ] || die "--out needs a value" 2; OUT="$2"; shift 2 ;;
    --direct-hosts) [ $# -ge 2 ] || die "--direct-hosts needs a value" 2; DIRECT="$2"; shift 2 ;;
    --print-remote) PRINT_REMOTE=1; shift ;;   # the script that would run on the builder, for review and tests
    -h|--help)      usage; exit 0 ;;
    *)              die "unknown argument: $1 (see --help)" 2 ;;
  esac
done

# Everything that goes into the remote script is checked here: it is shell text on the builder.
[[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "--ref: not a plain git ref: $REF" 2
[[ -z "$ONLY" || "$ONLY" =~ ^[a-z0-9][a-z0-9_.-]*(,[a-z0-9][a-z0-9_.-]*)*$ ]] || die "--only: comma-separated service ids expected" 2
[[ -z "$DIRECT" || "$DIRECT" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(,[0-9]{1,3}(\.[0-9]{1,3}){3}){0,1}$ ]] || die "--direct-hosts: one or two IPv4 addresses expected" 2
[[ "$CT" =~ ^[0-9]+$ ]] || die "VERIFY_EXAMPLES_CT must be a number" 2

TMP="$(mktemp -d "${TMPDIR:-/tmp}/verify-examples-run.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# The previous result, to compare against (a copy: --out is overwritten below).
OLD="$TMP/old.json"
if [ -f "$OUT" ]; then cp "$OUT" "$OLD"; else echo '{"services":{}}' > "$OLD"; fi

{
  printf 'REF=%q\nONLY=%q\nDRY=%q\nDIRECT=%q\n' "$REF" "$ONLY" "$DRY" "$DIRECT"
  cat <<'REMOTE'
# ---- runs on the builder, as root, in bash -s: every command that could read stdin gets </dev/null ----
main() {
set -u -o pipefail
umask 077
REPO="${VERIFY_EXAMPLES_REPO:-/root/workspace/services-registry}"   # the overrides are for the tests; nothing sets them on the builder
ENVF="${VERIFY_EXAMPLES_ENVFILE:-/root/.fleet-runner.env}"
LOCKF="${VERIFY_EXAMPLES_LOCKFILE:-/root/.verify-examples.lock}"
PAUSE="${VERIFY_EXAMPLES_PAUSE:-3}"   # seconds between revoke / proof retries
SLUG=examples-verifier
W=""
KEYF=""
HDRF=""
MINTED=0
HAD_KEY=0
PYPID=""
RUN_RC=1
note() { echo "[builder] $*" >&2; }
fail() { note "ERROR: $*"; exit 1; }

fr() {    # fleet-runner with the keystore environment, in a subshell so the admin token reaches nothing else
  ( set -a; . "$ENVF"; set +a; export FLEET_SECRETS_URL=https://fleet-secrets.0exec.com; exec fleet-runner "$@" </dev/null )
}

code() {  # code <url>: HTTP status of a GET with the key from the header file (curl reads -H @file: no key in argv)
  curl -s -o /dev/null -w '%{http_code}' --max-time 30 -H "@$HDRF" "$1" </dev/null 2>/dev/null || true
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  set +e
  [ -n "$PYPID" ] && kill "$PYPID" 2>/dev/null && wait "$PYPID" 2>/dev/null   # a signal must not leave the check running with the key
  local revoked=0 proof="n/a"
  if [ "$MINTED" = 1 ]; then
    local i
    for i in 1 2 3; do
      if fr key revoke "$SLUG" >"$W/revoke.log" 2>&1; then revoked=1; break; fi
      sleep "$PAUSE"
    done
    [ -s "$HDRF" ] && HAD_KEY=1
    if [ "$revoked" = 1 ] && [ "$HAD_KEY" = 1 ]; then
      proof="unproven"
      local tries after_gw after_lan
      for tries in 1 2 3 4 5 6; do
        after_gw="$(code "$PROOF_GW")"; after_lan="$(code "$PROOF_LAN")"
        if { [ "$BEFORE_GW" != 401 ] && [ "$after_gw" = 401 ]; } || { [ "$BEFORE_LAN" != 401 ] && [ "$after_lan" = 401 ]; }; then
          proof="ok"; break
        fi
        sleep "$PAUSE"
      done
      note "revoked key: gateway $BEFORE_GW -> ${after_gw:-?}, service $BEFORE_LAN -> ${after_lan:-?}: refusal is $proof"
    elif [ "$revoked" != 1 ]; then
      note "REVOKE FAILED (3 tries): $(tr -d '\n' < "$W/revoke.log" | cut -c1-200)"
    fi
  fi
  # The result goes out only when it is free of the key; then everything that held the key is shredded.
  if [ "$RUN_RC" = 0 ] && [ -s "$W/out.json" ]; then
    if [ -s "$KEYF" ] && grep -qF -f "$KEYF" "$W/out.json"; then
      note "ERROR: the result file contains the key: not emitted"; RUN_RC=1
    else
      echo '===VERIFY-EXAMPLES-JSON-BEGIN==='
      cat "$W/out.json"
      echo '===VERIFY-EXAMPLES-JSON-END==='
    fi
  fi
  [ -n "$W" ] && [ -d "$W" ] && { find "$W" -type f -exec shred -u {} + 2>/dev/null; rm -rf "$W"; }
  # a key that was never stored (the mint failed) has nothing to prove; a stored one must be shown refused
  if [ "$MINTED" = 1 ] && { [ "$revoked" != 1 ] || { [ "$HAD_KEY" = 1 ] && [ "$proof" != ok ]; }; }; then
    note "THE KEY IS NOT SHOWN REVOKED: it expires by itself within 1 h; revoke by hand: fleet-runner key revoke $SLUG"
    exit 3
  fi
  [ "$rc" -ne 0 ] && exit "$rc"
  [ "$RUN_RC" -ne 0 ] && exit 1
  exit 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

command -v fleet-runner >/dev/null || fail "fleet-runner is not installed here"
[ -r "$ENVF" ] || fail "$ENVF is not readable"
for t in git curl python3 flock shred; do command -v "$t" >/dev/null || fail "$t is missing"; done
exec 9>"$LOCKF"
flock -n 9 || fail "another verify-examples run holds the lock"

BASE="${VERIFY_EXAMPLES_WORKBASE:-/dev/shm}"; [ -w "$BASE" ] || BASE=/root
W="$(mktemp -d "$BASE/verify-examples.XXXXXX")" || fail "no work directory"
KEYF="$W/key"; HDRF="$W/hdr"
SVC="$W/services.json"; PY="$W/verify-examples.py"

note "fetching origin (remote-tracking refs only; the clone is never checked out)"
timeout 120 git -C "$REPO" fetch -q origin </dev/null 2>&1 | sed 's/^/[git] /' >&2 || note "fetch failed: using the refs the clone already has"
SHA="$(git -C "$REPO" rev-parse --short "$REF" </dev/null 2>/dev/null)" || fail "cannot resolve $REF in $REPO"
git -C "$REPO" show "$REF:services.json" </dev/null > "$SVC" || fail "no services.json at $REF"
git -C "$REPO" show "$REF:bin/verify-examples.py" </dev/null > "$PY" || fail "no bin/verify-examples.py at $REF"
note "ref $REF = $SHA"

# Targets, scope, use limit and the proof URLs, from the SAME verify-examples.py that will run (its own eligible()).
python3 -I - "$PY" "$SVC" "$ONLY" "$W" "$DIRECT" <<'PYEOF'
import importlib.util, json, sys
from urllib.parse import urlsplit
py, svc, only, w, direct = sys.argv[1:6]
spec = importlib.util.spec_from_file_location("verify_examples", py)
ve = importlib.util.module_from_spec(spec); spec.loader.exec_module(ve)
items = ve.load_registry(svc)
want = set(x for x in only.split(",") if x)
targets = [s for s in items if ve.eligible(s) and (not want or (s.get("id") or s.get("slug")) in want)]
missing = want - {(s.get("id") or s.get("slug")) for s in targets}
if missing:
    sys.exit("not an eligible service: " + ",".join(sorted(missing)))
hosts = sorted({urlsplit(s["url"]).netloc for s in targets if str(s.get("url", "")).startswith("https://")})
n = len(targets)
open(w + "/scope", "w").write(",".join(hosts))
open(w + "/count", "w").write(str(n))
# the service with the shortest example path that is reachable both ways, for the revocation proof
cand = [s for s in targets if str(s.get("example_path", "")).startswith("/") and s.get("host_port")
        and s.get("runtime") == "compose" and str(s.get("url", "")).startswith("https://")]
cand.sort(key=lambda s: (len(s["example_path"]), s["id"]))
first_lan = (direct.split(",")[0] if direct else "10.10.10.20")
if cand:
    s = cand[0]
    open(w + "/proof_gw", "w").write("https://%s%s" % (urlsplit(s["url"]).netloc, s["example_path"]))
    open(w + "/proof_lan", "w").write("http://%s:%d%s" % (first_lan, int(s["host_port"]), s["example_path"]))
PYEOF
[ -s "$W/count" ] || fail "cannot compute the targets (see the error above)"
N="$(cat "$W/count")"
SCOPE="$(cat "$W/scope")"
[ "$N" -ge 1 ] || fail "no service to check"
LIMIT=$(( N * 2 + 50 ))
PROOF_GW="$(cat "$W/proof_gw" 2>/dev/null || true)"; PROOF_LAN="$(cat "$W/proof_lan" 2>/dev/null || true)"
[ -n "$PROOF_GW" ] && [ -n "$PROOF_LAN" ] || fail "no service to prove the revocation with"
BEFORE_GW="-"; BEFORE_LAN="-"
note "targets: $N services, $(tr ',' '\n' <<<"$SCOPE" | wc -l) hostnames in scope, ttl 1h, use limit $LIMIT"

# An active key of this slug is a leftover of a crashed run (the lock is ours): it is revoked before a new one is minted.
ACTIVE="$(fr key list --json --filter "$SLUG" 2>/dev/null | python3 -I -c '
import json, sys, datetime
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
try:
    keys = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
print(sum(1 for k in keys if k.get("user") == "examples-verifier" and not k.get("revoked_at") and (k.get("expires_at") or "9") > now))
')"
ACTIVE="${ACTIVE:-0}"

if [ "${DRY:-0}" = 1 ]; then
  note "DRY RUN: nothing is minted or run"
  note "active $SLUG keys now: $ACTIVE (a real run revokes them first)"
  note "would mint: fleet-runner key issue $SLUG --user $SLUG --scope <$(tr ',' '\n' <<<"$SCOPE" | wc -l) hostnames> --ttl 1h --use-limit $LIMIT"
  note "would run:  verify-examples.py --services <$REF:services.json> --api-key-file <key> ${DIRECT:+--direct-hosts $DIRECT }${ONLY:+--only $ONLY}"
  note "revocation proof would use: gateway $(sed 's#^\(https://[^/]*\).*#\1#' <<<"$PROOF_GW") and service $(sed 's#^\(http://[^/]*\).*#\1#' <<<"$PROOF_LAN")"
  trap - EXIT
  [ -n "$W" ] && rm -rf "$W"
  exit 0
fi

while [ "$ACTIVE" -gt 0 ]; do
  note "revoking a leftover active $SLUG key"
  fr key revoke "$SLUG" >/dev/null 2>&1 || fail "cannot revoke the leftover $SLUG key"
  ACTIVE=$(( ACTIVE - 1 ))
done

MINTED=1   # set before the call: a key that was minted but not stored is still revoked by slug
: > "$KEYF"
fr key issue "$SLUG" --user "$SLUG" --scope "$SCOPE" --ttl 1h --use-limit "$LIMIT" \
   --note "verify-examples-run $(date -u +%FT%TZ) $SHA GET only, read-only use" >"$KEYF" 2>"$W/issue.log" \
   || { python3 -I - "$KEYF" "$W/issue.log" <<'PYEOF'
import sys
k = open(sys.argv[1]).read().strip()
m = open(sys.argv[2]).read()
print("[builder] mint failed: " + (m.replace(k, "<key>") if k else m).strip()[:300], file=sys.stderr)
PYEOF
        fail "key issue failed"; }
[ -s "$KEYF" ] || fail "key issue printed no key"
printf 'X-API-Key: %s\n' "$(tr -d '\n' < "$KEYF")" > "$HDRF"
note "key minted (user $SLUG, ttl 1h, use limit $LIMIT); it is shown nowhere"

BEFORE_GW="$(code "$PROOF_GW")"; BEFORE_LAN="$(code "$PROOF_LAN")"
note "control before the run: gateway $BEFORE_GW, service $BEFORE_LAN (a 401 here makes that side's proof worthless)"

note "running verify-examples.py"
# In the background and waited for: bash runs a trap only after a foreground command ends, and a TERM must revoke at once.
python3 -I "$PY" --services "$SVC" --out "$W/out.json" --api-key-file "$KEYF" \
   ${DIRECT:+--direct-hosts "$DIRECT"} ${ONLY:+--only "$ONLY"} </dev/null &
PYPID=$!
wait "$PYPID"
RUN_RC=$?
PYPID=""
[ "$RUN_RC" = 0 ] || note "verify-examples.py exited $RUN_RC"
exit "$RUN_RC"
}
main </dev/null
REMOTE
} > "$TMP/remote.sh"

if [ "$PRINT_REMOTE" = 1 ]; then cat "$TMP/remote.sh"; exit 0; fi
echo "verify-examples-run: builder $BUILDER ct $CT, ref $REF${ONLY:+, only $ONLY}$([ "$DRY" = 1 ] && echo ', DRY')" >&2
set +e
ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 "$BUILDER" "pct exec $CT -- bash -s" < "$TMP/remote.sh" > "$TMP/stdout" 2> >(cat >&2)
RC=$?
set -e

if [ "$DRY" = 1 ]; then exit "$RC"; fi

# Split the result out of the remote stdout (the remote prints nothing else there).
python3 -I - "$TMP/stdout" "$TMP/new.json" <<'PY' || { echo "verify-examples-run: no result came back (exit $RC)" >&2; exit "$([ "$RC" -ne 0 ] && echo "$RC" || echo 1)"; }
import sys
b, e = "===VERIFY-EXAMPLES-JSON-BEGIN===", "===VERIFY-EXAMPLES-JSON-END==="
lines = open(sys.argv[1]).read().split("\n")
if b not in lines or e not in lines:
    sys.exit(1)
open(sys.argv[2], "w").write("\n".join(lines[lines.index(b) + 1:lines.index(e)]) + "\n")
PY

python3 -I - "$OLD" "$TMP/new.json" "$ONLY" <<'PY'
import json, sys
old = json.load(open(sys.argv[1])).get("services", {})
new_doc = json.load(open(sys.argv[2]))
new = new_doc["services"]
only = set(x for x in sys.argv[3].split(",") if x)
if only:                       # a partial run: compare and (below) merge only the services it checked
    old = {k: v for k, v in old.items() if k in only}
tally = {}
for r in new.values():
    tally[r["state"]] = tally.get(r["state"], 0) + 1
print("tally (%d services): %s" % (len(new), json.dumps(dict(sorted(tally.items())))))
div = sorted(k for k, r in new.items() if r.get("diverged"))
print("replicas: %d answered on more than one host; diverged: %s" % (
    sum(1 for r in new.values() if r.get("replicas")), ", ".join(div) or "none"))
changed = [(k, old.get(k, {}).get("state", "(new)"), new[k]["state"]) for k in sorted(new)
           if old.get(k, {}).get("state") != new[k]["state"]]
print("changed states versus the committed file: %d" % len(changed))
for k, a, b in changed:
    print("  %-40s %s -> %s" % (k, a, b))
for k in sorted(set(old) - set(new)):
    print("  %-40s %s -> (not checked)" % (k, old[k].get("state")))
PY

# A full run replaces the file; a partial one (--only) updates only the services it checked, so the rest of the file stays.
python3 -I - "$OUT" "$TMP/new.json" "$ONLY" <<'PY'
import json, os, sys
out, new_path, only = sys.argv[1], sys.argv[2], sys.argv[3]
new = json.load(open(new_path))
if only and os.path.exists(out):
    cur = json.load(open(out))
    cur["services"].update(new["services"])
    cur["services"] = {k: cur["services"][k] for k in sorted(cur["services"])}
    cur["checked_at"] = new["checked_at"]
    new = cur
open(out, "w").write(json.dumps(new, indent=2) + "\n")
print("wrote %s" % out, file=sys.stderr)
PY
exit "$RC"
