#!/usr/bin/env python3
"""
Topic-driven registry generator.

Queries the GitHub API for every repo under baditaflorin/* with a
mesh-{0exec,0crawl,pages} topic, then derives a services.json entry per
repo from the topics + repo metadata. Replaces the old three-source merge
in bin/build.py — no more snapshotting hub-app.js or 0crawl-services.json.

Per-service human-curated fields (description, example_path, public demo
token overrides) live in overrides.json, which IS hand-edited. Anything
not overridden falls back to GitHub's description / a sane default
example for the category.

Run:
    python3 bin/generate.py             # writes services.json + summary
    python3 bin/generate.py --dry-run   # prints diff without writing
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES_JSON   = ROOT / "services.json"
OVERRIDES_JSON  = ROOT / "overrides.json"
SUMMARY_TXT     = ROOT / "services.summary.txt"

MESHES = ("0exec", "0crawl", "pages")

# Auth defaults per mesh — overridable per-entry in overrides.json.
AUTH_DEFAULTS = {
    "0exec":  {"type": "api_key",    "query_param": "api_key", "header": "X-API-Key"},
    "0crawl": {"type": "path_token", "path_template": "/t/{token}", "public_demo_token": "default_token"},
    "pages":  {"type": "none"},
}

# Default example query strings by category. Anything more specific should
# go in overrides.json.
DEFAULT_EXAMPLES = {
    "proxy":          "/?url=https://example.com",
    "content":        "/?url=https://example.com",
    "nlp":            "/?url=https://example.com",
    "ocr":            "/?url=https://www.africau.edu/images/default/sample.pdf",
    "search":         "/?query=anthropic+claude",
    "geo":            "/?text=Bucharest",
    "domains":        "/?target=example.com",
    "web_analysis":   "/?url=https://example.com",
    "recon":          "/?target=example.com",
    "security":       "/?target=example.com",
    "infrastructure": "/?target=example.com",
    "visualization":  "/",
    "uncategorized":  "/",
}

_SECRET_PATTERNS = [
    re.compile(r"fb_[a-z0-9]{20,}"),     # 0exec api_key shape
    re.compile(r"\b[a-f0-9]{60,}\b"),    # raw hex master-key shape
]


# ─── GitHub queries ─────────────────────────────────────────────────────

def gh_repos_with_topic(topic: str) -> list[dict]:
    """List baditaflorin/* repos that have a given topic."""
    out = subprocess.run(
        ["gh", "repo", "list", "baditaflorin",
         "--topic", topic, "--limit", "500",
         "--json", "name,description,homepageUrl,url,repositoryTopics,visibility"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def normalize_topics(raw_topics: list[dict]) -> list[str]:
    """gh returns repositoryTopics as [{"name": "..."}]."""
    return [t["name"] for t in raw_topics]


# ─── Topic → field extraction ──────────────────────────────────────────

def mesh_of(topics: list[str]) -> str | None:
    for t in topics:
        if t.startswith("mesh-"):
            v = t[len("mesh-"):]
            if v in MESHES:
                return v
    return None


def category_of(topics: list[str]) -> str:
    """GitHub topics force hyphens, but consumer code (hub icons, 0crawl
    dashboard JSON) uses snake_case. Normalize back. Single-word categories
    are unaffected; only multi-word ones like web-analysis → web_analysis."""
    for t in topics:
        if t.startswith("category-"):
            return t[len("category-"):].replace("-", "_")
    return "uncategorized"


def tags_of(topics: list[str]) -> list[str]:
    return sorted({t for t in topics
                   if not t.startswith(("mesh-", "category-"))
                   and t != "microservice"})


# ─── Per-repo → registry entry ─────────────────────────────────────────

def slug_from_repo_name(name: str, mesh: str) -> str:
    """0crawl repos are named `go_xxxx` on GitHub but the service runs at
    `xxxx.0crawl.com`, so we strip the `go-` prefix for that mesh only. The
    0exec mesh keeps the prefix (`go-js-proxy.0exec.com` matches the repo)."""
    s = name.replace("_", "-").lower()
    if mesh == "0crawl":
        s = s.removeprefix("go-")
    return s


def humanize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def service_url(slug: str, mesh: str, repo: dict) -> str:
    if mesh == "0exec":  return f"https://{slug}.0exec.com"
    if mesh == "0crawl": return f"https://{slug}.0crawl.com"
    if mesh == "pages":
        # Prefer repo homepage if set, else github.io fallback.
        return repo.get("homepageUrl") or f"https://baditaflorin.github.io/{repo['name']}/"
    raise ValueError(f"unknown mesh {mesh}")


def health_url(base: str, mesh: str) -> str:
    if mesh == "pages":
        return base  # static sites have no /health
    return f"{base}/health" if mesh == "0crawl" else f"{base}/_gw_health"


def make_entry(repo: dict, overrides: dict) -> dict | None:
    topics = normalize_topics(repo.get("repositoryTopics") or [])
    mesh = mesh_of(topics)
    if mesh is None:
        return None
    slug = slug_from_repo_name(repo["name"], mesh)
    ov = overrides.get(slug, {})

    cat   = ov.get("category") or category_of(topics)
    base  = service_url(slug, mesh, repo)
    auth  = ov.get("auth") or AUTH_DEFAULTS[mesh]
    desc  = ov.get("description") or (repo.get("description") or "").strip()
    name  = ov.get("name") or humanize(slug)
    tags  = ov.get("tags") or tags_of(topics)
    exp   = ov.get("example_path",
                   DEFAULT_EXAMPLES.get(cat, DEFAULT_EXAMPLES["uncategorized"]))

    return {
        "id":           slug,
        "name":         name,
        "description":  desc,
        "category":     cat,
        "mesh":         mesh,
        "tags":         sorted(set(tags)),
        "url":          base,
        "health_url":   health_url(base, mesh),
        "repo_url":     repo["url"],
        "example_path": exp,
        "auth":         dict(auth),
    }


# ─── Build + safety checks ─────────────────────────────────────────────

def assert_no_secrets(entries: list[dict]) -> None:
    blob = json.dumps(entries)
    for pat in _SECRET_PATTERNS:
        m = pat.search(blob)
        if m:
            sys.exit(f"ERROR: secret-shaped value in output: {m.group(0)[:8]}…  "
                     f"check overrides.json and any topic value.")


def load_overrides() -> dict:
    if not OVERRIDES_JSON.exists():
        return {}
    data = json.loads(OVERRIDES_JSON.read_text())
    if not isinstance(data, dict):
        sys.exit(f"ERROR: {OVERRIDES_JSON} must be a JSON object (slug → patch)")
    return data


def build(overrides: dict) -> list[dict]:
    seen: dict[str, dict] = {}
    for mesh in MESHES:
        for repo in gh_repos_with_topic(f"mesh-{mesh}"):
            entry = make_entry(repo, overrides)
            if entry is None:
                continue
            if entry["id"] in seen:
                print(f"WARN: duplicate slug {entry['id']} (kept first)", file=sys.stderr)
                continue
            seen[entry["id"]] = entry
    return sorted(seen.values(), key=lambda e: (e["mesh"], e["id"]))


def write_summary(entries: list[dict]) -> str:
    from collections import Counter
    by_mesh = Counter(e["mesh"] for e in entries)
    by_cat  = Counter(e["category"] for e in entries)
    lines = ["# Registry summary", f"total: {len(entries)}", "", "## by mesh"]
    lines += [f"  {n:3d}  {m}" for m, n in by_mesh.most_common()]
    lines += ["", "## by category"]
    lines += [f"  {n:3d}  {c}" for c, n in by_cat.most_common()]
    txt = "\n".join(lines) + "\n"
    SUMMARY_TXT.write_text(txt)
    return txt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print diff, don't write")
    args = ap.parse_args()

    overrides = load_overrides()
    entries = build(overrides)
    assert_no_secrets(entries)

    new_blob = json.dumps(entries, indent=2) + "\n"
    if args.dry_run:
        old = SERVICES_JSON.read_text() if SERVICES_JSON.exists() else ""
        if new_blob == old:
            print("no changes")
            return 0
        # Compact diff: counts only
        old_entries = json.loads(old) if old else []
        added   = {e["id"] for e in entries}     - {e["id"] for e in old_entries}
        removed = {e["id"] for e in old_entries} - {e["id"] for e in entries}
        print(f"+{len(added)} new entries: {sorted(added)[:10]}{'…' if len(added) > 10 else ''}")
        print(f"-{len(removed)} removed entries: {sorted(removed)[:10]}{'…' if len(removed) > 10 else ''}")
        return 0

    SERVICES_JSON.write_text(new_blob)
    print(write_summary(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
