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
SLUG_JSON       = ROOT / "slug.json"
RENAMES_JSON    = ROOT / "renames.json"
SUMMARY_TXT     = ROOT / "services.summary.txt"

# Sliced projections of services.json — sibling files emitted next to the
# full registry so AI consumers (and dashboards) can fetch just the fields
# they need instead of paying for the ~280 KB full blob. Each slice is a
# stable URL at raw.githubusercontent.com/baditaflorin/services-registry/main/<file>.
# Slices are derived (never hand-edited); rebuild via `bin/generate.py` or
# `bin/generate.py --slices-only`.
#
# Adding a new slice: add one entry below — value is either a callable
# (entry -> any) for non-dict shapes like the bare-id list, or a list of
# keys for the common "pick these fields, drop entries missing all of them"
# case. Slices are written compact (no indent) — they exist to minimize
# transferred bytes / tokens; the human-readable view is `services.json`.
def _pick(keys: list[str]):
    """Return entries projected to `keys`. Keys absent on an entry are
    omitted (not nulled) so the slice stays small. Entries that have none
    of the keys beyond `id` are dropped — e.g. `services.ports.json`
    excludes kind=static entries that have no host_port at all."""
    non_id = [k for k in keys if k != "id"]
    def f(e: dict) -> dict | None:
        if non_id and not any(k in e for k in non_id):
            return None
        return {k: e[k] for k in keys if k in e}
    return f

PROJECTIONS = {
    # Bare slug list — smallest possible "what services exist?" answer.
    "services.ids.json":     lambda e: e["id"],
    # Picker / menu rendering.
    "services.names.json":   _pick(["id", "name"]),
    # Catalog overview — enough to render a row without auth/port detail.
    "services.minimal.json": _pick(["id", "name", "mesh", "kind", "category",
                                    "language", "trl", "url"]),
    # "Build an Open link" — URLs + auth hint, no TRL / deploy fields.
    "services.urls.json":    _pick(["id", "url", "health_url", "example_path",
                                    "auth_help"]),
    # TRL audits — claude-haiku-trl-batch and friends only need these.
    "services.trl.json":     _pick(["id", "trl", "trl_ceiling",
                                    "trl_assessed_at", "trl_assessor"]),
    # Port allocation — kind=static entries fall out (no host_port).
    "services.ports.json":   _pick(["id", "host_port", "container_port"]),
    # fleet-runner deploy targeting.
    "services.deploy.json":  _pick(["id", "mesh", "kind", "runtime",
                                    "language", "repo_url"]),
    # Declared dependency edges — tiny slice consumed by go-fleet-visualizer
    # and fleet-runner audit-graph. Entries without depends_on are dropped
    # by _pick (since "depends_on" is the only non-id key requested).
    "services.depends.json": _pick(["id", "depends_on"]),
}

MESHES = ("0exec", "0crawl", "pages")

# kind = what kind of deployable this is (orthogonal to mesh).
#   container = runs as a Docker service (port, /health, Dockerfile, workspace).
#   static    = static GitHub Pages site (no port, no container, no workspace).
# fleet-runner gates kind-specific operations on this field — kind=static is
# skipped by health/smoke/deploy/clone-missing/audit-port/bump-version.
KIND_BY_MESH = {
    "0exec":  "container",
    "0crawl": "container",
    "pages":  "static",
}

# Auth defaults per mesh — overridable per-entry in overrides.json.
# 0crawl accepts BOTH a path-token (legacy callers) AND an api_key (new
# universal shape, keystore-gated). The nginx vhost decides which one
# the caller used; both flow through the same auth_request to the
# keystore. The path_template stays advertised so existing /t/<token>/...
# callers keep working unchanged.
AUTH_DEFAULTS = {
    "0exec":  {"type": "api_key",    "query_param": "api_key", "header": "X-API-Key"},
    "0crawl": {
        "type": "api_key",
        "query_param": "api_key",
        "header": "X-API-Key",
        "path_template": "/t/{token}",
        "public_demo_token": "default_token",
    },
    "pages":  {"type": "none"},
}

# Language defaults per mesh when no explicit lang-<x> topic is present.
# Container services in baditaflorin/* are overwhelmingly Go; pages are HTML.
LANG_DEFAULTS = {
    "0exec":  "go",
    "0crawl": "go",
    "pages":  "html",
}

