#!/usr/bin/env python3
"""Before/after report for the CI cache pilot, read through `fleet-runner` (no API token here).

`ci_cache_pilot_report.py` talks to Woodpecker's API and needs a token. This variant reads
the same facts through `fleet-runner woodpecker pipeline --json`, the read-only verb that
loads its own credential, for a range of pipeline numbers of ONE repository. It reuses that
script's `runs_from_detail` and `compare`, so the method (difference in differences per
workflow, successful runs only, gate workflow excluded) is identical, and it adds a per-step
table, because a workflow median hides where the time goes:

  ci_cache_pilot_fleet_runner_report.py --repo owner/name --first 8450 --last 9570 \\
      --pilot-agent-id 99 --cutover 2026-10-17T10:50:47Z

Find the pilot agent's numeric id by joining `fleet-runner woodpecker queue --json` (agent
NAME per running workflow) with `pipeline --json` (agent_id per workflow) for the same
pipeline and workflow name. Pipeline numbers are per repository and not every number exists
(on 2026-10-19 about a fifth of a busy repository's numbers answered HTTP 404, for example
9485, so they are real gaps, not read errors); numbers that cannot be read are counted and
reported as "unreadable", never silently dropped.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_cache_pilot_report as base  # noqa: E402

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
Runner = Callable[[str, str, int], "dict[str, Any] | None"]


def run_fleet_runner(plane: str, repo: str, number: int) -> dict[str, Any] | None:
    """One pipeline's JSON, or None if the verb failed (retried twice)."""
    for _ in range(3):
        proc = subprocess.run(
            ["fleet-runner", "woodpecker", "pipeline", "--plane", plane, "--repo", repo,
             "--number", str(number), "--json"],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip().startswith("{"):
            return json.loads(proc.stdout)
    return None


def fetch(plane: str, repo: str, numbers: Iterable[int], workers: int = 4,
          runner: Runner = run_fleet_runner) -> tuple[list[dict[str, Any]], int]:
    """(pipelines, number that could not be read). Order follows `numbers`."""
    nums = list(numbers)
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        details = list(pool.map(lambda n: runner(plane, repo, n), nums))
    got = [dict(d, number=n) for n, d in zip(nums, details) if d]
    return got, len(nums) - len(got)


def step_overhead(pipelines: list[dict[str, Any]], pilot_ids: set[int],
                  after: int | None = None) -> dict[str, dict[str, tuple[int, float]]]:
    """Median seconds of every step of successful workflows: pilot agent against the others.

    With `after`, only workflows that started at or after that epoch second count.
    """
    cells: dict[tuple[str, bool], list[int]] = {}
    for pipeline in pipelines:
        for workflow in pipeline.get("workflows") or []:
            agent = int(workflow.get("agent_id") or 0)
            if workflow.get("state") != "success" or agent <= 0:
                continue
            if after is not None and int(workflow.get("started") or 0) < after:
                continue
            for step in workflow.get("children") or workflow.get("steps") or []:
                start, end = int(step.get("started") or 0), int(step.get("finished") or 0)
                if start > 0 and end >= start:
                    cells.setdefault((str(step.get("name")), agent in pilot_ids), []).append(end - start)
    out: dict[str, dict[str, tuple[int, float]]] = {}
    for (name, pilot), values in cells.items():
        out.setdefault(name, {})["pilot" if pilot else "others"] = (len(values), statistics.median(values))
    return out


def summarize(pipelines: list[dict[str, Any]], repo: str, pilot_ids: set[int], cutover: int,
              window_days: float = 3.0, min_runs: int = 3) -> dict[str, Any]:
    runs: list[base.Run] = []
    for pipeline in pipelines:
        runs += base.runs_from_detail(repo, str(pipeline.get("event") or ""), pipeline)
    summary = base.compare(runs, pilot_ids, cutover, int(window_days * 86400), min_runs=min_runs)
    summary["steps_after_cutover"] = step_overhead(pipelines, pilot_ids, after=cutover)
    summary["workflow_runs"] = len(runs)
    return summary


def print_summary(summary: dict[str, Any], fetched: int, failed: int) -> None:
    print(f"pipelines read: {fetched}, unreadable: {failed}; workflow runs used: {summary['workflow_runs']}")
    base.print_human(summary, "pilot agent")
    steps = summary["steps_after_cutover"]
    both = sorted((s for s in steps if {"pilot", "others"} <= set(steps[s])),
                  key=lambda s: -steps[s]["others"][0])[:15]
    if both:
        print("\nstep medians after the cutover (n, seconds): pilot agent | other agents")
        for name in both:
            p, o = steps[name]["pilot"], steps[name]["others"]
            print(f"  {name:<28} {p[0]:>4}, {p[1]:>6.0f} | {o[0]:>4}, {o[1]:>6.0f}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plane", default="0exec")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--first", type=int, required=True, help="first pipeline number")
    ap.add_argument("--last", type=int, required=True, help="last pipeline number")
    ap.add_argument("--pilot-agent-id", type=int, action="append", required=True)
    ap.add_argument("--cutover", required=True, help="UTC instant, e.g. 2026-10-17T10:50:47Z")
    ap.add_argument("--window-days", type=float, default=3.0)
    ap.add_argument("--min-runs", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    if not REPO_RE.match(args.repo):
        ap.error("--repo must look like owner/name")
    if not 0 < args.first <= args.last or args.last - args.first > 5000:
        ap.error("--first/--last must be a positive range of at most 5000 pipelines")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipelines, failed = fetch(args.plane, args.repo, range(args.first, args.last + 1), args.workers)
    summary = summarize(pipelines, args.repo, set(args.pilot_agent_id), base.parse_instant(args.cutover),
                        args.window_days, args.min_runs)
    if args.json:
        print(json.dumps({"fetched": len(pipelines), "unreadable": failed, **summary}, indent=2, default=list))
    else:
        print_summary(summary, len(pipelines), failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
