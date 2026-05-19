#!/usr/bin/env python3
"""audit-slug-strip — preview the impact of unifying the slug-derivation
rule across container meshes.

Background: pre-2026-05-19, `bin/generate.py` stripped the leading `go-`
prefix only for `mesh-0crawl`; 0exec services retained the prefix (so
`go-js-proxy.0exec.com` matched the GitHub repo `go-js-proxy`). After
unification, both meshes strip the prefix.

This script answers three questions BEFORE running the rename:

  1. Which 0exec services change slug (i.e. lose the `go-` prefix)?
  2. Do any of the new slugs COLLIDE with an existing slug in the
     registry (the same mesh or 0crawl)?
  3. Are any of the new slugs SEMANTICALLY suspicious (e.g. the `go-`
     prefix was part of a single word like `go-runtime` → `runtime`)?

Run from anywhere; reads services.json from the repo it ships with.
Emits a markdown summary on stdout. Exit code 0 if no collisions and
no suspect renames; 1 if collisions exist; 2 if suspects exist.

Usage:
    bin/audit-slug-strip.py [--json] [--emit-renames OUT.json]

`--emit-renames` writes a renames.json-shaped batch you can append to
the canonical renames.json after operator review. The script never
touches renames.json itself — that's a separate `fleet-runner` step.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys


# Slugs that, after stripping `go-`, would collapse to a name that is
# semantically a single English/Go word. These flag the user-mentioned
# `going-repo` style false positive — the literal `go-` prefix exists,
# but stripping changes meaning. The script flags these as "suspect"
# and refuses to emit them into the rename batch without operator opt-in.
#
# Empty by default; populated by inspection of the current fleet. Add a
# row here if a future repo name needs the carve-out. Use lowercase
# pre-strip name (matches `name.replace('_','-').lower()` form).
SEMANTIC_SUSPECT_PRE_STRIP = {
    # Examples of the shape we'd refuse (none exist in the current
    # fleet at audit time; this is documentation of intent):
    # "go-runtime": "would strip to 'runtime' but Go runtime is one term",
    # "go-fish":    "would strip to 'fish' but go-fish is a card game",
}


def proposed_slug(name: str, slug_overrides: dict[str, str]) -> str:
    """Replicate the unified slug rule from bin/generate.py — including the
    slug.json override layer, which always wins over the auto-derivation.
    A repo whose operator deliberately picked a non-default slug (e.g.
    `go_postmessage_listener_finder` → `postmessage`) keeps that slug;
    the unified strip rule only applies to repos that fall through to
    the auto-derivation path."""
    if name in slug_overrides:
        return slug_overrides[name]
    s = name.replace("_", "-").lower()
    return s.removeprefix("go-")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--emit-renames", metavar="OUT.json",
                    help="write a renames.json batch (safe rows only) for operator review")
    ap.add_argument("--services", default=None,
                    help="path to services.json (default: alongside this script)")
    args = ap.parse_args()

    here = pathlib.Path(__file__).resolve().parent.parent
    services_path = pathlib.Path(args.services) if args.services else (here / "services.json")
    services = json.loads(services_path.read_text())

    slug_json = json.loads((here / "slug.json").read_text())
    slug_overrides = slug_json.get("overrides", {})

    existing_ids = {e["id"] for e in services}
    container = [e for e in services if e.get("kind") == "container"]

    proposed_renames = []   # 0exec/0crawl entries whose slug would change
    unchanged = []          # already in the target shape
    collisions = []         # proposed new slug already taken
    suspects = []           # semantically risky strips

    for entry in container:
        slug = entry["id"]
        repo_url = entry.get("repo_url", "")
        repo_name = repo_url.rstrip("/").split("/")[-1] if repo_url else slug
        # Derive the slug fresh from the repo name, matching the new rule
        # AND the slug.json override layer. Repos that fall through to
        # auto-derivation are the only ones the strip-rule unification
        # actually affects.
        new_slug = proposed_slug(repo_name, slug_overrides)
        if new_slug == slug:
            unchanged.append(slug)
            continue

        pre_strip = repo_name.replace("_", "-").lower()
        if pre_strip in SEMANTIC_SUSPECT_PRE_STRIP:
            suspects.append({
                "id": slug,
                "repo": repo_name,
                "proposed_id": new_slug,
                "reason": SEMANTIC_SUSPECT_PRE_STRIP[pre_strip],
            })
            continue

        if new_slug in existing_ids and new_slug != slug:
            collisions.append({
                "id": slug,
                "repo": repo_name,
                "proposed_id": new_slug,
                "collides_with": new_slug,
            })
            continue

        # renames.json schema wants bare hostnames (format: hostname),
        # not full URLs. Strip the https:// here so the emit step doesn't
        # have to.
        from_url = entry.get("url", "").removeprefix("https://").removeprefix("http://").rstrip("/")
        to_url = from_url.replace(f"{slug}.", f"{new_slug}.", 1)
        proposed_renames.append({
            "id": slug,
            "repo": repo_name,
            "mesh": entry["mesh"],
            "proposed_id": new_slug,
            "from_url": from_url,
            "to_url": to_url,
        })

    if args.json:
        print(json.dumps({
            "unchanged_count": len(unchanged),
            "renames": proposed_renames,
            "collisions": collisions,
            "suspects": suspects,
        }, indent=2))
    else:
        print(f"# Slug-strip audit ({services_path})")
        print()
        print(f"- container services: {len(container)}")
        print(f"- already in target shape: {len(unchanged)}")
        print(f"- proposed renames:    {len(proposed_renames)}")
        print(f"- collisions:          {len(collisions)}")
        print(f"- semantic suspects:   {len(suspects)}")
        print()
        if collisions:
            print("## collisions — refuse to rename without operator decision")
            print("| current id | repo | would collapse to | already taken by |")
            print("|---|---|---|---|")
            for c in collisions:
                print(f"| {c['id']} | {c['repo']} | {c['proposed_id']} | {c['collides_with']} |")
            print()
        if suspects:
            print("## semantic suspects — verify before renaming")
            print("| current id | repo | would strip to | concern |")
            print("|---|---|---|---|")
            for s in suspects:
                print(f"| {s['id']} | {s['repo']} | {s['proposed_id']} | {s['reason']} |")
            print()
        if proposed_renames:
            print(f"## proposed renames ({len(proposed_renames)})")
            print("| mesh | current id | repo | new id | from_url → to_url |")
            print("|---|---|---|---|---|")
            for r in proposed_renames:
                print(f"| {r['mesh']} | {r['id']} | {r['repo']} | {r['proposed_id']} | {r['from_url']} → {r['to_url']} |")

    if args.emit_renames and not collisions and not suspects:
        today = dt.date.today().isoformat()
        retire = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        rows = []
        for r in proposed_renames:
            rows.append({
                "from_id":    r["id"],
                "to_id":      r["proposed_id"],
                "from_url":   r["from_url"],
                "to_url":     r["to_url"],
                "renamed_at": today,
                "retire_at":  retire,
                "renamed_by": "slug-strip-unification",
                "reason":     "Unify slug-derivation: strip leading `go-` for both 0exec and 0crawl.",
                "status":     "redirect",
            })
        # Match the renames.json envelope shape so the operator can merge
        # this directly into the canonical log (after review) — e.g.
        #   jq '.renames += $b.renames' renames.json -s --slurpfile b out.json
        out = {
            "$schema": "schema/renames.v1.json",
            "renames": rows,
        }
        pathlib.Path(args.emit_renames).write_text(json.dumps(out, indent=2) + "\n")
        print(f"\n# wrote {len(rows)} candidate renames.json rows to {args.emit_renames}", file=sys.stderr)

    if collisions:
        return 1
    if suspects:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