# Runtime defaults — how the service is started/managed. Orthogonal to
# language: a Go service may run under compose, systemd, or as a static
# binary; a Python service under compose or systemd; etc. Default for
# kind=container is compose (every service is docker-compose today);
# default for kind=static is github-pages.
RUNTIME_DEFAULTS_BY_KIND = {
    "container": "compose",
    "static":    "github-pages",
}

RUNTIME_VALUES = {"compose", "systemd", "binary", "k8s", "github-pages", "external"}

# Known language values (must match schema enum).
LANG_VALUES = {"go", "node", "python", "rust", "c", "html", "wasm", "other"}

# Topic shortcuts that imply a language when no explicit lang-<x> topic exists.
# Mirrors the historical tag soup ("node", "c") used before lang-* topics
# were introduced.
LANG_FROM_TAG = {
    "node":   "node",
    "nodejs": "node",
    "python": "python",
    "rust":   "rust",
    "c":      "c",
    "wasm":   "wasm",
    "go":     "go",
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
                   if not t.startswith(("mesh-", "category-", "lang-"))
                   and t != "microservice"})


def language_of(topics: list[str], mesh: str) -> str:
    """Derive primary language for the service.

    Precedence:
      1. Explicit `lang-<x>` topic (e.g. `lang-go`, `lang-node`).
      2. Tag-soup fallback for legacy repos that signal language via a
         category-style topic ("node", "c", etc.).
      3. Mesh default (container meshes → "go"; pages → "html").

    fleet-runner uses this for filters like `--language=go` so a Go
    dep-bump doesn't touch a Node or Python service. UIs use it to badge
    the catalog row.
    """
    for t in topics:
        if t.startswith("lang-"):
            v = t[len("lang-"):]
            if v in LANG_VALUES:
                return v
    for t in topics:
        if t in LANG_FROM_TAG:
            return LANG_FROM_TAG[t]
    return LANG_DEFAULTS[mesh]


# ─── Per-repo → registry entry ─────────────────────────────────────────

# Per-repo slug overrides live in slug.json (single source of truth, shared
# with bin/backfill-host-ports.py). Loaded lazily so a missing or malformed
# file gives a clear error instead of an import-time crash.
def load_slug_overrides() -> dict[str, str]:
    if not SLUG_JSON.exists():
        sys.exit(f"ERROR: {SLUG_JSON} not found (single source of truth for slug map)")
    data = json.loads(SLUG_JSON.read_text())
    if not isinstance(data, dict) or "overrides" not in data:
        sys.exit(f"ERROR: {SLUG_JSON} must be a JSON object with an 'overrides' key")
    ov = data["overrides"]
    if not isinstance(ov, dict):
        sys.exit(f"ERROR: {SLUG_JSON} 'overrides' must be a JSON object")
    return ov


SLUG_OVERRIDES = load_slug_overrides()


def load_renames() -> dict[str, dict]:
    """Load renames.json and return {to_id: rename_record}. Missing file is
    not an error — most fleets don't have renames pending. Schema is
    services-registry/schema/renames.v1.json.

    The returned map is keyed by `to_id` (the new slug) so make_entry can
    look up "what alias should this entry advertise?" in O(1)."""
    if not RENAMES_JSON.exists():
        return {}
    data = json.loads(RENAMES_JSON.read_text())
    if not isinstance(data, dict) or "renames" not in data:
        sys.exit(f"ERROR: {RENAMES_JSON} must be a JSON object with a 'renames' key")
    out: dict[str, dict] = {}
    for r in data.get("renames", []):
        to_id = r.get("to_id")
        if not to_id:
            continue
        # Multiple renames pointing at the same to_id (rare; chained renames
        # collapse here): accumulate aliases.
        if to_id in out:
            out[to_id]["_aliases"].append(r["from_id"])
            out[to_id]["_alias_urls"].append(r["from_url"])
        else:
            out[to_id] = {
                **r,
                "_aliases":    [r["from_id"]],
                "_alias_urls": [r["from_url"]],
            }
    return out


RENAMES = load_renames()


