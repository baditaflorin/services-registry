#!/usr/bin/env python3
"""Before/after report for the shared CI cache pilot on one Woodpecker agent.

Question it answers: did workflows run faster on the pilot agent after the cache
was switched on, beyond what happened on the other agents over the same period?

Method (difference in differences, per repository and workflow):
  1. Read recent successful workflow runs from Woodpecker's API (the same client,
     token handling and endpoints as ci_execution_report.py; tokens are never
     printed).
  2. Split each (repo, workflow) into four cells: pilot agent or control agents,
     before or after --cutover.
  3. For every (repo, workflow) that has at least --min-runs in all four cells,
     take median_after / median_before on the pilot and on the controls.
  4. The effect is median(pilot ratios) / median(control ratios). Below 1.0 means
     the pilot got faster than the controls did over the same window, which
     cancels fleet-wide load and time-of-day drift. 0.60 means 40% faster.

The gate workflow (named "woodpecker") is excluded: its "run time" is mostly waiting
for the other workflows, not work. Only successful workflows count; a workflow with
no agent id or no timestamps is skipped.

  ci_cache_pilot_report.py --plane ci.0exec.com --pilot-agent 0own-build-agent \\
      --cutover 2026-10-20T09:00:00Z --repos-file go-sample-repos.txt

Acceptance criteria suggested in docs/runbooks/woodpecker-ci-go-cache-pilot.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_execution_report as cer  # noqa: E402  (shared API client and config)

DEFAULT_EXCLUDED_WORKFLOWS = frozenset({"woodpecker"})


@dataclass(frozen=True)
class Run:
    repo: str
    workflow: str
    agent_id: int
    event: str
    started: int
    finished: int

    @property
    def seconds(self) -> int:
        return self.finished - self.started


REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def valid_repo_names(lines: Iterable[str]) -> list[str]:
    """owner/name per line; blanks and # comments skipped; anything else is an error
    (a name is placed in an API URL path, so it must not carry slashes or dots tricks)."""
    names = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not REPO_NAME.match(line) or ".." in line:
            raise ValueError(f"not an owner/name repository: {line!r}")
        names.append(line)
    return names


def parse_instant(value: str) -> int:
    """ISO 8601 (a trailing Z means UTC) or plain unix seconds -> unix seconds."""
    text = value.strip()
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return int(moment.timestamp())


def runs_from_detail(
    repo: str,
    event: str,
    detail: dict[str, Any],
    excluded: Iterable[str] = DEFAULT_EXCLUDED_WORKFLOWS,
) -> list[Run]:
    """Successful, timed, agent-attributed workflow runs of one pipeline."""
    skip = set(excluded)
    runs = []
    for workflow in detail.get("workflows") or []:
        name = str(workflow.get("name", ""))
        if name in skip or workflow.get("state") != "success":
            continue
        agent_id = int(workflow.get("agent_id") or 0)
        started = int(workflow.get("started") or 0)
        finished = int(workflow.get("finished") or 0)
        if agent_id <= 0 or started <= 0 or finished < started:
            continue
        runs.append(Run(repo, name, agent_id, event, started, finished))
    return runs


def resolve_agent_ids(agents: dict[int, str], selector: str) -> set[int]:
    """Agent selector -> agent ids. A number is an id; otherwise an exact name."""
    if selector.isdigit() and int(selector) in agents:
        return {int(selector)}
    matched = {agent_id for agent_id, name in agents.items() if name == selector}
    if not matched:
        known = ", ".join(sorted(agents.values())) or "none"
        raise ValueError(f"no agent named or numbered {selector!r}; known agents: {known}")
    return matched


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def compare(
    runs: list[Run],
    pilot_ids: set[int],
    cutover: int,
    window_seconds: int,
    min_runs: int = 3,
) -> dict[str, Any]:
    """Difference-in-differences summary of `runs` around `cutover`."""
    lo, hi = cutover - window_seconds, cutover + window_seconds
    cells: dict[tuple[str, str, bool, bool], list[int]] = {}
    for run in runs:
        if not lo <= run.started <= hi:
            continue
        key = (run.repo, run.workflow, run.agent_id in pilot_ids, run.started >= cutover)
        cells.setdefault(key, []).append(run.seconds)

    def secs(repo: str, workflow: str, pilot: bool, after: bool) -> list[int]:
        return cells.get((repo, workflow, pilot, after), [])

    pairs = sorted({(repo, workflow) for repo, workflow, _p, _a in cells})
    pilot_ratios: list[float] = []
    control_ratios: list[float] = []
    rows = []
    for repo, workflow in pairs:
        cell_runs = [secs(repo, workflow, p, a) for p in (True, False) for a in (False, True)]
        if any(len(c) < min_runs for c in cell_runs):
            continue
        pb, pa, cb, ca = (statistics.median(c) for c in cell_runs)
        if pb <= 0 or cb <= 0:
            continue
        pilot_ratios.append(pa / pb)
        control_ratios.append(ca / cb)
        rows.append(
            {
                "repo": repo,
                "workflow": workflow,
                "pilot_before_s": pb,
                "pilot_after_s": pa,
                "control_before_s": cb,
                "control_after_s": ca,
            }
        )

    def group(pilot: bool, after: bool) -> dict[str, Any]:
        values = [
            s
            for (_r, _w, is_pilot, is_after), seconds in cells.items()
            if is_pilot == pilot and is_after == after
            for s in seconds
        ]
        if not values:
            return {"runs": 0}
        return {
            "runs": len(values),
            "median_s": statistics.median(values),
            "p90_s": percentile(values, 0.9),
        }

    summary: dict[str, Any] = {
        "pairs_compared": len(rows),
        "unpaired": {
            "pilot_before": group(True, False),
            "pilot_after": group(True, True),
            "control_before": group(False, False),
            "control_after": group(False, True),
        },
        "rows": rows,
    }
    if rows:
        pilot_ratio = statistics.median(pilot_ratios)
        control_ratio = statistics.median(control_ratios)
        summary["pilot_after_over_before"] = round(pilot_ratio, 3)
        summary["control_after_over_before"] = round(control_ratio, 3)
        summary["effect"] = round(pilot_ratio / control_ratio, 3)
    else:
        summary["note"] = (
            "no (repo, workflow) had enough runs in all four cells; widen --window-days, "
            "lower --min-runs, or add repositories"
        )
    return summary


def collect_runs(
    client: cer.WoodpeckerClient,
    repo_names: list[str] | None,
    pipelines_per_repo: int,
    max_repos: int,
    workers: int,
    excluded: Iterable[str],
) -> list[Run]:
    if repo_names:
        repositories = []
        for name in repo_names[:max_repos]:
            repo = client.get(f"/repos/lookup/{name}")
            repositories.append(repo)
    else:
        repositories = client.repositories()[:max_repos]

    def per_repo(repository: dict[str, Any]) -> list[Run]:
        collected: list[Run] = []
        for candidate in client.repo_candidates(repository, pipelines_per_repo, False):
            detail = client.pipeline_detail(candidate)
            collected.extend(
                runs_from_detail(candidate.repository, candidate.event, detail, excluded)
            )
        return collected

    runs: list[Run] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(per_repo, repositories):
            runs.extend(part)
    return runs


def print_human(summary: dict[str, Any], pilot_label: str) -> None:
    print(f"pilot agent: {pilot_label}")
    print(f"(repo, workflow) pairs compared: {summary['pairs_compared']}")
    for label, cell in summary["unpaired"].items():
        if cell["runs"]:
            print(
                f"  {label:<15} runs={cell['runs']:<5} median={cell['median_s']:.0f}s "
                f"p90={cell['p90_s']:.0f}s"
            )
        else:
            print(f"  {label:<15} runs=0")
    if "effect" in summary:
        print(f"pilot after/before   : {summary['pilot_after_over_before']}")
        print(f"control after/before : {summary['control_after_over_before']}")
        print(f"effect (pilot/control): {summary['effect']}   (below 1.0 = faster than the controls)")
    else:
        print(summary["note"])


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=cer.DEFAULT_CONFIG)
    parser.add_argument("--plane", required=True, help="control plane name in the config, e.g. ci.0exec.com")
    parser.add_argument("--pilot-agent", required=True, help="agent name or numeric id that got the cache")
    parser.add_argument("--cutover", required=True, help="when the cache went live: ISO 8601 UTC or unix seconds")
    parser.add_argument("--repos-file", type=Path, help="owner/name per line; default: every active repo")
    parser.add_argument("--window-days", type=float, default=7.0)
    parser.add_argument("--min-runs", type=int, default=3)
    parser.add_argument("--pipelines-per-repo", type=int, default=60)
    parser.add_argument("--max-repos", type=int, default=400)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--exclude-workflow", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = cer.load_config(args.config)
    plane = next((p for p in config["control_planes"] if p["name"] == args.plane), None)
    if plane is None:
        names = ", ".join(p["name"] for p in config["control_planes"])
        print(f"unknown plane {args.plane!r}; configured: {names}", file=sys.stderr)
        return 2
    client = cer.WoodpeckerClient(plane, args.workers)
    pilot_ids = resolve_agent_ids(client.agents(), args.pilot_agent)
    repo_names = None
    if args.repos_file:
        repo_names = valid_repo_names(args.repos_file.read_text(encoding="utf-8").splitlines())
    excluded = set(DEFAULT_EXCLUDED_WORKFLOWS) | set(args.exclude_workflow)
    runs = collect_runs(
        client, repo_names, args.pipelines_per_repo, args.max_repos, args.workers, excluded
    )
    summary = compare(
        runs,
        pilot_ids,
        parse_instant(args.cutover),
        int(args.window_days * 86400),
        args.min_runs,
    )
    summary["runs_read"] = len(runs)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_human(summary, args.pilot_agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
