#!/usr/bin/env python3
"""Report which physical hosts executed the most recent Woodpecker gates.

The report queries Woodpecker's API on every configured control plane, merges
pipeline records by creation time, resolves workflow agent IDs, and classifies
each pipeline through an explicit agent-name-to-host policy. Tokens are read
from files or environment variables and are never emitted.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "ci-execution-report.json"
TERMINAL_STATUSES = {"success", "failure", "error", "killed", "declined"}


@dataclass(frozen=True)
class Candidate:
    control_plane: str
    repo_id: int
    repository: str
    number: int
    created: int
    status: str
    event: str


class WoodpeckerClient:
    def __init__(self, config: dict[str, Any], workers: int) -> None:
        self.config = config
        self.name = config["name"]
        self.base_url = config["url"].rstrip("/")
        self.workers = workers
        self.token = self._read_token(config)

    @staticmethod
    def _read_token(config: dict[str, Any]) -> str:
        token = os.getenv(config.get("token_env", "")) if config.get("token_env") else None
        if not token and config.get("token_file"):
            token = Path(config["token_file"]).read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"no API token available for {config['name']}")
        return token

    def get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        suffix = ""
        if query:
            suffix = "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            f"{self.base_url}/api{path}{suffix}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "services-registry-ci-execution-report/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"{self.name} API returned HTTP {exc.code} for {path}"
            ) from exc

    def repositories(self) -> list[dict[str, Any]]:
        repositories = []
        page = 1
        while True:
            rows = self.get(
                "/repos",
                {"all": "true", "perPage": 100, "page": page},
            )
            repositories.extend(row for row in rows if row.get("active") is True)
            if len(rows) < 100:
                return repositories
            page += 1

    def repo_candidates(
        self,
        repository: dict[str, Any],
        limit: int,
        include_active: bool,
    ) -> list[Candidate]:
        candidates = []
        page = 1
        while len(candidates) < limit:
            rows = self.get(
                f"/repos/{repository['id']}/pipelines",
                {"perPage": min(100, limit), "page": page},
            )
            for pipeline in rows:
                status = str(pipeline.get("status", "unknown"))
                if include_active or status in TERMINAL_STATUSES:
                    candidates.append(
                        Candidate(
                            control_plane=self.name,
                            repo_id=int(repository["id"]),
                            repository=repository["full_name"],
                            number=int(pipeline["number"]),
                            created=int(pipeline.get("created") or 0),
                            status=status,
                            event=str(pipeline.get("event", "unknown")),
                        )
                    )
            if len(rows) < min(100, limit):
                break
            page += 1
        return candidates[:limit]

    def candidates(self, limit: int, include_active: bool) -> list[Candidate]:
        repositories = self.repositories()
        candidates = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [
                pool.submit(self.repo_candidates, repo, limit, include_active)
                for repo in repositories
            ]
            for future in concurrent.futures.as_completed(futures):
                candidates.extend(future.result())
        return candidates

    def agents(self) -> dict[int, str]:
        return {
            int(agent["id"]): str(agent["name"])
            for agent in self.get("/agents", {"perPage": 100})
        }

    def pipeline_detail(self, candidate: Candidate) -> dict[str, Any]:
        return self.get(
            f"/repos/{candidate.repo_id}/pipelines/{candidate.number}"
        )


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("version") != 1 or not config.get("control_planes"):
        raise ValueError("config must declare version 1 and control_planes")
    names = [plane.get("name") for plane in config["control_planes"]]
    if len(names) != len(set(names)):
        raise ValueError("control-plane names must be unique")
    for plane in config["control_planes"]:
        if not plane.get("url") or not plane.get("default_execution_host"):
            raise ValueError(f"control plane {plane.get('name')} is incomplete")
    return config


def classify_pipeline(
    detail: dict[str, Any],
    agent_names: dict[int, str],
    plane_config: dict[str, Any],
) -> tuple[str, list[str]]:
    workflows = detail.get("workflows") or []
    agent_ids = sorted(
        {
            int(workflow["agent_id"])
            for workflow in workflows
            if workflow.get("agent_id") not in {None, 0}
        }
    )
    if not agent_ids:
        return "unassigned", []

    agent_hosts = plane_config.get("agent_hosts", {})
    agent_id_hosts = plane_config.get("agent_id_hosts", {})
    hosts = []
    names = []
    for agent_id in agent_ids:
        name = agent_names.get(agent_id)
        explicit_id_host = agent_id_hosts.get(str(agent_id))
        if explicit_id_host:
            hosts.append(explicit_id_host)
            names.append(name or f"retired-agent-{agent_id}")
            continue
        if name is None:
            hosts.append("unknown")
            names.append(f"unknown-agent-{agent_id}")
            continue
        names.append(name)
        hosts.append(agent_hosts.get(name, plane_config["default_execution_host"]))
    unique_hosts = set(hosts)
    return (hosts[0] if len(unique_hosts) == 1 else "mixed"), names


def utc(timestamp: int) -> str:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def build_report(
    config: dict[str, Any],
    limit: int,
    workers: int,
    include_active: bool,
) -> dict[str, Any]:
    clients = {
        plane["name"]: WoodpeckerClient(plane, workers)
        for plane in config["control_planes"]
    }
    candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = {
            pool.submit(client.candidates, limit, include_active): name
            for name, client in clients.items()
        }
        for future in concurrent.futures.as_completed(futures):
            candidates.extend(future.result())

    selected = sorted(
        candidates,
        key=lambda item: (
            item.created,
            item.control_plane,
            item.repo_id,
            item.number,
        ),
        reverse=True,
    )[:limit]

    plane_configs = {plane["name"]: plane for plane in config["control_planes"]}
    agent_names = {name: client.agents() for name, client in clients.items()}
    records = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(clients[item.control_plane].pipeline_detail, item): item
            for item in selected
        }
        details = {item: future.result() for future, item in futures.items()}

    host_counts: dict[str, int] = {}
    plane_counts: dict[str, dict[str, int]] = {}
    for item in selected:
        host, agents = classify_pipeline(
            details[item],
            agent_names[item.control_plane],
            plane_configs[item.control_plane],
        )
        host_counts[host] = host_counts.get(host, 0) + 1
        by_host = plane_counts.setdefault(item.control_plane, {})
        by_host[host] = by_host.get(host, 0) + 1
        records.append(
            {
                "created": item.created,
                "created_utc": utc(item.created),
                "control_plane": item.control_plane,
                "repository": item.repository,
                "pipeline_number": item.number,
                "event": item.event,
                "status": item.status,
                "execution_host": host,
                "agents": agents,
            }
        )

    return {
        "requested": limit,
        "counted": len(records),
        "include_active": include_active,
        "newest_utc": records[0]["created_utc"] if records else None,
        "oldest_utc": records[-1]["created_utc"] if records else None,
        "host_counts": dict(sorted(host_counts.items())),
        "control_plane_host_counts": {
            name: dict(sorted(counts.items()))
            for name, counts in sorted(plane_counts.items())
        },
        "records": records,
    }


def print_human(report: dict[str, Any]) -> None:
    print(
        f"CI execution report: {report['counted']} of {report['requested']} "
        f"most recent {'all' if report['include_active'] else 'completed'} gates"
    )
    print(f"window: {report['oldest_utc']} -> {report['newest_utc']}")
    print("by physical execution host:")
    for host, count in report["host_counts"].items():
        print(f"  {host}: {count}")
    print("by control plane and physical host:")
    for plane, counts in report["control_plane_host_counts"].items():
        rendered = ", ".join(f"{host}={count}" for host, count in counts.items())
        print(f"  {plane}: {rendered}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--include-active", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.limit < 1 or args.workers < 1:
        print("--limit and --workers must be positive", file=sys.stderr)
        return 2
    try:
        report = build_report(
            load_config(args.config),
            args.limit,
            args.workers,
            args.include_active,
        )
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"CI execution report failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_human(report)
    return 0 if report["counted"] == args.limit else 1


if __name__ == "__main__":
    raise SystemExit(main())
