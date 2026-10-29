#!/usr/bin/env python3
"""Check that every Woodpecker agent record is intact and that the load controller can find its agents by name.

A PATCH to /api/agents/{id} replaces the whole record. One that leaves out ``name`` (or ``custom_labels``) clears it, and the agent keeps
running with a record the load controller cannot match: it reports ``node_missing`` and stops managing that node's slots. Nothing else
notices, so this check writes Prometheus textfile metrics per plane:

* ``woodpecker_agents_unnamed``: agents whose record has an empty name;
* ``woodpecker_agents_without_labels``: agents whose record has no custom labels (routing labels such as site, mem, lv3 live there);
* ``woodpecker_load_controller_missing_agents``: per controller node, the names in its config that match no registered agent
  (an emptied name, or a stale config entry for an agent that is gone);
* ``woodpecker_agent_names_check_up``: 1 when the plane was read, 0 when it was not (an unread plane must not look clean).

The check only reads. A restart of an idle agent re-registers its name (``WOODPECKER_HOSTNAME``); that stays an operator step.

    woodpecker-agent-names-check --plane ci.0exec,https://ci.0exec.com,/etc/woodpecker-load-controller/ci.0exec.token,/etc/woodpecker-load-controller/ci.0exec.json \
        --output /var/lib/prometheus-node-exporter/textfile/woodpecker-agent-names.prom
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Callable

PER_PAGE = 50


def fetch_agents(base_url: str, token: str, timeout: float = 15) -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    page = 1
    while True:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/agents?page={page}&perPage={PER_PAGE}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "User-Agent": "services-registry-woodpecker-agent-names-check/1"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            rows = json.loads(response.read())
        if not isinstance(rows, list):
            raise ValueError("Woodpecker agents response must be a JSON array")
        agents.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < PER_PAGE:
            return agents
        page += 1


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def plane_metrics(plane: str, agents: list[dict[str, Any]] | None, config: dict[str, Any] | None) -> list[str]:
    """Exposition lines for one plane; ``agents`` is None when the plane could not be read."""
    p = label(plane)
    if agents is None:
        return [f'woodpecker_agent_names_check_up{{plane="{p}"}} 0']
    lines = [
        f'woodpecker_agent_names_check_up{{plane="{p}"}} 1',
        f'woodpecker_agents_unnamed{{plane="{p}"}} {sum(1 for a in agents if not str(a.get("name") or "").strip())}',
        f'woodpecker_agents_without_labels{{plane="{p}"}} {sum(1 for a in agents if not a.get("custom_labels"))}',
    ]
    if config is not None:
        names = {str(a.get("name") or "") for a in agents}
        for node in config.get("nodes", []):
            missing = [n for n in node.get("agent_names", []) if n not in names]
            lines.append(f'woodpecker_load_controller_missing_agents{{plane="{p}",node="{label(str(node.get("name", "")))}"}} {len(missing)}')
    return lines


HEADER = [
    "# HELP woodpecker_agent_names_check_up 1 when the plane's agent list was read, 0 when it was not.",
    "# TYPE woodpecker_agent_names_check_up gauge",
    "# HELP woodpecker_agents_unnamed Agent records with an empty name.",
    "# TYPE woodpecker_agents_unnamed gauge",
    "# HELP woodpecker_agents_without_labels Agent records with no custom labels.",
    "# TYPE woodpecker_agents_without_labels gauge",
    "# HELP woodpecker_load_controller_missing_agents Names in the load controller config that match no registered agent, per node.",
    "# TYPE woodpecker_load_controller_missing_agents gauge",
]


def collect(planes: list[tuple[str, str, str, str | None]], fetch: Callable[[str, str], list[dict[str, Any]]] = fetch_agents) -> str:
    body: list[str] = []
    for plane, url, token_file, config_file in planes:
        try:
            agents: list[dict[str, Any]] | None = fetch(url, Path(token_file).read_text().strip())
        except Exception as exc:  # an unread plane is reported as down, never as clean
            sys.stderr.write(f"{plane}: {exc}\n")
            agents = None
        config = None
        if config_file:
            try:
                config = json.loads(Path(config_file).read_text())
            except Exception as exc:
                sys.stderr.write(f"{plane}: config {config_file}: {exc}\n")
        body.extend(plane_metrics(plane, agents, config))
    # one HELP/TYPE per metric, then the samples grouped by metric as Prometheus expects
    samples: dict[str, list[str]] = {}
    for line in body:
        samples.setdefault(line.split("{", 1)[0], []).append(line)
    out: list[str] = []
    for i in range(0, len(HEADER), 2):
        name = HEADER[i].split()[2]
        out += HEADER[i:i + 2] + samples.get(name, [])
    return "\n".join(out) + "\n"


def write_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agent-names-", suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def parse_plane(value: str) -> tuple[str, str, str, str | None]:
    parts = value.split(",")
    if len(parts) not in (3, 4):
        raise argparse.ArgumentTypeError("--plane NAME,URL,TOKEN_FILE[,CONTROLLER_CONFIG]")
    return parts[0], parts[1], parts[2], parts[3] if len(parts) == 4 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--plane", action="append", type=parse_plane, required=True)
    parser.add_argument("--output", help="textfile path; stdout when omitted")
    args = parser.parse_args(argv)
    text = collect(args.plane)
    if args.output:
        write_atomic(args.output, text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
