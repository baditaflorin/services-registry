#!/usr/bin/env python3
"""extract-service-facts: read every service repo's own service.yaml and write the inputs each service expects.

Why. The registry's `example_path` (overrides.json) is hand-curated and for most services it is only the category
default, so the hub's "Open" / "Run example" link answers a 400 ("missing required query parameter 'q'"). The
service's own service.yaml is the tested truth: `test.url` is a request the repo's own check sends and
`api.params` lists every input with `required`, `type` and `description`. This script turns the clones into one
deterministic facts file; bin/sync-service-params.py turns the facts file into registry data.

Read-only: it opens `<workspace>/*/service.yaml` and the registry and writes nothing but --out. Stdlib + PyYAML only.

Matching. A clone belongs to the registry entry whose `repo_url` basename equals the clone's directory name once both
are lowercased and `-`/`_` are removed (many repositories are cloned twice, as go-x and go_x). When both spellings
exist the one spelled exactly like the repo_url basename wins, else the first by name; the choice is the same on
every run. A repository that several registry entries share (an `$expand` family such as the fleet metrics hub) is
left out: its service.yaml describes one of those entries and the file does not say which. A service.yaml
that does not parse, or is not a mapping, is left out too. The ids and reasons that were left out go to stderr.

    python3 bin/extract-service-facts.py --workspace /root/workspace --out /tmp/service-facts.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 1


# -- pure helpers (unit-tested) ------------------------------------------------------------------------------------

def norm(name):
    """Directory / repository name with case and the -/_ spelling removed."""
    return re.sub(r"[-_]", "", (name or "").lower())


def repo_basename(repo_url):
    base = (repo_url or "").rstrip("/").split("/")[-1]
    return base[:-4] if base.endswith(".git") else base


def plain(value):
    """A YAML value as plain JSON data (dates and other YAML scalars become strings)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    return str(value)


def clean_text(value):
    """A one-line description: runs of whitespace collapsed, None for anything that is not text."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def parse_params(raw):
    """The `api.params` list as [{name, required, type, description, example, default}], in declared order.

    A param without a string name is dropped; `required` is true only for a real boolean true."""
    out = []
    for p in raw or []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            continue
        out.append({
            "name": p["name"].strip(),
            "required": p.get("required") is True,
            "type": p.get("type") if isinstance(p.get("type"), str) else None,
            "description": clean_text(p.get("description")),
            "example": plain(p.get("example")),
            "default": plain(p.get("default")),
        })
    return out


def parse_service_yaml(doc):
    """Facts of one parsed service.yaml, or None when it is not a mapping."""
    if not isinstance(doc, dict):
        return None
    test = doc.get("test") if isinstance(doc.get("test"), dict) else {}
    api = doc.get("api") if isinstance(doc.get("api"), dict) else {}
    url = test.get("url") or test.get("example_url") or ""
    return {
        "test_url": url.strip() if isinstance(url, str) else "",
        "api": {
            "endpoint": api.get("endpoint") if isinstance(api.get("endpoint"), str) else None,
            "method": api.get("method").upper() if isinstance(api.get("method"), str) else None,
        },
        "params": parse_params(api.get("params")),
    }


def registry_index(services):
    """({normalised repo basename: [registry ids]}, {id: repo basename}) over every entry that has a repo_url."""
    index, base_of = {}, {}
    for s in services:
        base = repo_basename(s.get("repo_url"))
        if base and s.get("id"):
            index.setdefault(norm(base), []).append(s["id"])
            base_of[s["id"]] = base
    return index, base_of


def pick_clone(dirs, exact):
    """The one directory to read among the clones of a repository: the exact spelling, else the first by name."""
    return exact if exact in dirs else sorted(dirs)[0]


def build_facts(clones, services):
    """clones: {dir name: parsed doc or None}. Returns (facts rows sorted by id, left-out [(what, reason)])."""
    index, base_of = registry_index(services)
    groups = {}
    for d in clones:
        groups.setdefault(norm(d), []).append(d)
    rows, left_out = [], []
    for key in sorted(groups):
        ids = index.get(key)
        if not ids:
            continue  # a clone of something that is not a registry service (libraries, tools): not our business
        if len(ids) > 1:
            left_out.append((key, f"repository shared by {len(ids)} registry entries ({', '.join(sorted(ids))})"))
            continue
        sid = ids[0]
        d = pick_clone(groups[key], base_of[sid])
        facts = parse_service_yaml(clones[d])
        if facts is None:
            left_out.append((sid, f"{d}/service.yaml does not parse into a mapping"))
            continue
        rows.append({"id": sid, "repo": base_of[sid], **facts})
    rows.sort(key=lambda r: r["id"])
    return rows, left_out


def read_clones(workspace):
    """{dir name: parsed service.yaml (None when unreadable)} for every <workspace>/*/service.yaml."""
    import yaml  # PyYAML is only needed to read files; the pure helpers above work without it (CI has none)

    clones = {}
    for f in sorted(Path(workspace).glob("*/service.yaml")):
        try:
            clones[f.parent.name] = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            clones[f.parent.name] = None
    return clones


def facts_document(rows):
    return {"schema": SCHEMA, "services": rows}


def dumps(doc):
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workspace", default="/root/workspace", help="directory holding the service clones")
    ap.add_argument("--registry", default=str(ROOT / "services.json"), help="services.json to match repositories to ids")
    ap.add_argument("--out", default="-", help="facts file to write (default: stdout)")
    args = ap.parse_args(argv)

    services = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    rows, left_out = build_facts(read_clones(args.workspace), services)
    text = dumps(facts_document(rows))
    if args.out == "-":
        sys.stdout.write(text)
    else:
        Path(args.out).write_text(text, encoding="utf-8")
    required = sum(1 for r in rows if any(p["required"] for p in r["params"]))
    print(f"facts: {len(rows)} services ({required} declare a required param), {len(left_out)} left out", file=sys.stderr)
    for what, why in left_out:
        print(f"  left out: {what}: {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