def auth_help_for(auth: dict) -> str:
    """Canonical short label for what auth a caller needs. UIs render this
    verbatim instead of re-implementing the if/else (which historically
    drifts and produces "No auth" for services that actually require auth).

    On 0crawl after the keystore migration, the auth object carries BOTH
    api_key fields and the legacy `path_template` — the label advertises
    api_key as primary, with the path-token shape mentioned as also-supported.
    """
    t = auth.get("type")
    if t == "api_key":
        qp = auth.get("query_param") or "api_key"
        hdr = auth.get("header") or "X-API-Key"
        base = f"api_key required (header {hdr} or ?{qp}=)"
        tmpl = auth.get("path_template")
        if tmpl:
            demo = auth.get("public_demo_token")
            extra = f"; legacy path token {tmpl}" + (f" (demo: {demo})" if demo else "")
            return base + extra
        return base
    if t == "path_token":
        demo = auth.get("public_demo_token")
        tmpl = auth.get("path_template") or "/t/{token}/"
        if demo:
            return f"path token {tmpl} — public demo: {demo}"
        return f"path token {tmpl} required"
    if t == "none":
        return "no auth"
    return "auth: unknown"


def slug_from_repo_name(name: str, mesh: str) -> str:
    """0crawl repos are named `go_xxxx` on GitHub but the service runs at
    `xxxx.0crawl.com`, so we strip the `go-` prefix for that mesh only. The
    0exec mesh keeps the prefix (`go-js-proxy.0exec.com` matches the repo).
    Per-repo overrides above win over the auto-derivation."""
    if name in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[name]
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


def make_entry(repo: dict, by_slug: dict, rules: list[dict]) -> dict | None:
    topics = normalize_topics(repo.get("repositoryTopics") or [])
    mesh = mesh_of(topics)
    if mesh is None:
        return None
    slug = slug_from_repo_name(repo["name"], mesh)

    # Resolve overrides in two phases so rules can match on mesh/kind/
    # language/runtime (derived from topics) before per-slug patches
    # apply. Phase 1: build a probe entry with just the derivable axes
    # so rules can match on them. Phase 2: collect rule patches + the
    # per-slug patch into a single `ov` dict the rest of the function
    # consumes verbatim. Per-slug wins over rules.
    kind = KIND_BY_MESH[mesh]
    probe = {
        "id":       slug,
        "mesh":     mesh,
        "kind":     kind,
        "language": language_of(topics, mesh),
        "runtime":  RUNTIME_DEFAULTS_BY_KIND[kind],
        "category": category_of(topics),
    }
    ov, _ = resolved_overrides_for(probe, by_slug, rules)

    cat   = ov.get("category") or category_of(topics)
    base  = service_url(slug, mesh, repo)
    auth  = ov.get("auth") or AUTH_DEFAULTS[mesh]
    desc  = ov.get("description") or (repo.get("description") or "").strip()
    name  = ov.get("name") or humanize(slug)
    tags  = ov.get("tags") or tags_of(topics)
    exp   = ov.get("example_path",
                   DEFAULT_EXAMPLES.get(cat, DEFAULT_EXAMPLES["uncategorized"]))

    lang = ov.get("language") or language_of(topics, mesh)
    runtime = ov.get("runtime") or RUNTIME_DEFAULTS_BY_KIND[kind]

    entry = {
        "id":           slug,
        "name":         name,
        "description":  desc,
        "category":     cat,
        "mesh":         mesh,
        # `kind` is the deployment shape (container vs static). Orthogonal
        # to mesh and to auth — gate kind-specific tooling on this field,
        # never on mesh. Adding a new "kind" (serverless, cdn-only, …) is
        # how the fleet absorbs new deployable shapes without rewriting
        # every audit.
        "kind":         kind,
        # `language` drives bulk-operation filters. A Go-only dep bump
        # narrows to language=go; a Node lockfile audit narrows to
        # language=node. UIs render it as a small badge.
        "language":     lang,
        # `runtime` is the deploy-dispatch axis. A `compose` runtime
        # rolls forward via `docker compose pull && up -d`; a `systemd`
        # runtime via `systemctl restart`; a `github-pages` runtime
        # via a Pages CI trigger. Today every container service is
        # compose, but the field is load-bearing for future shapes
        # without re-classifying mesh or language.
        "runtime":      runtime,
        "tags":         sorted(set(tags)),
        "url":          base,
        "health_url":   health_url(base, mesh),
        "repo_url":     repo["url"],
        "example_path": exp,
        "auth":         dict(auth),
        # Single source of truth for the "what auth does this need" label
        # rendered by every UI (catalog, hub, dashboard). UIs that derive
        # their own label drift (see hub showing "No auth" for api_key
        # services on 2026-05-13). Format: short imperative.
        "auth_help":    auth_help_for(auth),
    }

    # kind=static carries a `pages_url` for the catalog UI; container
    # entries must NOT carry this field (schema enforces).
    if kind == "static":
        entry["pages_url"] = base
        if ov.get("pages_source_branch"):
            entry["pages_source_branch"] = ov["pages_source_branch"]

    # Rename log — if this entry is the to_id of one or more renames, emit
    # `aliases` (old slugs) and `alias_urls` (old hostnames) so by-id lookups
    # in older tooling still resolve. fleet-runner nginx-render reads these
    # to emit 301-redirect vhosts from each alias_url -> url. The canonical
    # log lives in renames.json (schema: schema/renames.v1.json).
    if entry["id"] in RENAMES:
        r = RENAMES[entry["id"]]
        entry["aliases"]    = sorted(set(r["_aliases"]))
        entry["alias_urls"] = sorted(set(r["_alias_urls"]))
        if r.get("status"):
            entry["rename_status"] = r["status"]
        if r.get("retire_at"):
            entry["rename_retire_at"] = r["retire_at"]

    for k in ("trl", "trl_evidence", "trl_ceiling", "trl_ceiling_reason",
              "trl_assessed_at", "trl_assessor",
              "host_port", "container_port", "port",
              # Declared service-to-service dependency edges. Static
              # counterpart to the live graph in go-fleet-graph. Set
              # via overrides.json; fleet-runner audit-graph diffs
              # declared vs observed.
              "depends_on",
              # Render-time vhost knobs (consumed by fleet-runner
              # nginx-render). Per-service patches via overrides.json
              # or via $rules; not derived from any other source.
              "static_fallback_key", "cert_domain", "rewrite_token_path",
              "vhost"):
        if k in ov:
            entry[k] = ov[k]

    return entry


