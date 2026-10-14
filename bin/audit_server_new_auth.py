#!/usr/bin/env python3
"""Find services that build their HTTP server with `server.New` but never install API-key auth.

`server.Run` / `server.RunTier` prepend `WithKeystoreAuth` for the service; a service that calls
`server.New(cfg, ...)` and registers its own routes only gets auth if it adds a keystore option itself.
On compose hosts nginx checks keys at the edge, but a Coolify host has no such check, so on
`runtime: coolify` a missing option means an unauthenticated service (found 2026-10-14: api-extractor,
cors-tester, crypto-finder, wp-plugins).

Usage:
  bin/audit_server_new_auth.py --github [--runtime coolify] [--fail-on coolify]
  bin/audit_server_new_auth.py --repos /path/to/clones [--runtime coolify]

Reads main.go (or cmd/server/main.go) of each registry service whose `auth.type` is `api_key`.
Exit status 1 when a flagged service has a runtime listed in --fail-on.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

AUTH_MARKERS = (
    "WithKeystoreAuth", "WithKeystoreAuthTier", "WithKeystoreAuthMesh",
    "TokenAuthKeystore", "server.Run(", "server.RunTier(",
)
MAIN_CANDIDATES = ("main.go", "cmd/server/main.go", "cmd/main.go")


def classify(source: str) -> str:
    """Return 'ok', 'no-auth-option' or 'not-server-new' for one Go source file."""
    if any(marker in source for marker in AUTH_MARKERS):
        return "ok"
    if re.search(r"\bserver\.New\(", source):
        return "no-auth-option"
    return "not-server-new"


def repo_name(entry: dict) -> str:
    url = (entry.get("repo_url") or "").rstrip("/")
    return url.rsplit("/", 1)[-1].removesuffix(".git") if url else ""


def read_github(entry: dict, path: str) -> str | None:
    owner_repo = "/".join((entry["repo_url"].rstrip("/").split("/")[-2:])).removesuffix(".git")
    out = subprocess.run(
        ["gh", "api", f"repos/{owner_repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True, timeout=60)
    return out.stdout if out.returncode == 0 else None


def read_local(root: Path, entry: dict, path: str) -> str | None:
    f = root / repo_name(entry) / path
    return f.read_text(errors="replace") if f.is_file() else None


def audit_entry(entry: dict, reader) -> dict:
    result = {"id": entry["id"], "runtime": entry.get("runtime"), "status": "no-source"}
    for path in MAIN_CANDIDATES:
        src = reader(entry, path)
        if src is None:
            continue
        status = classify(src)
        result.update(status=status, file=path)
        if status != "not-server-new":
            break
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", default=str(Path(__file__).resolve().parent.parent / "services.json"))
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--github", action="store_true", help="read main.go through `gh api`")
    src.add_argument("--repos", help="directory holding clones named like the repos")
    ap.add_argument("--runtime", action="append", help="limit to these runtimes (repeatable)")
    ap.add_argument("--fail-on", action="append", default=[], help="exit 1 if a flagged service has this runtime")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    data = json.loads(Path(args.registry).read_text())
    entries = data if isinstance(data, list) else data.get("services", data)
    todo = [e for e in entries
            if (e.get("auth") or {}).get("type") == "api_key" and e.get("repo_url")
            and e.get("runtime") in (args.runtime or ("coolify", "compose"))]
    reader = (read_github if args.github else (lambda e, p, r=Path(args.repos): read_local(r, e, p)))
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(lambda e: audit_entry(e, reader), todo))

    flagged = [r for r in results if r["status"] == "no-auth-option"]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"audited {len(results)} services: {counts}")
    for r in sorted(flagged, key=lambda r: (r["runtime"] or "", r["id"])):
        print(f"FLAG {r['runtime']:8} {r['id']}  ({r['file']}: server.New without a keystore auth option)")
    return 1 if any(r["runtime"] in args.fail_on for r in flagged) else 0


if __name__ == "__main__":
    sys.exit(main())
