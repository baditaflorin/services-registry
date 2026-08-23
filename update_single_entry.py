#!/usr/bin/env python3
"""Update exactly one existing entry in services.json in place, via raw
text replacement of that entry's JSON block, to avoid reformatting the
rest of the file. Rebuilds all derived slices/summary/public-mirror
from the resulting full entry list."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "bin")
import generate  # noqa: E402

ROOT = Path(".")


def fetch_repo(full_name: str) -> dict:
    out = subprocess.run(
        ["gh", "repo", "view", full_name,
         "--json", "name,description,homepageUrl,url,repositoryTopics,visibility"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: update_single_entry.py <org>/<repo>")
    full_name = sys.argv[1]

    repo = fetch_repo(full_name)
    overrides = generate.load_overrides()
    by_slug, rules, _, _ = generate.split_overrides(overrides)
    new_entry = generate.make_entry(repo, by_slug, rules)
    if new_entry is None:
        sys.exit(f"ERROR: {full_name} has no mesh-* topic")

    services_json = ROOT / "services.json"
    raw = services_json.read_text()
    entries = json.loads(raw)

    idx = next((i for i, e in enumerate(entries) if e["id"] == new_entry["id"]), None)
    if idx is None:
        sys.exit(f"ERROR: id {new_entry['id']!r} not found in services.json — nothing to update")

    old_entry = entries[idx]
    old_block = json.dumps(old_entry, indent=2)
    # Match the file's actual indentation for a top-level array element
    # (2 spaces) — same convention used by add_single_entry.py.
    old_indented = "\n".join(("  " + line if line.strip() else line) for line in old_block.splitlines())
    if raw.count(old_indented) != 1:
        sys.exit(f"ERROR: expected exactly 1 occurrence of the current {new_entry['id']!r} "
                  f"block in services.json, found {raw.count(old_indented)} — refusing to guess")

    new_block = json.dumps(new_entry, indent=2)
    new_indented = "\n".join(("  " + line if line.strip() else line) for line in new_block.splitlines())
    new_raw = raw.replace(old_indented, new_indented, 1)

    entries_for_derived = list(entries)
    entries_for_derived[idx] = new_entry
    generate.assert_no_secrets(entries_for_derived)

    reparsed = json.loads(new_raw)
    if json.dumps(reparsed, sort_keys=True) != json.dumps(entries_for_derived, sort_keys=True):
        sys.exit("ERROR: spliced services.json does not round-trip to the expected entry set — aborting")

    services_json.write_text(new_raw)
    print(generate.write_summary(entries_for_derived))
    print("\n## slices")
    for fname, n, sz in generate.write_slices(entries_for_derived):
        print(f"  {fname:28s}  {n:4d} entries  {sz:7d} bytes")
    n, sz = generate.write_public_mirror(entries_for_derived)
    print(f"  {'services-public.json':28s}  {n:4d} entries  {sz:7d} bytes")
    print(f"\nUpdated entry:\n{json.dumps(new_entry, indent=2)}")


if __name__ == "__main__":
    main()