# ─── Build + safety checks ─────────────────────────────────────────────

def assert_no_secrets(entries: list[dict]) -> None:
    blob = json.dumps(entries)
    for pat in _SECRET_PATTERNS:
        m = pat.search(blob)
        if m:
            sys.exit(f"ERROR: secret-shaped value in output: {m.group(0)[:8]}…  "
                     f"check overrides.json and any topic value.")


def load_overrides() -> dict:
    """Load overrides.json.

    Backward-compat shape: top-level keys are slug names; values are
    patch dicts merged into the rendered registry entry for that slug.

    Extended (2026-05-14): keys starting with `$` are reserved metadata,
    NOT slugs. The only metadata key today is `$rules`, a list of
    bulk-override rules of the form:

        {
          "name": "phone-extractor-san-cert",
          "match": {                         # any-of within a field, all-of across fields
            "ids":      ["a11y-quick", …],   # explicit slug list, OR
            "mesh":     "0crawl",            # mesh filter, OR
            "category": "domains",           # category filter, OR
            "language": "go",                # language filter, OR
            "kind":     "container"          # kind filter
          },
          "patch": { "cert_domain": "phone-extractor.0crawl.com" },
          "why":   "46 vhosts share phone-extractor's SAN cert; SAN list is the actual covered set"
        }

    Rules apply in declaration order; the per-slug entry (if any) wins
    over rules. Use `fleet-runner overrides list / explain / audit` to
    see the resolved patch per service. This shape lets you encode "46
    services have the same cert_domain" as one rule instead of 46
    near-identical per-slug entries.
    """
    if not OVERRIDES_JSON.exists():
        return {}
    data = json.loads(OVERRIDES_JSON.read_text())
    if not isinstance(data, dict):
        sys.exit(f"ERROR: {OVERRIDES_JSON} must be a JSON object (slug → patch)")
    return data


