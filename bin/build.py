#!/usr/bin/env python3
"""
Merge three upstream sources into one normalized services.json.

Inputs (in sources/, refreshed by bin/sync.sh):
  - 0crawl-services.json   live dashboard config; 110+ entries
  - 0exec-catalog-main.go  slug list + examples map from go-catalog-service
  - hub-app.js             hardcoded DIRECTORY array from hub_scrapetheworld_org

Output:
  - services.json          single registry, sorted by mesh then id
  - services.summary.txt   counts by mesh and category

Open-source rule: never copy a real API key into the registry. The hub
DIRECTORY ships `apiKey: 'fb_…'` per entry; those are stripped here. The
0crawl `default_token` path segment IS retained (it's an intentionally
public demo token).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources"

# Category mapping for 0exec slugs (the catalog's slug list has no category field).
_OEXEC_CATEGORY = {
    "proxy":   {"c-proxy", "go-proxy", "node-proxy", "python-proxy",
                "go-js-proxy", "node-js-proxy", "random-proxy"},
    "search":  {"go-search-duck", "node-search-duck", "node-search-bing"},
    "ocr":     {"ocr-pdf", "ocr-pdf-express"},
    "geo":     {"geo-geocode", "db-geo-geocode", "geo-places"},
    "nlp":     {"nlp-extractinfo"},
    "content": {"utils-readcontent", "linkedin-attributes"},
    "visualization": {"d3-graph"},
}
_SLUG_TO_CATEGORY = {slug: cat for cat, slugs in _OEXEC_CATEGORY.items() for slug in slugs}

# Auth conventions per mesh.
_AUTH_0EXEC = {
    "type": "api_key",
    "query_param": "api_key",
    "header": "X-API-Key",
}
_AUTH_0CRAWL = {
    "type": "path_token",
    "path_template": "/t/{token}",
    "public_demo_token": "default_token",
}


def _strip_default_token(example_url: str) -> str:
    """Remove '/t/default_token' from a 0crawl example URL and return just the
    path+query, so the registry stays auth-agnostic."""
    if not example_url:
        return ""
    u = urlparse(example_url)
    path = re.sub(r"^/t/[^/]+", "", u.path) or "/"
    return path + (("?" + u.query) if u.query else "")


def _0exec_repo(slug: str) -> str:
    return f"https://github.com/baditaflorin/{slug}"


def load_0crawl() -> list[dict]:
    data = json.loads((SOURCES / "0crawl-services.json").read_text())
    out = []
    for s in data:
        u = urlparse(s["example_url"]) if s.get("example_url") else None
        base = f"{u.scheme}://{u.netloc}" if u else None
        if not base:
            # Fall back to deriving from health_url
            hu = urlparse(s.get("health_url", ""))
            if not hu.netloc:
                continue
            base = f"{hu.scheme}://{hu.netloc}"
        out.append({
            "id":           s.get("id", "").lstrip("_").replace("_", "-").removeprefix("go-") or s.get("id"),
            "name":         s.get("display_name") or s.get("name") or s["id"],
            "description":  s.get("description", ""),
            "category":     s.get("category") or "uncategorized",
            "mesh":         "0crawl",
            "tags":         sorted(set(s.get("tags", []))),
            "url":          base,
            "health_url":   s.get("health_url") or (base + "/health"),
            "repo_url":     s.get("repo_url") or "",
            "example_path": _strip_default_token(s.get("example_url", "")),
            "auth":         dict(_AUTH_0CRAWL),
            "port":         s.get("port"),
        })
    return out


# Examples for 0exec services, mirroring the hand-tuned set we built into
# go-catalog-service. Kept here so the registry is the single source.
_0EXEC_EXAMPLES = {
    "c-proxy":             "/?url=https://example.com",
    "go-proxy":            "/?url=https://example.com",
    "node-proxy":          "/?url=https://example.com",
    "python-proxy":        "/?url=https://example.com",
    "go-js-proxy":         "/?url=https://example.com",
    "node-js-proxy":       "/?url=https://example.com",
    "random-proxy":        "/?url=https://example.com",
    "go-search-duck":      "/?query=anthropic+claude",
    "node-search-duck":    "/?query=anthropic+claude",
    "node-search-bing":    "/?query=anthropic+claude",
    "utils-readcontent":   "/?url=https://example.com",
    "nlp-extractinfo":     "/?url=https://example.com",
    "linkedin-attributes": "/?url=https://www.linkedin.com/in/baditaflorin",
    "ocr-pdf":             "/?url=https://www.africau.edu/images/default/sample.pdf",
    "ocr-pdf-express":     "/?url=https://www.africau.edu/images/default/sample.pdf",
    "geo-geocode":         "/?text=Bucharest",
    "db-geo-geocode":      "/?text=Bucharest",
    "geo-places":          "/?query=coffee+Bucharest",
    "d3-graph":            "/",
}


def load_0exec_from_catalog() -> list[dict]:
    src = (SOURCES / "0exec-catalog-main.go").read_text()
    # Grab the `Slugs = []string{ ... }` block.
    m = re.search(r"Slugs\s*=\s*\[\]string\{([^}]+)\}", src, re.DOTALL)
    if not m:
        sys.exit("ERROR: could not locate Slugs slice in 0exec-catalog-main.go")
    slugs = re.findall(r'"([a-z0-9-]+)"', m.group(1))
    out = []
    for slug in slugs:
        domain = f"{slug}.0exec.com"
        out.append({
            "id":           slug,
            "name":         _humanize(slug),
            "description":  "",
            "category":     _SLUG_TO_CATEGORY.get(slug, "uncategorized"),
            "mesh":         "0exec",
            "tags":         _0exec_tags(slug),
            "url":          f"https://{domain}",
            "health_url":   f"https://{domain}/_gw_health",
            "repo_url":     _0exec_repo(slug),
            "example_path": _0EXEC_EXAMPLES.get(slug, ""),
            "auth":         dict(_AUTH_0EXEC),
        })
    return out


def load_hub_overrides() -> dict[str, dict[str, str]]:
    """Pull display name + description for 0exec services from the hub's
    hardcoded DIRECTORY array, keyed by slug derived from the URL."""
    src = (SOURCES / "hub-app.js").read_text()
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(
        r"name:\s*'([^']+)'.*?url:\s*'https://([a-z0-9-]+)\.0exec\.com'.*?desc:\s*'([^']*)'",
        src,
        re.DOTALL,
    ):
        name, slug, desc = m.group(1), m.group(2), m.group(3)
        out[slug] = {"name": name, "description": desc}
    return out


def _humanize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def _0exec_tags(slug: str) -> list[str]:
    tags = []
    if slug.startswith(("go-", "go_")) or slug == "d3-graph":
        tags.append("go")
    if slug.startswith("node-") or "node" in slug:
        tags.append("node")
    if slug.startswith("python-"):
        tags.append("python")
    if slug.startswith("c-"):
        tags.append("c")
    cat = _SLUG_TO_CATEGORY.get(slug)
    if cat:
        tags.append(cat)
    return sorted(set(tags))


def merge() -> list[dict]:
    entries = load_0crawl() + load_0exec_from_catalog()
    overrides = load_hub_overrides()
    for e in entries:
        if e["mesh"] != "0exec":
            continue
        ov = overrides.get(e["id"])
        if not ov:
            continue
        e["name"] = ov["name"]
        if not e["description"]:
            e["description"] = ov["description"]
    # Dedup on id (last wins). 0crawl loaded first; 0exec overrides because
    # the same slug across meshes is extremely unlikely but if it happens
    # the maintained 0exec entry should win.
    by_id: dict[str, dict] = {}
    for e in entries:
        if e["id"] in by_id:
            print(f"WARN: duplicate id {e['id']} (mesh {by_id[e['id']]['mesh']} → {e['mesh']})", file=sys.stderr)
        by_id[e["id"]] = e
    out = sorted(by_id.values(), key=lambda e: (e["mesh"], e["id"]))
    return out


_SECRET_PATTERNS = [
    re.compile(r"fb_[a-z0-9]{20,}"),    # 0exec api_key shape
    re.compile(r"\b[a-f0-9]{60,}\b"),   # raw hex master-key shape
]


def assert_no_secrets(services: list[dict]) -> None:
    """Block the build if a real-looking secret ends up in the output. The
    registry is published; secrets here would leak."""
    blob = json.dumps(services)
    for pat in _SECRET_PATTERNS:
        m = pat.search(blob)
        if m:
            sys.exit(f"ERROR: secret-shaped value in services.json: {m.group(0)[:8]}…  "
                     f"refusing to write. Remove the upstream leak and re-run.")


def main() -> int:
    services = merge()
    assert_no_secrets(services)
    (ROOT / "services.json").write_text(json.dumps(services, indent=2) + "\n")

    by_mesh: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    for s in services:
        by_mesh[s["mesh"]] = by_mesh.get(s["mesh"], 0) + 1
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
    summary = ["# Registry summary", f"total: {len(services)}", "", "## by mesh"]
    summary += [f"  {n:3d}  {m}" for m, n in sorted(by_mesh.items(), key=lambda x: -x[1])]
    summary += ["", "## by category"]
    summary += [f"  {n:3d}  {c}" for c, n in sorted(by_cat.items(), key=lambda x: -x[1])]
    (ROOT / "services.summary.txt").write_text("\n".join(summary) + "\n")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
