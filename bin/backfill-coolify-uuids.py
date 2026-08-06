#!/usr/bin/env python3
"""
backfill-coolify-uuids.py — populate coolify_app_uuid for runtime=coolify
services by matching against Coolify's own application list.

Companion to bin/backfill-host-ports.py, same shape: read-only by
default, --apply writes overrides.json, operator runs generate.py after.

Why: services tagged runtime=coolify in overrides.json (see the
security-recon-coolify-lv3 $rules entry) are deployed through Coolify's
own API instead of fleet-runner's SSH+docker-compose path
(go_fleet_runner/deploy_coolify.go). That backend needs to know WHICH
Coolify application UUID corresponds to each fleet registry id.

Confirmed 2026-08-06: Coolify's `applications[].name` field is an exact
match for the fleet registry `id` for every service checked (they were
migrated in with that naming preserved on purpose — see each
application's `description` field, e.g. "ws-0481 fleet migration:
admin-finder migrated from 0docker"). So the match here is a plain
name == id lookup, no fuzzy port-matching needed (unlike the earlier
port-based cross-reference used to discover the runtime=coolify set in
the first place).

Coolify's API (lv3 cluster, VM 170 "coolify-lv3", port 8000) is only
reachable from inside that Proxmox host's private LAN — same
reachability constraint as go_fleet_runner/coolify.go, so this script
shells the same curl-over-SSH-to-the-hypervisor pattern rather than a
direct HTTP client.

Usage:
  bin/backfill-coolify-uuids.py                # dry-run, prints diff + report
  bin/backfill-coolify-uuids.py --apply        # writes overrides.json

Requires $COOLIFY_API_TOKEN (see bin/fleet-runner.env.example).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES_JSON = ROOT / "services.json"
OVERRIDES_JSON = ROOT / "overrides.json"

# No hardcoded default here on purpose — this repo is PUBLIC. The actual
# bastion + internal API address live in private fleet-state/OPS.md
# (and as real defaults in the private go_fleet_runner repo's coolify.go,
# which this script's SSH/curl shape mirrors). Both env vars are required.
DEFAULT_BASTION = os.environ.get("COOLIFY_BASTION", "")
DEFAULT_API_URL = os.environ.get("COOLIFY_API_URL", "")


def ssh_curl(bastion: str, api_url: str, token: str, path: str, identity: str = "") -> str:
    cmd = f"curl -sS -H 'Authorization: Bearer {token}' --max-time 15 {api_url}{path}"
    args = ["ssh"]
    if identity:
        args += ["-i", identity, "-o", "IdentitiesOnly=yes"]
    args += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", bastion, cmd]
    return subprocess.check_output(args, text=True)


def list_coolify_applications(bastion: str, api_url: str, token: str, identity: str = "") -> list[dict]:
    out = ssh_curl(bastion, api_url, token, "/api/v1/applications", identity)
    apps = json.loads(out)
    if not isinstance(apps, list):
        sys.exit(f"ERROR: unexpected /api/v1/applications response shape: {out[:300]}")
    return apps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bastion", default=DEFAULT_BASTION,
                     help="lv3 Proxmox SSH bastion (or $COOLIFY_BASTION). See fleet-state/OPS.md.")
    ap.add_argument("--api-url", default=DEFAULT_API_URL,
                     help="Coolify API base URL (or $COOLIFY_API_URL). See fleet-state/OPS.md.")
    ap.add_argument("--apply", action="store_true", help="write overrides.json (default: dry-run)")
    ap.add_argument("--ssh-identity", default=os.environ.get("COOLIFY_SSH_IDENTITY", ""),
                     help="explicit SSH private key path, if the bastion isn't already reachable via "
                          "ssh-agent/~/.ssh/config (or $COOLIFY_SSH_IDENTITY)")
    args = ap.parse_args()

    if not args.bastion:
        sys.exit("ERROR: --bastion required (or set $COOLIFY_BASTION). See fleet-state/OPS.md — "
                  "this repo is public, so the address isn't hardcoded here.")
    if not args.api_url:
        sys.exit("ERROR: --api-url required (or set $COOLIFY_API_URL). See fleet-state/OPS.md — "
                  "this repo is public, so the address isn't hardcoded here.")

    token = os.environ.get("COOLIFY_API_TOKEN", "")
    if not token:
        sys.exit("ERROR: $COOLIFY_API_TOKEN not set. See bin/fleet-runner.env.example.")

    if not SERVICES_JSON.exists():
        print(f"ERROR: {SERVICES_JSON} not found", file=sys.stderr)
        return 2
    services = json.loads(SERVICES_JSON.read_text())
    overrides = json.loads(OVERRIDES_JSON.read_text(), object_pairs_hook=OrderedDict)

    coolify_ids = {s["id"]: s for s in services if s.get("runtime") == "coolify"}
    print(f"registry: {len(coolify_ids)} services tagged runtime=coolify", file=sys.stderr)

    print(f"Fetching Coolify applications via {args.bastion} → {args.api_url} …", file=sys.stderr)
    try:
        apps = list_coolify_applications(args.bastion, args.api_url, token, args.ssh_identity)
    except subprocess.CalledProcessError as e:
        print(f"ssh/curl failed: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"Coolify response wasn't valid JSON (bad/missing token?): {e}", file=sys.stderr)
        return 2
    print(f"coolify: {len(apps)} applications", file=sys.stderr)

    apps_by_name = {a["name"]: a for a in apps if a.get("name")}

    added = updated = unchanged = 0
    diffs: list[str] = []
    no_coolify_match: list[str] = []
    for slug in sorted(coolify_ids):
        app = apps_by_name.get(slug)
        if not app:
            no_coolify_match.append(slug)
            continue
        uuid = app["uuid"]
        cur = overrides.get(slug, {}).get("coolify_app_uuid")
        if cur == uuid:
            unchanged += 1
            continue
        if slug not in overrides:
            overrides[slug] = OrderedDict()
        overrides[slug]["coolify_app_uuid"] = uuid
        if cur is None:
            added += 1
            diffs.append(f"  + {slug}: coolify_app_uuid={uuid}")
        else:
            updated += 1
            diffs.append(f"  ~ {slug}: coolify_app_uuid {cur}→{uuid}")

    no_registry_match = sorted(set(apps_by_name) - set(coolify_ids))

    print(f"\nResult: added {added}, updated {updated}, unchanged {unchanged}")
    if diffs:
        print("\nProposed changes:")
        for d in diffs[:60]:
            print(d)
        if len(diffs) > 60:
            print(f"  …and {len(diffs) - 60} more")

    if no_coolify_match:
        print(f"\nregistry runtime=coolify ids with NO matching Coolify app ({len(no_coolify_match)}) — investigate, don't guess:")
        for s in no_coolify_match:
            print(f"  ? {s}")

    if no_registry_match:
        print(f"\nCoolify apps with NO runtime=coolify registry match ({len(no_registry_match)}) — either a non-fleet app on this instance, or a service missing the runtime=coolify tag:")
        for s in no_registry_match[:30]:
            print(f"  ? {s}")
        if len(no_registry_match) > 30:
            print(f"  …and {len(no_registry_match) - 30} more")

    if args.apply and diffs:
        out = OrderedDict((k, overrides[k]) for k in sorted(overrides.keys()))
        OVERRIDES_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        print(f"\n✓ wrote {OVERRIDES_JSON}.")
        print("  overrides.json is the source of truth, but services.json is what")
        print("  fleet-runner actually reads at deploy time — it does NOT auto-sync from")
        print("  overrides.json. Two ways to land the new coolify_app_uuid values there:")
        print("    - full regen: python3 bin/generate.py  (re-applies ALL overrides —")
        print("      as of 2026-08-06 this pulled in unrelated stale TRL/description")
        print("      drift from generate.py's live GitHub-fetch source; diff carefully")
        print("      before committing, or hand-patch just the new field like the")
        print("      companion services-registry PR #47 did for the runtime field)")
        print("    - --slices-only only rebuilds the derived slices FROM the existing")
        print("      services.json — it will NOT pick up these overrides.json changes")
        print("      on its own; run it only after services.json itself is updated.")
    elif not args.apply and diffs:
        print("\nDry-run only. Re-run with --apply to write.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
