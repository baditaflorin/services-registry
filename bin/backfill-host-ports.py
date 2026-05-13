#!/usr/bin/env python3
"""
backfill-host-ports.py — discover live host_port for each service and update overrides.json.

Single source of truth shift: stop hand-editing nginx vhosts. Instead, populate
services.json with host_port (canonical), render vhosts from a template via
`fleet-runner nginx-render`, push.

This script seeds the canonical record from current live state:
  1. SSH to dockerhost, run `docker ps`.
  2. For each container, derive the slug (strip go_ prefix, _ → -).
  3. Cross-ref with services.json's existing repo_url → slug.
  4. Parse the FIRST host-bound port (NNNN->XXXX/tcp).
  5. If new info: add host_port + container_port to overrides.json.
  6. Print a summary diff. Doesn't run generate.py — operator does that.

Read-only safe by default. Pass --apply to actually write overrides.json.

Usage:
  bin/backfill-host-ports.py                 # dry-run, prints diff
  bin/backfill-host-ports.py --apply         # writes overrides.json
  bin/backfill-host-ports.py --bastion root@0docker.com --dockerhost ubuntu_vm@10.10.10.20
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES_JSON = ROOT / "services.json"
OVERRIDES_JSON = ROOT / "overrides.json"

PORT_RX = re.compile(r"(?:(?:[\d.]+):)?(\d+)->(\d+)/tcp")


def ssh(bastion: str, target: str, cmd: str) -> str:
    args = ["ssh"]
    if bastion:
        args += ["-J", bastion]
    args += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, cmd]
    return subprocess.check_output(args, text=True)


def docker_ports(bastion: str, dockerhost: str) -> dict[str, tuple[int, int]]:
    """Returns {container_name: (host_port, container_port)} for each container."""
    out = ssh(bastion, dockerhost,
              r"docker ps --format '{{.Names}}|{{.Ports}}' 2>/dev/null")
    result = {}
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, ports = line.split("|", 1)
        m = PORT_RX.search(ports)
        if not m:
            continue
        host_port = int(m.group(1))
        container_port = int(m.group(2))
        result[name.strip()] = (host_port, container_port)
    return result


def slug_candidates(container_name: str) -> list[str]:
    """A container is typically named go_<repo>-app-1 or go-<repo>-app-1.
    Strip docker-compose suffixes and prefix variants, normalize to kebab-case."""
    s = container_name
    s = re.sub(r"-app-\d+$", "", s)
    s = re.sub(r"-\d+$", "", s)
    s = re.sub(r"_\d+$", "", s)
    s = s.replace("_", "-")
    out = [s, s.lstrip("go-")]
    return list(dict.fromkeys(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bastion", default="root@0docker.com")
    ap.add_argument("--dockerhost", default="ubuntu_vm@10.10.10.20")
    ap.add_argument("--apply", action="store_true", help="write overrides.json (default: dry-run)")
    args = ap.parse_args()

    if not SERVICES_JSON.exists():
        print(f"ERROR: {SERVICES_JSON} not found", file=sys.stderr)
        return 2
    services = json.loads(SERVICES_JSON.read_text())
    overrides = json.loads(OVERRIDES_JSON.read_text(), object_pairs_hook=OrderedDict)

    # Build slug → service map
    slug_to_service = {s["id"]: s for s in services}

    print(f"Probing dockerhost {args.dockerhost}…", file=sys.stderr)
    try:
        ports = docker_ports(args.bastion, args.dockerhost)
    except subprocess.CalledProcessError as e:
        print(f"ssh failed: {e}", file=sys.stderr)
        return 2
    print(f"Found {len(ports)} containers with host port bindings.", file=sys.stderr)

    added = updated = matched = unmatched = 0
    diffs: list[str] = []
    unmatched_containers: list[str] = []
    for cname, (hp, cp) in ports.items():
        candidates = slug_candidates(cname)
        found_slug = next((c for c in candidates if c in slug_to_service), None)
        if not found_slug:
            unmatched += 1
            unmatched_containers.append(f"{cname} → {candidates[0]} (no service)")
            continue
        matched += 1
        cur = slug_to_service[found_slug]
        cur_hp = cur.get("host_port")
        cur_cp = cur.get("container_port")
        if cur_hp == hp and cur_cp == cp:
            continue
        if found_slug not in overrides:
            overrides[found_slug] = OrderedDict()
        overrides[found_slug]["host_port"] = hp
        overrides[found_slug]["container_port"] = cp
        if cur_hp is None:
            added += 1
            diffs.append(f"  + {found_slug}: host_port={hp} container_port={cp}  (container={cname})")
        else:
            updated += 1
            diffs.append(f"  ~ {found_slug}: host_port {cur_hp}→{hp}  container_port {cur_cp}→{cp}")

    print(f"\nResult: matched {matched}, unmatched {unmatched}")
    print(f"  added: {added}, updated: {updated}, unchanged: {matched - added - updated}")
    if diffs:
        print("\nProposed changes:")
        for d in diffs[:40]:
            print(d)
        if len(diffs) > 40:
            print(f"  …and {len(diffs) - 40} more")
    if unmatched_containers:
        print(f"\nUnmatched containers ({len(unmatched_containers)} — investigate if any are real fleet services):")
        for u in unmatched_containers[:20]:
            print(f"  ? {u}")
        if len(unmatched_containers) > 20:
            print(f"  …and {len(unmatched_containers) - 20} more")

    if args.apply and diffs:
        out = OrderedDict((k, overrides[k]) for k in sorted(overrides.keys()))
        OVERRIDES_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        print(f"\n✓ wrote {OVERRIDES_JSON}. Now run: python3 bin/generate.py")
    elif not args.apply and diffs:
        print(f"\nDry-run only. Re-run with --apply to write.")


if __name__ == "__main__":
    raise SystemExit(main())
