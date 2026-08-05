#!/usr/bin/env python3
"""
MCP-readiness audit — mirrors go-fleet-mcp-gateway's own live discovery
into the registry, rather than re-probing 447 services independently.

The gateway (github.com/baditaflorin/go-fleet-mcp-gateway) already
scans services.json every REGISTRY_REFRESH and live-probes every
kind=container service's GET /agent.json — that's the authoritative
"is this service actually MCP-ready right now" answer. This script
just reads GET /gateway/tools and writes what it found into the
registry, so the hub's directory filter and services.mcp.json don't
need their own separate probing logic (and can't drift from what the
gateway itself is actually serving).

Why this patches services.json directly instead of only overrides.json
+ a full `bin/generate.py` run: as of 2026-08-05, overrides.json's
trl_* fields are stale relative to services.json for a large number of
existing services (some by months) — a full regen sources TRL from
overrides.json and would silently regress already-merged TRL audit
work fleet-wide. See services-registry PR #33/#34 for the incident
this came out of. Until that's fixed, every registry write in this
repo goes through the same two-step pattern: patch overrides.json (the
source of truth for the NEXT full regen, whenever it's safe to run)
AND patch services.json directly (what's actually served today), then
rebuild only the derived slices with --slices-only (pure projection,
no repo re-scan, cannot regress unrelated fields).

Usage:
    python3 bin/audit_mcp_ready.py [--gateway-url URL] [--api-key KEY] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES_JSON = ROOT / "services.json"
OVERRIDES_JSON = ROOT / "overrides.json"

DEFAULT_GATEWAY_URL = "https://fleet-mcp-gateway.0exec.com/gateway/tools"
DEFAULT_API_KEY = "default_token"


def fetch_gateway_tools(url: str, api_key: str) -> dict:
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}api_key={api_key}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def tool_counts_by_service(payload: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in payload.get("tools", []):
        svc = t.get("service")
        if svc:
            counts[svc] = counts.get(svc, 0) + 1
    return counts


def apply_patch(entries: list[dict], counts: dict[str, int], assessed_at: str) -> int:
    """Mutates entries in place. Returns the number changed."""
    by_id = {e["id"]: e for e in entries if "id" in e}
    changed = 0
    for svc_id, count in counts.items():
        e = by_id.get(svc_id)
        if e is None:
            continue  # gateway found a service the registry doesn't know about; skip, don't invent an entry
        if e.get("mcp_ready") is not True or e.get("mcp_tool_count") != count:
            changed += 1
        e["mcp_ready"] = True
        e["mcp_tool_count"] = count
        e["mcp_assessed_at"] = assessed_at
    # Services that WERE mcp_ready but the gateway no longer reports —
    # drop the flag rather than leave a stale "true". Per schema: absence
    # means "not currently assessed as ready", not "confirmed not ready".
    for e in entries:
        if e.get("mcp_ready") is True and e["id"] not in counts:
            del e["mcp_ready"], e["mcp_tool_count"], e["mcp_assessed_at"]
            changed += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-url", default=DEFAULT_GATEWAY_URL)
    ap.add_argument("--api-key", default=DEFAULT_API_KEY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = fetch_gateway_tools(args.gateway_url, args.api_key)
    counts = tool_counts_by_service(payload)
    assessed_at = payload.get("refreshed_at") or ""

    print(f"gateway reports {payload.get('services_reachable', '?')} reachable "
          f"services, {len(counts)} contributing tools, refreshed_at={assessed_at}")

    if not counts:
        print("nothing to do — no services currently reachable via the gateway")
        return 0

    services = json.loads(SERVICES_JSON.read_text())
    overrides = json.loads(OVERRIDES_JSON.read_text())

    n_services = apply_patch(services, counts, assessed_at)

    # overrides.json entries are {slug: {...patch...}}; create the slug
    # key if it doesn't exist yet rather than requiring pre-registration.
    n_overrides = 0
    for svc_id, count in counts.items():
        ov = overrides.setdefault(svc_id, {})
        if ov.get("mcp_ready") is not True or ov.get("mcp_tool_count") != count:
            n_overrides += 1
        ov["mcp_ready"] = True
        ov["mcp_tool_count"] = count
        ov["mcp_assessed_at"] = assessed_at
    for svc_id, ov in list(overrides.items()):
        if svc_id.startswith("$"):
            continue
        if isinstance(ov, dict) and ov.get("mcp_ready") is True and svc_id not in counts:
            del ov["mcp_ready"], ov["mcp_tool_count"], ov["mcp_assessed_at"]
            n_overrides += 1

    print(f"services.json: {n_services} entries changed; overrides.json: {n_overrides} entries changed")

    if args.dry_run:
        print("--dry-run: not writing")
        return 0

    if n_services == 0 and n_overrides == 0:
        print("no changes")
        return 0

    SERVICES_JSON.write_text(json.dumps(services, indent=2) + "\n")
    OVERRIDES_JSON.write_text(json.dumps(overrides, indent=2, ensure_ascii=False) + "\n")

    subprocess.run([sys.executable, str(ROOT / "bin" / "generate.py"), "--slices-only"], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
