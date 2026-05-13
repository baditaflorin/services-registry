#!/usr/bin/env python3
"""
trl-merge.py — append a TRL audit result to overrides.json.

Usage:
  bin/trl-merge.py <slug> <trl_json_response>
  echo '<trl_json>' | bin/trl-merge.py <slug>

Expects the haiku-returned JSON object:
  {"trl": 6, "trl_evidence": "...", "trl_ceiling": null, "trl_ceiling_reason": null}

Sets trl_assessed_at to today and trl_assessor to "haiku-backfill-<date>".
After merging, run `python3 bin/generate.py` to refresh services.json.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERRIDES = ROOT / "overrides.json"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    slug = sys.argv[1]
    raw = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read()
    data = json.loads(raw)

    today = _dt.date.today().isoformat()
    overrides = json.loads(OVERRIDES.read_text(), object_pairs_hook=OrderedDict)
    if slug not in overrides:
        overrides[slug] = OrderedDict()

    entry = overrides[slug]
    entry["trl"] = int(data["trl"])
    entry["trl_evidence"] = str(data["trl_evidence"])
    entry["trl_ceiling"] = data.get("trl_ceiling")  # int or None
    entry["trl_ceiling_reason"] = data.get("trl_ceiling_reason")
    entry["trl_assessed_at"] = today
    entry["trl_assessor"] = f"haiku-backfill-{today}"

    # keep top-level keys alphabetical (existing convention)
    out = OrderedDict((k, overrides[k]) for k in sorted(overrides.keys()))
    OVERRIDES.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"✓ merged TRL {entry['trl']} for {slug} (ceiling={entry['trl_ceiling']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