def split_overrides(raw: dict) -> tuple[dict, list[dict]]:
    """Separate per-slug patches from `$rules` metadata."""
    rules = raw.get("$rules") or []
    if not isinstance(rules, list):
        sys.exit(f"ERROR: $rules in {OVERRIDES_JSON} must be an array of rule objects")
    by_slug = {k: v for k, v in raw.items() if not k.startswith("$")}
    return by_slug, rules


def rule_matches(rule: dict, entry: dict) -> bool:
    """Return True iff `entry` satisfies all of `rule.match`'s clauses.

    Each clause is "any-of": `ids: [a, b]` matches if entry.id is a OR b.
    Different fields are "all-of": id must match AND mesh must match,
    etc. An empty match clause matches nothing (defensive: a rule with
    no criteria would fan out to every service, which is almost
    certainly a typo)."""
    m = rule.get("match") or {}
    if not m:
        return False
    if "ids" in m and entry["id"] not in m["ids"]:
        return False
    for k in ("mesh", "kind", "language", "runtime", "category"):
        if k in m and entry.get(k) != m[k]:
            return False
    return True


def resolved_overrides_for(entry: dict, by_slug: dict, rules: list[dict]) -> tuple[dict, list[str]]:
    """Return (resolved_patch, applied_rule_names) for one entry.

    Resolution: rules first (in declaration order), per-slug last.
    Patches are shallow-merged — later writes overwrite earlier."""
    out: dict = {}
    applied: list[str] = []
    for r in rules:
        if rule_matches(r, entry):
            out.update(r.get("patch") or {})
            applied.append(r.get("name") or "<unnamed>")
    if entry["id"] in by_slug:
        out.update(by_slug[entry["id"]])
    return out, applied


def build(overrides: dict) -> list[dict]:
    by_slug, rules = split_overrides(overrides)
    seen: dict[str, dict] = {}
    for mesh in MESHES:
        for repo in gh_repos_with_topic(f"mesh-{mesh}"):
            entry = make_entry(repo, by_slug, rules)
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
    by_kind = Counter(e["kind"] for e in entries)
    by_lang = Counter(e["language"] for e in entries)
    by_cat  = Counter(e["category"] for e in entries)
    lines = ["# Registry summary", f"total: {len(entries)}", "", "## by kind"]
    lines += [f"  {n:3d}  {k}" for k, n in by_kind.most_common()]
    lines += ["", "## by mesh"]
    lines += [f"  {n:3d}  {m}" for m, n in by_mesh.most_common()]
    lines += ["", "## by language"]
    lines += [f"  {n:3d}  {l}" for l, n in by_lang.most_common()]
    lines += ["", "## by category"]
    lines += [f"  {n:3d}  {c}" for c, n in by_cat.most_common()]
    txt = "\n".join(lines) + "\n"
    SUMMARY_TXT.write_text(txt)
    return txt


def write_slices(entries: list[dict]) -> list[tuple[str, int, int]]:
    """Emit every projection in PROJECTIONS as a compact JSON file next to
    services.json. Returns (filename, entry_count, byte_size) per slice
    so main() can print a summary. Slices are pure derivatives — never
    hand-edit; rerun `bin/generate.py [--slices-only]` to rebuild."""
    out = []
    for fname, proj in PROJECTIONS.items():
        sliced = [v for v in (proj(e) for e in entries) if v is not None]
        blob = json.dumps(sliced, separators=(",", ":")) + "\n"
        (ROOT / fname).write_text(blob)
        out.append((fname, len(sliced), len(blob)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print diff, don't write")
    ap.add_argument("--slices-only", action="store_true",
                    help="rebuild only the sliced projection files from the "
                         "existing services.json (skips the GitHub fetch)")
    args = ap.parse_args()

    if args.slices_only:
        if not SERVICES_JSON.exists():
            sys.exit(f"ERROR: {SERVICES_JSON} does not exist — run without "
                     f"--slices-only first to build the full registry")
        entries = json.loads(SERVICES_JSON.read_text())
        for fname, n, sz in write_slices(entries):
            print(f"  {fname:28s}  {n:4d} entries  {sz:7d} bytes")
        return 0

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
    print("\n## slices")
    for fname, n, sz in write_slices(entries):
        print(f"  {fname:28s}  {n:4d} entries  {sz:7d} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
