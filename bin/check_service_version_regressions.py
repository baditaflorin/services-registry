#!/usr/bin/env python3
"""Fail CI when a registry edit lowers an existing service's SemVer version."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SEMVER = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def semver_precedence(value: str) -> tuple[Any, ...] | None:
    """Return a comparable SemVer precedence key, ignoring build metadata."""
    match = SEMVER.fullmatch(value)
    if not match:
        return None
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    prerelease = match.group(4)
    if prerelease is None:
        return major, minor, patch, 1, ()
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    )
    return major, minor, patch, 0, identifiers


def version_regressions(base: list[dict[str, Any]], head: list[dict[str, Any]]) -> list[str]:
    base_by_id = {entry.get("id"): entry for entry in base if isinstance(entry, dict)}
    failures: list[str] = []
    for entry in head:
        if not isinstance(entry, dict):
            continue
        service_id = entry.get("id")
        previous = base_by_id.get(service_id)
        if previous is None:
            continue
        old = previous.get("version")
        new = entry.get("version")
        if old == new:
            continue
        old_key = semver_precedence(old) if isinstance(old, str) else None
        new_key = semver_precedence(new) if isinstance(new, str) else None
        if old_key is None or new_key is None:
            failures.append(f"{service_id}: cannot compare changed non-SemVer version {old!r} -> {new!r}")
        elif new_key < old_key:
            failures.append(f"{service_id}: service version rollback {old} -> {new}")
    return failures


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_service_version_regressions.py <base-ref>", file=sys.stderr)
        return 2
    base_ref = sys.argv[1]
    try:
        base_text = subprocess.run(
            ["git", "show", f"{base_ref}:services.json"],
            check=True, capture_output=True, text=True,
        ).stdout
        base = json.loads(base_text)
        head = json.loads(Path("services.json").read_text(encoding="utf-8"))
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        print(f"version regression check could not load registry data: {exc}", file=sys.stderr)
        return 2
    failures = version_regressions(base, head)
    if failures:
        print("service catalog version regression(s) detected:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("service catalog versions do not regress from the base ref")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
