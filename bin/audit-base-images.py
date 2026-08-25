#!/usr/bin/env python3
"""
Base-image audit — for every container-kind service in the registry,
read its Dockerfile's final-stage FROM line and classify whether
switching to a distroless base would actually win anything.

Distroless only helps when the final stage is (a) Debian/Ubuntu-based
today, since that's where the real image-size/attack-surface delta is,
and (b) doesn't apk/apt-get install runtime system packages (browser
engines, fonts, codecs, ...) that distroless has no package manager to
install. An already-alpine static Go binary gains ~nothing at runtime
(alpine is already ~5-8MB; distroless/static is ~2MB) — same CPU, same
behavior, just fewer bytes to pull and a smaller CVE surface.

Reads the Dockerfile from the local workspace clone when present
(/root/workspace/<repo>/Dockerfile — fast, no API calls, matches how
this box already has ~360 repos checked out), and falls back to
`gh api repos/<repo>/contents/Dockerfile` for anything not cloned
locally. Both paths are deterministic: same repo state in, same
verdict out, no judgment calls made per-run.

Usage:
    python3 bin/audit-base-images.py [--json] [--limit N] [--workspace DIR]
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SERVICES_JSON = ROOT / "services.json"
DEFAULT_WORKSPACE = Path("/root/workspace")

# Runtime packages that mean "needs a full OS", not installable in a
# distroless image (no package manager, no shell to run apk/apt in).
HEAVY_RUNTIME_MARKERS = (
    "chromium", "chrome", "playwright", "puppeteer", "firefox",
    "ffmpeg", "imagemagick", "graphicsmagick", "libreoffice",
    "poppler", "ghostscript", "wkhtmltopdf",
)

# Packages that are themselves distroless-safe to drop (already covered
# by distroless's own minimal cert/tz baseline, or trivial to vendor).
BENIGN_MARKERS = (
    "ca-certificates", "tini", "dumb-init", "tzdata", "wget", "curl",
    "git", "netcat", "bash", "shadow",
)

DEBIAN_FAMILY = ("debian", "ubuntu", "jammy", "bullseye", "bookworm", "focal", "noble")


def repo_name_from_url(repo_url: str) -> str | None:
    if not repo_url:
        return None
    path = urlparse(repo_url).path.strip("/")
    if not path:
        return None
    return path.split("/")[-1]


def read_dockerfile_local(repo: str, workspace: Path) -> str | None:
    p = workspace / repo / "Dockerfile"
    if p.is_file():
        try:
            return p.read_text(errors="replace")
        except OSError:
            return None
    return None


def read_dockerfile_gh(repo_url: str) -> str | None:
    path = urlparse(repo_url).path.strip("/")
    if not path:
        return None
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{path}/contents/Dockerfile", "--jq", ".content"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return base64.b64decode(out.stdout.strip()).decode(errors="replace")
    except Exception:
        return None


def final_stage_from(dockerfile: str) -> str | None:
    from_lines = re.findall(r"^\s*FROM\s+(\S+)(?:\s+AS\s+\S+)?", dockerfile, re.IGNORECASE | re.MULTILINE)
    return from_lines[-1] if from_lines else None


def final_stage_body(dockerfile: str) -> str:
    """Text of the last FROM..end block, for scanning RUN apk/apt lines."""
    blocks = re.split(r"^\s*FROM\s+", dockerfile, flags=re.IGNORECASE | re.MULTILINE)
    return blocks[-1] if blocks else dockerfile


def classify(base_image: str, stage_body: str) -> tuple[str, str]:
    base_lower = base_image.lower()
    body_lower = stage_body.lower()

    if "distroless" in base_lower:
        return "already-distroless", "No action needed."

    heavy_hits = [m for m in HEAVY_RUNTIME_MARKERS if m in body_lower]
    is_alpine = "alpine" in base_lower
    is_debian_family = any(d in base_lower for d in DEBIAN_FAMILY) or (
        not is_alpine and ("slim" in base_lower or re.match(r"^(node|python|golang):[\d.]+$", base_lower))
    )

    if heavy_hits:
        return (
            "not-compatible",
            f"Needs a full OS at runtime ({', '.join(heavy_hits)}); distroless has no "
            f"package manager to install these. Base image choice isn't the lever here.",
        )

    if is_debian_family:
        return (
            "real-win",
            "Debian/Ubuntu-based with no heavy runtime deps found — a real candidate "
            "for a distroless equivalent (smaller pull, smaller image-scan surface, no shell/pkg-mgr in prod).",
        )

    if is_alpine:
        return (
            "marginal",
            "Already alpine (~5-8MB) with no heavy runtime deps — distroless/static "
            "would only shave a few MB and drop the shell/apk attack surface. No CPU or "
            "runtime-behavior difference for a compiled binary.",
        )

    return "unknown-base", f"Unrecognized base image pattern: {base_image}"


def audit(services: list[dict], workspace: Path) -> list[dict]:
    results = []
    for svc in services:
        if svc.get("kind") != "container":
            continue
        repo_url = svc.get("repo_url") or ""
        repo = repo_name_from_url(repo_url)
        if not repo:
            continue

        dockerfile = read_dockerfile_local(repo, workspace)
        source = "local"
        if dockerfile is None:
            dockerfile = read_dockerfile_gh(repo_url)
            source = "gh-api"
        if dockerfile is None:
            results.append({
                "id": svc["id"], "repo": repo, "verdict": "no-dockerfile",
                "reason": "Dockerfile not found locally or via gh api.",
                "base_image": None, "source": None,
            })
            continue

        base_image = final_stage_from(dockerfile)
        if not base_image:
            results.append({
                "id": svc["id"], "repo": repo, "verdict": "no-from-line",
                "reason": "No FROM line matched.", "base_image": None, "source": source,
            })
            continue

        body = final_stage_body(dockerfile)
        verdict, reason = classify(base_image, body)
        results.append({
            "id": svc["id"], "repo": repo, "verdict": verdict, "reason": reason,
            "base_image": base_image, "source": source,
        })
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit raw JSON instead of the grouped report")
    ap.add_argument("--limit", type=int, default=None, help="only audit the first N container services (for testing)")
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help="local checkout root to prefer over gh api")
    args = ap.parse_args()

    services = json.loads(SERVICES_JSON.read_text())
    services = [s for s in services if s.get("kind") == "container"]
    if args.limit:
        services = services[: args.limit]

    results = audit(services, args.workspace)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    by_verdict: dict[str, list[dict]] = {}
    for r in results:
        by_verdict.setdefault(r["verdict"], []).append(r)

    order = ["real-win", "marginal", "not-compatible", "already-distroless", "no-dockerfile", "no-from-line", "unknown-base"]
    labels = {
        "real-win": "REAL WIN — Debian/Ubuntu base, no heavy runtime deps: worth switching",
        "marginal": "MARGINAL — already alpine, no CPU/runtime win, only attack-surface/size",
        "not-compatible": "NOT COMPATIBLE — needs a full OS at runtime (browser/codec/etc.)",
        "already-distroless": "ALREADY DISTROLESS",
        "no-dockerfile": "NO DOCKERFILE FOUND",
        "no-from-line": "NO FROM LINE PARSED",
        "unknown-base": "UNRECOGNIZED BASE IMAGE",
    }

    print(f"Audited {len(results)} container services\n")
    for key in order:
        group = by_verdict.get(key, [])
        if not group:
            continue
        print(f"## {labels[key]} ({len(group)})")
        by_base: dict[str, list[str]] = {}
        for r in group:
            by_base.setdefault(r["base_image"] or "-", []).append(r["id"])
        for base, ids in sorted(by_base.items(), key=lambda kv: -len(kv[1])):
            sample = ", ".join(sorted(ids)[:8])
            more = f" (+{len(ids) - 8} more)" if len(ids) > 8 else ""
            print(f"  {base}: {len(ids)} services — {sample}{more}")
        if group:
            print(f"  Why: {group[0]['reason']}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
