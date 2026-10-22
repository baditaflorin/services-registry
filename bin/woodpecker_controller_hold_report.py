#!/usr/bin/env python3
"""How often does the Woodpecker load controller hold one agent's host? (reads its journal, no API)

The load-aware admission controller (templates/woodpecker-load-controller@.service) logs one
JSON line per node every interval, including the node's I/O pressure and how many of its agents
are schedulable. A node with zero schedulable agents is HELD: its agent takes no new jobs. A held
agent still looks online and healthy, so "the pilot got little traffic" can only be explained here.

Feed it the controller's journal on stdin:

  journalctl -u woodpecker-load-controller@ci.0exec --since -26h --no-pager -o cat \\
    | python3 bin/woodpecker_controller_hold_report.py --node <node name> --split 2026-10-19T20:13:52Z

`--split` (optional) prints the held share before and after a change, so a fix can be judged by the
controller's own log rather than by anyone's impression. Samples are about 30 s apart; a short
window proves little, because the controller resumes an agent only after many calm samples in a row.

`--daily-window 07-16` (optional) adds one line per UTC day with the held share inside those hours,
which is the number to judge a working day by: one good day is not a trend (2026-10-20 13 %, the
next day 42 %), and on 2026-10-22 the controller's journal held only the last ~30 hours, so save
the figures you need.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from typing import Iterable

UTC = dt.timezone.utc


def parse_instant(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def samples(lines: Iterable[str], node: str) -> list[dict]:
    """The `node_observed` records of one node, in input order; other lines are ignored."""
    out = []
    for line in lines:
        if node not in line or "node_observed" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("node") == node and rec.get("event") == "node_observed" and "timestamp" in rec:
            out.append(rec)
    return out


def held(rec: dict) -> bool:
    return rec.get("schedulable_agents", 1) == 0


def by_hour(recs: list[dict]) -> dict[str, dict[str, float]]:
    hours: dict[str, list[dict]] = collections.defaultdict(list)
    for rec in recs:
        hours[dt.datetime.fromtimestamp(rec["timestamp"], UTC).strftime("%m-%d %H:00")].append(rec)
    return {
        hour: {
            "samples": len(items),
            "held_pct": 100.0 * sum(held(r) for r in items) / len(items),
            "io_mean": sum(float(r.get("io_pressure_pct") or 0) for r in items) / len(items),
            "io_over_15_pct": 100.0 * sum(float(r.get("io_pressure_pct") or 0) > 15 for r in items) / len(items),
        }
        for hour, items in sorted(hours.items())
    }


def share(recs: list[dict]) -> tuple[int, float]:
    return len(recs), (100.0 * sum(held(r) for r in recs) / len(recs)) if recs else 0.0


def parse_window(text: str) -> tuple[int, int]:
    """'07-16' -> (7, 16): the UTC hours [7, 16)."""
    first, _, last = text.partition("-")
    start, end = int(first), int(last)
    if not 0 <= start < end <= 24:
        raise ValueError(f"window {text!r} must look like 07-16 (UTC hours, start before end)")
    return start, end


def by_day(recs: list[dict], window: tuple[int, int]) -> dict[str, dict[str, float]]:
    """Held share and mean I/O pressure per UTC day, counting only samples inside the hour window."""
    days: dict[str, list[dict]] = collections.defaultdict(list)
    for rec in recs:
        when = dt.datetime.fromtimestamp(rec["timestamp"], UTC)
        if window[0] <= when.hour < window[1]:
            days[when.strftime("%m-%d")].append(rec)
    return {
        day: {
            "samples": len(items),
            "held_pct": 100.0 * sum(held(r) for r in items) / len(items),
            "io_mean": sum(float(r.get("io_pressure_pct") or 0) for r in items) / len(items),
        }
        for day, items in sorted(days.items())
    }


def report(recs: list[dict], split: float | None, window: tuple[int, int] | None = None) -> str:
    lines = ["hour (UTC)   samples  held %  mean I/O pressure %  samples above 15 %"]
    for hour, row in by_hour(recs).items():
        lines.append(f"{hour:<12} {row['samples']:>7.0f}  {row['held_pct']:>5.0f}  {row['io_mean']:>18.1f}  {row['io_over_15_pct']:>17.0f}")
    n, pct = share(recs)
    lines.append(f"\nall samples: {n}, held {pct:.0f} %")
    if window is not None:
        lines.append(f"\nper day, only {window[0]:02d}:00-{window[1]:02d}:00 UTC (a day with few samples proves little):")
        lines.append("day     samples  held %  mean I/O pressure %")
        for day, row in by_day(recs, window).items():
            lines.append(f"{day:<7} {row['samples']:>7.0f}  {row['held_pct']:>5.0f}  {row['io_mean']:>18.1f}")
    if split is not None:
        for label, part in (("before", [r for r in recs if r["timestamp"] < split]),
                            ("after ", [r for r in recs if r["timestamp"] >= split])):
            n, pct = share(part)
            lines.append(f"{label} the split: {n} samples, held {pct:.0f} %")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", required=True, help="controller node name, e.g. the pilot host")
    ap.add_argument("--split", help="UTC instant, e.g. 2026-10-19T20:13:52Z")
    ap.add_argument("--daily-window", help="UTC hours, e.g. 07-16: also print the held share of each day inside them")
    args = ap.parse_args(argv)
    try:
        window = parse_window(args.daily_window) if args.daily_window else None
    except ValueError as err:
        ap.error(str(err))
    recs = samples(sys.stdin, args.node)
    if not recs:
        print(f"no node_observed records for {args.node!r} on stdin", file=sys.stderr)
        return 2
    print(report(recs, parse_instant(args.split) if args.split else None, window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
