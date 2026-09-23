#!/usr/bin/env python3
"""Drain and restore Woodpecker agents using node-exporter pressure signals.

The controller is dry-run by default. In apply mode it only restores agents
that it previously drained itself; an operator's manual no_schedule setting is
never cleared. State is persisted so hysteresis survives process restarts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


@dataclass(frozen=True)
class NodeSnapshot:
    cpu_idle_seconds: float
    cpu_total_seconds: float
    memory_available_pct: float
    disk_available_pct: float


@dataclass(frozen=True)
class Pressure:
    cpu_pct: float | None
    memory_available_pct: float
    disk_available_pct: float
    breaches: tuple[str, ...]
    recovered: bool


def parse_labels(raw: str) -> dict[str, str]:
    return {
        key: bytes(value, "utf-8").decode("unicode_escape")
        for key, value in LABEL_RE.findall(raw)
    }


def prometheus_samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    samples = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            lhs, raw_value = line.rsplit(None, 1)
            value = float(raw_value)
        except (ValueError, TypeError):
            continue
        if "{" in lhs and lhs.endswith("}"):
            name, raw_labels = lhs[:-1].split("{", 1)
            labels = parse_labels(raw_labels)
        else:
            name, labels = lhs, {}
        samples.append((name, labels, value))
    return samples


def node_snapshot(text: str, mountpoint: str = "/") -> NodeSnapshot:
    cpu_idle = 0.0
    cpu_total = 0.0
    memory_available = memory_total = None
    disk_available = disk_total = None
    for name, labels, value in prometheus_samples(text):
        if name == "node_cpu_seconds_total":
            mode = labels.get("mode")
            if mode in {"guest", "guest_nice"}:
                continue
            cpu_total += value
            if mode in {"idle", "iowait"}:
                cpu_idle += value
        elif name == "node_memory_MemAvailable_bytes":
            memory_available = value
        elif name == "node_memory_MemTotal_bytes":
            memory_total = value
        elif name == "node_filesystem_avail_bytes" and labels.get("mountpoint") == mountpoint:
            disk_available = value
        elif name == "node_filesystem_size_bytes" and labels.get("mountpoint") == mountpoint:
            disk_total = value
    if cpu_total <= 0 or memory_total in {None, 0} or disk_total in {None, 0}:
        raise ValueError("node-exporter response is missing CPU, memory, or filesystem metrics")
    assert memory_available is not None and disk_available is not None
    return NodeSnapshot(
        cpu_idle,
        cpu_total,
        100.0 * memory_available / memory_total,
        100.0 * disk_available / disk_total,
    )


def evaluate_pressure(
    current: NodeSnapshot,
    previous: NodeSnapshot | None,
    thresholds: dict[str, float],
) -> Pressure:
    cpu_pct = None
    if previous is not None:
        total_delta = current.cpu_total_seconds - previous.cpu_total_seconds
        idle_delta = current.cpu_idle_seconds - previous.cpu_idle_seconds
        if total_delta > 0 and 0 <= idle_delta <= total_delta:
            cpu_pct = 100.0 * (1.0 - idle_delta / total_delta)

    breaches = []
    if cpu_pct is not None and cpu_pct >= thresholds["cpu_stop_pct"]:
        breaches.append("cpu")
    if current.memory_available_pct <= thresholds["memory_stop_pct"]:
        breaches.append("memory")
    if current.disk_available_pct <= thresholds["disk_stop_pct"]:
        breaches.append("disk")

    recovered = (
        cpu_pct is not None
        and cpu_pct <= thresholds["cpu_resume_pct"]
        and current.memory_available_pct >= thresholds["memory_resume_pct"]
        and current.disk_available_pct >= thresholds["disk_resume_pct"]
    )
    return Pressure(
        cpu_pct,
        current.memory_available_pct,
        current.disk_available_pct,
        tuple(breaches),
        recovered,
    )


def update_hysteresis(
    agent_state: dict[str, Any],
    pressure: Pressure,
    no_schedule: bool,
    stop_samples: int,
    resume_samples: int,
) -> str | None:
    if pressure.breaches:
        agent_state["overload_samples"] = int(agent_state.get("overload_samples", 0)) + 1
        agent_state["recovery_samples"] = 0
    elif pressure.recovered:
        agent_state["overload_samples"] = 0
        agent_state["recovery_samples"] = int(agent_state.get("recovery_samples", 0)) + 1
    else:
        agent_state["overload_samples"] = 0
        agent_state["recovery_samples"] = 0

    if not no_schedule and agent_state["overload_samples"] >= stop_samples:
        return "drain"
    if (
        no_schedule
        and agent_state.get("managed_no_schedule") is True
        and agent_state["recovery_samples"] >= resume_samples
    ):
        return "restore"
    return None


def drain_allowed(schedulable_agents: int, minimum_schedulable_agents: int) -> bool:
    """Keep a last-resort agent eligible when every node reports pressure."""
    return schedulable_agents > minimum_schedulable_agents


def drain_priority(pressure: Pressure) -> tuple[int, float, float, float]:
    """Sort the most resource-constrained agents out of admission first."""
    return (
        len(pressure.breaches),
        pressure.cpu_pct if pressure.cpu_pct is not None else -1.0,
        -pressure.memory_available_pct,
        -pressure.disk_available_pct,
    )


class HTTPClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, path: str, method: str = "GET", payload: Any = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "services-registry-woodpecker-load-controller/1",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def agents(self) -> list[dict[str, Any]]:
        return self._request("/api/agents?perPage=100")

    def queue(self) -> dict[str, Any]:
        return self._request("/api/queue/info")

    def set_no_schedule(self, agent: dict[str, Any], value: bool) -> None:
        self._request(
            f"/api/agents/{agent['id']}",
            method="PATCH",
            payload={"name": agent["name"], "no_schedule": value},
        )


def fetch_text(url: str, timeout: float = 10) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "services-registry-woodpecker-load-controller/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists() and default is not None:
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def snapshot_from_state(agent_state: dict[str, Any]) -> NodeSnapshot | None:
    raw = agent_state.get("previous_snapshot")
    return NodeSnapshot(**raw) if isinstance(raw, dict) else None


def log_event(**fields: Any) -> None:
    fields["timestamp"] = int(time.time())
    print(json.dumps(fields, sort_keys=True), flush=True)


def run_cycle_v1(
    config: dict[str, Any],
    state: dict[str, Any],
    client: HTTPClient,
    apply: bool,
) -> bool:
    remote_agents = {agent["name"]: agent for agent in client.agents()}
    queue = client.queue()
    log_event(
        event="queue_observed",
        pending=len(queue.get("pending", [])),
        running=len(queue.get("running", [])),
        paused=bool(queue.get("paused", False)),
    )
    thresholds = config["thresholds"]
    changed = False
    configured_names = {entry["name"] for entry in config["agents"]}
    minimum_schedulable = int(config.get("minimum_schedulable_agents", 1))
    schedulable_remaining = sum(
        1
        for name, agent in remote_agents.items()
        if name in configured_names and not bool(agent.get("no_schedule"))
    )
    missing = sorted(configured_names - remote_agents.keys())
    for name in missing:
        log_event(event="agent_missing", agent=name)

    observations = []
    for entry in config["agents"]:
        name = entry["name"]
        remote = remote_agents.get(name)
        if remote is None:
            continue
        agent_state = state.setdefault("agents", {}).setdefault(name, {})
        try:
            current = node_snapshot(
                fetch_text(entry["metrics_url"]),
                entry.get("filesystem_mountpoint", "/"),
            )
            pressure = evaluate_pressure(current, snapshot_from_state(agent_state), thresholds)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # Telemetry loss is not proof of host pressure. Leave scheduling
            # unchanged and alert instead of accidentally draining the fleet.
            log_event(event="metrics_error", agent=name, error=str(exc))
            continue

        agent_state["previous_snapshot"] = current.__dict__
        action = update_hysteresis(
            agent_state,
            pressure,
            bool(remote.get("no_schedule")),
            int(config["stop_samples"]),
            int(config["resume_samples"]),
        )
        log_event(
            event="agent_observed",
            agent=name,
            cpu_pct=None if pressure.cpu_pct is None else round(pressure.cpu_pct, 2),
            memory_available_pct=round(pressure.memory_available_pct, 2),
            disk_available_pct=round(pressure.disk_available_pct, 2),
            breaches=list(pressure.breaches),
            no_schedule=bool(remote.get("no_schedule")),
            overload_samples=agent_state["overload_samples"],
            recovery_samples=agent_state["recovery_samples"],
            proposed_action=action,
            apply=apply,
        )
        observations.append((name, remote, agent_state, pressure, action))

    if apply:
        # Recover healthy capacity before considering drains. Drain candidates
        # are ordered by current pressure so the minimum remaining capacity is
        # the healthiest available agent, not an arbitrary configuration row.
        restores = [item for item in observations if item[4] == "restore"]
        drains = sorted(
            (item for item in observations if item[4] == "drain"),
            key=lambda item: drain_priority(item[3]),
            reverse=True,
        )
        for name, remote, agent_state, _pressure, _action in restores:
            client.set_no_schedule(remote, False)
            agent_state["managed_no_schedule"] = False
            schedulable_remaining += 1
            changed = True
            log_event(event="agent_restored", agent=name)
        for name, remote, agent_state, _pressure, _action in drains:
            if not drain_allowed(schedulable_remaining, minimum_schedulable):
                log_event(
                    event="agent_drain_skipped",
                    agent=name,
                    reason="minimum_schedulable_agents",
                    schedulable_agents=schedulable_remaining,
                    minimum_schedulable_agents=minimum_schedulable,
                )
                continue
            client.set_no_schedule(remote, True)
            agent_state["managed_no_schedule"] = True
            schedulable_remaining -= 1
            changed = True
            log_event(event="agent_drained", agent=name)
    return changed


def run_cycle_v2(
    config: dict[str, Any],
    state: dict[str, Any],
    client: HTTPClient,
    apply: bool,
) -> bool:
    """Apply load admission by physical host, including every matching agent ID."""
    remote_agents = client.agents()
    queue = client.queue()
    log_event(
        event="queue_observed",
        pending=len(queue.get("pending", [])),
        running=len(queue.get("running", [])),
        paused=bool(queue.get("paused", False)),
    )

    thresholds = config["thresholds"]
    nodes_state = state.setdefault("nodes", {})
    observations = []
    schedulable_nodes = 0
    for entry in config["nodes"]:
        name = entry["name"]
        configured_names = set(entry["agent_names"])
        agents = [agent for agent in remote_agents if agent.get("name") in configured_names]
        present_names = {agent.get("name") for agent in agents}
        for missing_name in sorted(configured_names - present_names):
            log_event(event="agent_missing", node=name, agent=missing_name)
        if not agents:
            log_event(event="node_missing", node=name)
            continue

        schedulable = [agent for agent in agents if not bool(agent.get("no_schedule"))]
        if schedulable:
            schedulable_nodes += 1
        node_state = nodes_state.setdefault(name, {})
        by_id = {str(agent.get("id")): agent for agent in agents if agent.get("id") is not None}
        previously_managed = {str(agent_id) for agent_id in node_state.get("managed_agent_ids", [])}
        # Forget agents that disappeared or were manually returned to service.
        managed_ids = {
            agent_id
            for agent_id in previously_managed
            if agent_id in by_id and bool(by_id[agent_id].get("no_schedule"))
        }
        node_state["managed_agent_ids"] = sorted(managed_ids)

        try:
            current = node_snapshot(
                fetch_text(entry["metrics_url"]),
                entry.get("filesystem_mountpoint", "/"),
            )
            pressure = evaluate_pressure(
                current,
                snapshot_from_state(node_state),
                thresholds,
            )
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # Telemetry loss is not proof of host pressure. Keep this host's
            # scheduling unchanged and preserve its prior CPU sample.
            log_event(event="metrics_error", node=name, error=str(exc))
            continue

        node_state["previous_snapshot"] = current.__dict__
        node_state["managed_no_schedule"] = bool(managed_ids)
        action = update_hysteresis(
            node_state,
            pressure,
            no_schedule=not bool(schedulable),
            stop_samples=int(config["stop_samples"]),
            resume_samples=int(config["resume_samples"]),
        )
        if (
            managed_ids
            and pressure.recovered
            and node_state["recovery_samples"] >= int(config["resume_samples"])
        ):
            # A replacement agent may be schedulable while IDs previously
            # drained by this controller are still paused. Recover those IDs
            # after the same sustained healthy window.
            action = "restore"
        elif (
            schedulable
            and pressure.breaches
            and node_state["overload_samples"] >= int(config["stop_samples"])
        ):
            action = "drain"
        log_event(
            event="node_observed",
            node=name,
            agent_count=len(agents),
            schedulable_agents=len(schedulable),
            cpu_pct=None if pressure.cpu_pct is None else round(pressure.cpu_pct, 2),
            memory_available_pct=round(pressure.memory_available_pct, 2),
            disk_available_pct=round(pressure.disk_available_pct, 2),
            breaches=list(pressure.breaches),
            overload_samples=node_state["overload_samples"],
            recovery_samples=node_state["recovery_samples"],
            proposed_action=action,
            apply=apply,
        )
        observations.append((name, agents, schedulable, node_state, pressure, action))

    if not apply:
        return False

    changed = False
    minimum_schedulable = int(config.get("minimum_schedulable_nodes", 1))
    # Restore first so healthy capacity comes back before any pressured host is drained.
    for name, agents, schedulable, node_state, _pressure, action in observations:
        if action != "restore":
            continue
        managed_ids = {str(agent_id) for agent_id in node_state.get("managed_agent_ids", [])}
        restored = []
        for agent in agents:
            agent_id = str(agent.get("id"))
            if agent_id in managed_ids and bool(agent.get("no_schedule")):
                client.set_no_schedule(agent, False)
                restored.append(agent_id)
                log_event(event="agent_restored", node=name, agent_id=agent.get("id"), agent=agent.get("name"))
        if restored:
            node_state["managed_agent_ids"] = sorted(managed_ids - set(restored))
            node_state["managed_no_schedule"] = bool(node_state["managed_agent_ids"])
            if not schedulable:
                schedulable_nodes += 1
            changed = True

    drains = sorted(
        (item for item in observations if item[5] == "drain"),
        key=lambda item: drain_priority(item[4]),
        reverse=True,
    )
    for name, agents, schedulable, node_state, _pressure, _action in drains:
        if not schedulable:
            continue
        if schedulable_nodes <= minimum_schedulable:
            log_event(
                event="node_drain_skipped",
                node=name,
                reason="minimum_schedulable_nodes",
                schedulable_nodes=schedulable_nodes,
                minimum_schedulable_nodes=minimum_schedulable,
            )
            continue
        managed_ids = {str(agent_id) for agent_id in node_state.get("managed_agent_ids", [])}
        drained = []
        for agent in schedulable:
            client.set_no_schedule(agent, True)
            agent_id = str(agent.get("id"))
            managed_ids.add(agent_id)
            drained.append(agent_id)
            log_event(event="agent_drained", node=name, agent_id=agent.get("id"), agent=agent.get("name"))
        if drained:
            node_state["managed_agent_ids"] = sorted(managed_ids)
            node_state["managed_no_schedule"] = True
            schedulable_nodes -= 1
            changed = True
    return changed


def run_cycle(
    config: dict[str, Any],
    state: dict[str, Any],
    client: HTTPClient,
    apply: bool,
) -> bool:
    if config["version"] == 1:
        return run_cycle_v1(config, state, client, apply)
    return run_cycle_v2(config, state, client, apply)


def validate_config(config: dict[str, Any]) -> None:
    version = config.get("version")
    if version not in {1, 2}:
        raise ValueError("unsupported controller config version")
    if not config.get("woodpecker_url"):
        raise ValueError("config must declare woodpecker_url")
    if version == 1:
        if not config.get("agents"):
            raise ValueError("version 1 config must declare agents")
        minimum_schedulable = int(config.get("minimum_schedulable_agents", 1))
        if minimum_schedulable < 1 or minimum_schedulable > len(config["agents"]):
            raise ValueError("minimum_schedulable_agents must be between 1 and the agent count")
    else:
        nodes = config.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("version 2 config must declare nodes")
        minimum_schedulable = int(config.get("minimum_schedulable_nodes", 1))
        if minimum_schedulable < 1 or minimum_schedulable > len(nodes):
            raise ValueError("minimum_schedulable_nodes must be between 1 and the node count")
        if any(not isinstance(node, dict) for node in nodes):
            raise ValueError("each node configuration must be an object")
        node_names = [node.get("name") for node in nodes]
        if any(not isinstance(name, str) or not name for name in node_names):
            raise ValueError("each node must have a non-empty name")
        if len(node_names) != len(set(node_names)):
            raise ValueError("node names must be unique")
        if any(not isinstance(node.get("agent_names"), list) for node in nodes):
            raise ValueError("each node must declare agent_names as a list")
        agent_names = [agent for node in nodes for agent in node["agent_names"]]
        if any(not isinstance(name, str) or not name for name in agent_names):
            raise ValueError("each node must declare non-empty agent_names")
        if len(agent_names) != len(set(agent_names)):
            raise ValueError("each Woodpecker agent name must belong to exactly one node")
    required = {
        "cpu_stop_pct",
        "cpu_resume_pct",
        "memory_stop_pct",
        "memory_resume_pct",
        "disk_stop_pct",
        "disk_resume_pct",
    }
    if not required.issubset(config.get("thresholds", {})):
        raise ValueError("controller thresholds are incomplete")
    if config["thresholds"]["cpu_resume_pct"] >= config["thresholds"]["cpu_stop_pct"]:
        raise ValueError("cpu resume threshold must be lower than stop threshold")
    if config["thresholds"]["memory_resume_pct"] <= config["thresholds"]["memory_stop_pct"]:
        raise ValueError("memory resume threshold must be higher than stop threshold")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/woodpecker-load-controller/state.json"))
    parser.add_argument(
        "--token-file",
        type=Path,
        help="read the Woodpecker API token from a root-managed file instead of the environment",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--apply", action="store_true", help="allow no_schedule API changes")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        token = (
            args.token_file.read_text(encoding="utf-8").strip()
            if args.token_file
            else os.getenv("WOODPECKER_TOKEN")
        )
    except OSError as exc:
        print(f"could not read Woodpecker token: {exc}", file=sys.stderr)
        return 2
    if not token:
        print("WOODPECKER_TOKEN or --token-file is required", file=sys.stderr)
        return 2
    try:
        config = load_json(args.config)
        validate_config(config)
        state = load_json(args.state, {"version": config["version"], "agents": {}, "nodes": {}})
        client = HTTPClient(config["woodpecker_url"], token)
        while True:
            try:
                run_cycle(config, state, client, args.apply)
                save_state(args.state, state)
            except (OSError, ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
                log_event(event="controller_error", error=str(exc))
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(float(config.get("interval_seconds", 30)))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"load controller configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
