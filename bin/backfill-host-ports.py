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
SS_LISTEN_RX = re.compile(
    r'LISTEN\s+\d+\s+\d+\s+[^\s]*:(\d+)\s+[^\s]+\s+users:\(\("([^"]+)"',
)


def ssh(bastion: str, target: str, cmd: str) -> str:
    args = ["ssh"]
    if bastion:
        args += ["-J", bastion]
    args += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, cmd]
    return subprocess.check_output(args, text=True)


def docker_ports(bastion: str, dockerhost: str) -> dict[str, tuple[int, int]]:
    """Returns {container_name: (host_port, container_port)} for each docker
    container with a published host port mapping."""
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


def native_listeners(bastion: str, dockerhost: str) -> dict[str, int]:
    """Returns {process_name: host_port} for native (non-docker) processes
    listening on TCP on the dockerhost. Catches host-network containers and
    binaries running outside docker, which `docker ps` misses.

    Agent F flagged this gap on 2026-05-13: 7 of 14 'broken' services were
    actually serving fine via native processes; the audit only saw docker.
    """
    # -tnlp: TCP, numeric, listening, with-process. Requires sudo for full info.
    out = ssh(bastion, dockerhost, "sudo ss -tlnp 2>/dev/null || ss -tlnp 2>/dev/null")
    result: dict[str, int] = {}
    for line in out.splitlines():
        m = SS_LISTEN_RX.search(line)
        if not m:
            continue
        port = int(m.group(1))
        proc = m.group(2).strip()
        # Process names from ss are truncated to 15 chars on linux. Keep
        # whichever first-seen entry; subsequent listens on the same name
        # are usually IPv6 echoes.
        if proc not in result:
            result[proc] = port
    return result


# Mirror of SLUG_OVERRIDES in bin/generate.py — Python doesn't see the Go map,
# so keep these in lockstep. Any name here is the canonical slug the registry
# uses (not the auto-derivation from repo name).
SLUG_OVERRIDES = {
    "go_jsbundle_secrets":           "jsbundle-secrets",
    "go_jsbundle_route_extractor":   "jsbundle-routes",
    "go_postmessage_listener_finder":"postmessage",
    "go_prototype_pollution_static": "proto-pollution",
    "go_jwt_pentest":                "jwt-pentest",
    "go_session_fixation":           "session-fixation",
}


def slug_candidates(container_name: str) -> list[str]:
    """A container is typically named go_<repo>-app-1 or go-<repo>-app-1.
    Strip docker-compose suffixes and prefix variants. Return canonical
    candidates including any SLUG_OVERRIDES that match the underlying repo
    name."""
    s = container_name
    s = re.sub(r"-app-\d+$", "", s)
    s = re.sub(r"-\d+$", "", s)
    s = re.sub(r"_\d+$", "", s)
    # Possible repo names: as-stripped (with underscores) or kebab.
    repo_candidates = {s, s.replace("-", "_")}
    out = []
    for repo in repo_candidates:
        if repo in SLUG_OVERRIDES:
            out.append(SLUG_OVERRIDES[repo])
    kebab = s.replace("_", "-")
    out.extend([kebab, kebab.lstrip("go-")])
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

    print(f"Probing dockerhost {args.dockerhost} (docker ps + ss -tlnp)…", file=sys.stderr)
    try:
        ports = docker_ports(args.bastion, args.dockerhost)
        natives = native_listeners(args.bastion, args.dockerhost)
    except subprocess.CalledProcessError as e:
        print(f"ssh failed: {e}", file=sys.stderr)
        return 2
    print(f"  docker ps: {len(ports)} containers with published ports", file=sys.stderr)
    print(f"  ss -tlnp:  {len(natives)} native TCP listeners", file=sys.stderr)

    # Synthesize a unified view: docker first (richer container_port info),
    # then native fillers for what docker missed.
    # Native proc name is truncated to 15 chars (linux comm limit), so we
    # match against slug + repo-name candidates fuzzily.
    used_native_procs: set[str] = set()

    added = updated = matched = unmatched = native_added = 0
    diffs: list[str] = []
    unmatched_containers: list[str] = []
    matched_slugs: set[str] = set()
    for cname, (hp, cp) in ports.items():
        candidates = slug_candidates(cname)
        found_slug = next((c for c in candidates if c in slug_to_service), None)
        if not found_slug:
            unmatched += 1
            unmatched_containers.append(f"{cname} → {candidates[0]} (no service)")
            continue
        matched += 1
        matched_slugs.add(found_slug)
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
            diffs.append(f"  + {found_slug}: host_port={hp} container_port={cp}  (docker={cname})")
        else:
            updated += 1
            diffs.append(f"  ~ {found_slug}: host_port {cur_hp}→{hp}  container_port {cur_cp}→{cp}")

    # Pass 2: fill in native processes for services docker didn't cover.
    # Process names are truncated to 15 chars (e.g. go_captcha_dete for
    # go_captcha_detector). Match against the slug's underscored form and
    # accept a prefix match.
    for proc, port in natives.items():
        # Build the canonical "go_<slug_with_underscores>" form for matching.
        # ss may report short names, so we match by prefix.
        for slug, svc in slug_to_service.items():
            if slug in matched_slugs:
                continue
            if svc.get("host_port") == port:
                continue  # already known
            expected = "go_" + slug.replace("-", "_")
            # Prefix-match either way (ss is truncated, slug may be shorter).
            if proc.startswith(expected[:15]) or expected.startswith(proc):
                if slug not in overrides:
                    overrides[slug] = OrderedDict()
                cur_hp = svc.get("host_port")
                overrides[slug]["host_port"] = port
                # container_port unknown for native; only set if absent.
                if "container_port" not in overrides[slug] and svc.get("container_port") is None:
                    overrides[slug]["container_port"] = port
                if cur_hp is None:
                    native_added += 1
                    diffs.append(f"  + {slug}: host_port={port}  (NATIVE proc={proc})")
                elif cur_hp != port:
                    updated += 1
                    diffs.append(f"  ~ {slug}: host_port {cur_hp}→{port}  (NATIVE proc={proc})")
                matched_slugs.add(slug)
                used_native_procs.add(proc)
                break

    print(f"\nResult: matched {matched} via docker + {native_added} via native ss -tlnp; unmatched docker {unmatched}")
    print(f"  added: {added + native_added}, updated: {updated}, unchanged: ~{matched - added - updated}")
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
