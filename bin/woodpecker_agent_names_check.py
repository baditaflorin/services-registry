#!/usr/bin/env python3
"""Check that every Woodpecker agent record is intact and that the load controller can find its agents by name.

A PATCH to /api/agents/{id} that leaves out ``name`` stores an empty name (Woodpecker 3.17 persists only name and no_schedule; labels survive,
tested 2026-10-30). The agent keeps running with a record the load controller cannot match: it reports ``node_missing`` and stops managing
that node's slots. Nothing else notices, so this check writes Prometheus textfile metrics per plane:

* ``woodpecker_agents_unnamed``: agents whose record has an empty name;
* ``woodpecker_agents_without_labels``: agents whose record has no custom labels (routing labels such as site, mem, lv3 live there);
* ``woodpecker_load_controller_missing_agents``: per controller node, the names in its config that match no registered agent
  (an emptied name, or a stale config entry for an agent that is gone);
* ``woodpecker_agent_names_check_up``: 1 when the plane was read, 0 when it was not (an unread plane must not look clean).

By default the check only reads. With ``--repair --state FILE`` it also remembers every agent's name (by id) and, when a record's name is
empty, PATCHes the remembered name back with the record's own ``no_schedule`` and labels, so a wipe by any caller is undone within a minute
instead of hiding the agent from the controller until someone restarts it (the caller's drain stays as it set it). A name is restored only
when the id is known and no other agent already has that name. ``woodpecker_agent_names_repaired_total`` counts restores: a rise means some
caller is still PATCHing without a name. An agent whose name was never seen stays unnamed (alerted); a restart re-registers it
(``WOODPECKER_HOSTNAME``).

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


def patch_agent(base_url: str, token: str, agent: dict[str, Any], name: str, timeout: float = 15) -> None:
    body = json.dumps({"name": name, "no_schedule": bool(agent.get("no_schedule")), "custom_labels": agent.get("custom_labels") or {}}).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/agents/{agent['id']}", data=body, method="PATCH",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "services-registry-woodpecker-agent-names-check/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def repair_names(agents: list[dict[str, Any]], known: dict[str, str], patch: Callable[[dict[str, Any], str], None]) -> int:
    """Learn names into ``known`` (id -> name) and restore empty ones from it; returns how many were restored."""
    restored = 0
    for agent in agents:
        name = str(agent.get("name") or "").strip()
        if name:
            known[str(agent["id"])] = name
    present = {str(a.get("name") or "") for a in agents}
    for agent in agents:
        if str(agent.get("name") or "").strip():
            continue
        candidate = known.get(str(agent["id"]))
        if not candidate or candidate in present:
            continue
        try:
            patch(agent, candidate)
        except Exception as exc:  # stays unnamed, which the alert reports
            sys.stderr.write(f"restore name of agent {agent.get('id')}: {exc}\n")
            continue
        agent["name"] = candidate
        present.add(candidate)
        restored += 1
    return restored


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def plane_metrics(plane: str, agents: list[dict[str, Any]] | None, config: dict[str, Any] | None, repaired_total: int | None = None) -> list[str]:
    """Exposition lines for one plane; ``agents`` is None when the plane could not be read."""
    p = label(plane)
    if agents is None:
        return [f'woodpecker_agent_names_check_up{{plane="{p}"}} 0']
    lines = [
        f'woodpecker_agent_names_check_up{{plane="{p}"}} 1',
        f'woodpecker_agents_unnamed{{plane="{p}"}} {sum(1 for a in agents if not str(a.get("name") or "").strip())}',
        f'woodpecker_agents_without_labels{{plane="{p}"}} {sum(1 for a in agents if not a.get("custom_labels"))}',
    ]
    if repaired_total is not None:
        lines.append(f'woodpecker_agent_names_repaired_total{{plane="{p}"}} {repaired_total}')
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
    "# HELP woodpecker_agent_names_repaired_total Empty agent names restored from the remembered name (a rise means a caller still wipes names).",
    "# TYPE woodpecker_agent_names_repaired_total counter",
    "# HELP woodpecker_load_controller_missing_agents Names in the load controller config that match no registered agent, per node.",
    "# TYPE woodpecker_load_controller_missing_agents gauge",
]


def collect(planes: list[tuple[str, str, str, str | None]], fetch: Callable[[str, str], list[dict[str, Any]]] = fetch_agents,
            state: dict[str, Any] | None = None, patch: Callable[[str, str, dict[str, Any], str], None] = patch_agent) -> str:
    """``state`` (plane -> {"names": {id: name}, "repaired_total": n}) turns repair on; it is updated in place."""
    body: list[str] = []
    for plane, url, token_file, config_file in planes:
        repaired_total: int | None = None
        try:
            token = Path(token_file).read_text().strip()
            agents: list[dict[str, Any]] | None = fetch(url, token)
            if state is not None:
                entry = state.setdefault(plane, {})
                entry.setdefault("names", {})
                restored = repair_names(agents, entry["names"], lambda agent, name, u=url, t=token: patch(u, t, agent, name))
                entry["repaired_total"] = int(entry.get("repaired_total", 0)) + restored
                repaired_total = entry["repaired_total"]
        except Exception as exc:  # an unread plane is reported as down, never as clean
            sys.stderr.write(f"{plane}: {exc}\n")
            agents = None
        config = None
        if config_file:
            try:
                config = json.loads(Path(config_file).read_text())
            except Exception as exc:
                sys.stderr.write(f"{plane}: config {config_file}: {exc}\n")
        body.extend(plane_metrics(plane, agents, config, repaired_total))
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
    parser.add_argument("--repair", action="store_true", help="restore an emptied agent name from the remembered one (needs --state)")
    parser.add_argument("--state", help="JSON file that remembers agent names (id -> name) between runs")
    args = parser.parse_args(argv)
    if args.repair and not args.state:
        parser.error("--repair needs --state")
    state: dict[str, Any] | None = None
    if args.repair:
        try:
            state = json.loads(Path(args.state).read_text())
        except FileNotFoundError:
            state = {}
        except Exception as exc:  # a corrupt state must not wipe what was remembered: stop and say so
            sys.stderr.write(f"state {args.state}: {exc}\n")
            return 2
    text = collect(args.plane, state=state)
    if state is not None:
        write_atomic(args.state, json.dumps(state, indent=1, sort_keys=True) + "\n")
    if args.output:
        write_atomic(args.output, text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
