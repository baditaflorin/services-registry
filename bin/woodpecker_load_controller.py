#!/usr/bin/env python3
"""Drain and restore Woodpecker agents using node-exporter pressure signals.

The controller is dry-run by default. In apply mode it only restores agents
that it previously drained itself; an operator's manual no_schedule setting is
never cleared. State is persisted so hysteresis survives process restarts.

Signals: CPU busy share, available memory, free disk space and, when thresholds
for it are configured, disk I/O pressure (PSI "some" from node-exporter's
node_pressure_io_waiting_seconds_total). A node on slow disks can have idle CPU,
free memory and free space while every fsync takes hundreds of milliseconds; its
agent then looks healthy to the other signals and keeps taking pipelines that
run several times slower than on a quiet node.
"""

from __future__ import annotations

import argparse
import json
import math
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
    disk_available_bytes: int | None = None
    load_per_cpu: float | None = None
    # Cumulative seconds tasks spent stalled on disk (node_pressure_io_waiting_seconds_total)
    # and the wall-clock time of the scrape; None when the node does not export pressure.
    io_waiting_seconds: float | None = None
    taken_at: float | None = None


@dataclass(frozen=True)
class FilesystemSnapshot:
    available_bytes: int
    size_bytes: int
    available_pct: float


@dataclass(frozen=True)
class Pressure:
    cpu_pct: float | None
    memory_available_pct: float
    disk_available_pct: float
    breaches: tuple[str, ...]
    recovered: bool
    critical_storage_breach: bool = False
    backing_storage_available_pct: float | None = None
    backing_storage_available_bytes: int | None = None
    io_pressure_pct: float | None = None


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


def node_snapshot(text: str, mountpoint: str = "/", taken_at: float | None = None) -> NodeSnapshot:
    cpu_idle = 0.0
    cpu_total = 0.0
    memory_available = memory_total = None
    disk_available = disk_total = None
    load1 = None
    cpu_ids = set()
    io_waiting = None
    for name, labels, value in prometheus_samples(text):
        if name == "node_pressure_io_waiting_seconds_total":
            if math.isfinite(value) and value >= 0:
                io_waiting = value
        elif name == "node_cpu_seconds_total":
            if labels.get("cpu") is not None:
                cpu_ids.add(labels["cpu"])
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
        elif name == "node_load1":
            load1 = value
    if cpu_total <= 0 or memory_total in {None, 0} or disk_total in {None, 0}:
        raise ValueError("node-exporter response is missing CPU, memory, or filesystem metrics")
    if memory_available is None or disk_available is None:
        raise ValueError("node-exporter response is missing available memory or filesystem metrics")
    if load1 is not None and (not math.isfinite(load1) or load1 < 0):
        raise ValueError("node-exporter response has invalid 1-minute load value")
    if (
        not math.isfinite(memory_available)
        or not math.isfinite(memory_total)
        or not math.isfinite(disk_available)
        or not math.isfinite(disk_total)
        or memory_available < 0
        or memory_available > memory_total
        or disk_available < 0
        or disk_available > disk_total
    ):
        raise ValueError("node-exporter response has invalid memory or filesystem capacity values")
    return NodeSnapshot(
        cpu_idle,
        cpu_total,
        100.0 * memory_available / memory_total,
        100.0 * disk_available / disk_total,
        int(disk_available),
        load1 / len(cpu_ids) if load1 is not None and cpu_ids else None,
        io_waiting,
        time.time() if taken_at is None else taken_at,
    )


def filesystem_snapshot(
    text: str,
    mountpoint: str,
    device: str,
) -> FilesystemSnapshot:
    """Read one exact filesystem series; ambiguity is an invalid safety signal."""
    matched: dict[str, list[float]] = {
        "node_filesystem_avail_bytes": [],
        "node_filesystem_size_bytes": [],
    }
    for name, labels, value in prometheus_samples(text):
        if (
            name in matched
            and labels.get("mountpoint") == mountpoint
            and labels.get("device") == device
        ):
            matched[name].append(value)

    available = matched["node_filesystem_avail_bytes"]
    size = matched["node_filesystem_size_bytes"]
    if len(available) != 1 or len(size) != 1:
        raise ValueError("node-exporter response is missing or ambiguous for the configured filesystem")
    if (
        not math.isfinite(available[0])
        or not math.isfinite(size[0])
        or available[0] < 0
        or size[0] <= 0
        or available[0] > size[0]
    ):
        raise ValueError("node-exporter response has invalid filesystem capacity values")
    available_bytes = int(available[0])
    size_bytes = int(size[0])
    return FilesystemSnapshot(
        available_bytes=available_bytes,
        size_bytes=size_bytes,
        available_pct=100.0 * available_bytes / size_bytes,
    )


def evaluate_pressure(
    current: NodeSnapshot,
    previous: NodeSnapshot | None,
    thresholds: dict[str, float],
    backing_storage: dict[str, Any] | None = None,
    backing_snapshot: FilesystemSnapshot | None = None,
) -> Pressure:
    cpu_pct = None
    if previous is not None:
        total_delta = current.cpu_total_seconds - previous.cpu_total_seconds
        idle_delta = current.cpu_idle_seconds - previous.cpu_idle_seconds
        if total_delta > 0 and 0 <= idle_delta <= total_delta:
            cpu_pct = 100.0 * (1.0 - idle_delta / total_delta)

    # I/O pressure is the share of wall time tasks spent stalled on disk between the two
    # scrapes. It needs both scrapes to carry the counter; a counter reset (exporter
    # restart) or a node without pressure metrics leaves it unknown, which never drains
    # and never blocks a restore (an unknown signal must not hold an agent out forever).
    io_pct = None
    if (
        previous is not None
        and previous.io_waiting_seconds is not None
        and previous.taken_at is not None
        and current.io_waiting_seconds is not None
        and current.taken_at is not None
    ):
        wall = current.taken_at - previous.taken_at
        waited = current.io_waiting_seconds - previous.io_waiting_seconds
        if wall >= 1.0 and waited >= 0:
            io_pct = min(100.0, 100.0 * waited / wall)
    io_stop = thresholds.get("io_pressure_stop_pct")
    io_resume = thresholds.get("io_pressure_resume_pct")

    breaches = []
    load_stop = thresholds.get("load_per_cpu_stop")
    load_resume = thresholds.get("load_per_cpu_resume")
    if load_stop is not None:
        if current.load_per_cpu is None:
            raise ValueError("node-exporter response is missing normalized load metrics")
        if current.load_per_cpu >= load_stop:
            breaches.append("cpu_load")
        cpu_recovered = current.load_per_cpu <= load_resume
    else:
        if cpu_pct is not None and cpu_pct >= thresholds["cpu_stop_pct"]:
            breaches.append("cpu")
        cpu_recovered = cpu_pct is not None and cpu_pct <= thresholds["cpu_resume_pct"]
    if io_stop is not None and io_pct is not None and io_pct >= io_stop:
        breaches.append("io")
    if current.memory_available_pct <= thresholds["memory_stop_pct"]:
        breaches.append("memory")
    if current.disk_available_pct <= thresholds["disk_stop_pct"]:
        breaches.append("disk")

    critical_storage_breach = False
    backing_available_pct = None
    backing_available_bytes = None
    backing_recovered = True
    if backing_storage is not None:
        if backing_snapshot is None:
            raise ValueError("required backing-storage metrics are unavailable")
        backing_available_pct = backing_snapshot.available_pct
        backing_available_bytes = backing_snapshot.available_bytes
        minimum_free_bytes = backing_storage.get("minimum_free_bytes")
        minimum_bytes_breached = False
        minimum_bytes_recovered = True
        if minimum_free_bytes is not None:
            if backing_available_bytes is None:
                raise ValueError("required backing-storage byte metrics are unavailable")
            minimum_bytes_breached = backing_available_bytes <= int(minimum_free_bytes)
            minimum_bytes_recovered = backing_available_bytes >= int(minimum_free_bytes)
        critical_storage_breach = (
            backing_available_pct <= float(backing_storage["disk_stop_pct"])
            or minimum_bytes_breached
        )
        if critical_storage_breach:
            breaches.append("backing_storage")
        backing_recovered = (
            backing_available_pct >= float(backing_storage["disk_resume_pct"])
            and minimum_bytes_recovered
        )

    recovered = (
        cpu_recovered
        and current.memory_available_pct >= thresholds["memory_resume_pct"]
        and current.disk_available_pct >= thresholds["disk_resume_pct"]
        and (io_resume is None or io_pct is None or io_pct <= io_resume)
        and backing_recovered
    )
    return Pressure(
        cpu_pct,
        current.memory_available_pct,
        current.disk_available_pct,
        tuple(breaches),
        recovered,
        critical_storage_breach,
        backing_available_pct,
        backing_available_bytes,
        io_pct,
    )


def validate_graded(node: dict[str, Any], thresholds: dict[str, Any]) -> None:
    """A node's optional graded admission: its "full" levels must sit on the healthy side of the
    stop thresholds, or the linear share between them would be undefined."""
    where = f"node {node.get('name')!r} graded"
    graded = node["graded"]
    if not isinstance(graded, dict):
        raise ValueError(f"{where} must be an object")
    unknown = set(graded) - GRADED_KEYS
    if unknown:
        raise ValueError(f"{where} has unknown keys: {sorted(unknown)}")
    for key in ("min_open_slots", "open_samples", "close_samples"):
        if key in graded:
            value = graded[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{where} {key} must be a positive integer")
    for key in ("load_per_cpu_full", "memory_full_pct", "io_full_pct"):
        if key in graded:
            value = graded[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{where} {key} must be a non-negative number")
    effective = thresholds_for_node(thresholds, node)
    load_stop = effective.get("load_per_cpu_stop")
    if load_stop is not None and float(graded.get("load_per_cpu_full", effective["load_per_cpu_resume"])) >= load_stop:
        raise ValueError(f"{where} load_per_cpu_full must be lower than load_per_cpu_stop")
    if float(graded.get("memory_full_pct", effective["memory_resume_pct"])) <= effective["memory_stop_pct"]:
        raise ValueError(f"{where} memory_full_pct must be higher than memory_stop_pct")
    io_stop = effective.get("io_pressure_stop_pct")
    if io_stop is not None and float(graded.get("io_full_pct", effective.get("io_pressure_resume_pct", 0))) >= io_stop:
        raise ValueError(f"{where} io_full_pct must be lower than io_pressure_stop_pct")


IO_THRESHOLD_KEYS = ("io_pressure_stop_pct", "io_pressure_resume_pct")


def thresholds_for_node(
    thresholds: dict[str, float],
    node: dict[str, Any],
) -> dict[str, float]:
    """The global thresholds with one node's (or v1 agent's) own limits on top: an optional
    disk-space pair (disk_thresholds) and an optional I/O pressure pair. Both are properties
    of a node's disks, not of the fleet."""
    effective = dict(thresholds)
    disk_thresholds = node.get("disk_thresholds")
    if disk_thresholds is not None:
        effective["disk_stop_pct"] = float(disk_thresholds["stop_pct"])
        effective["disk_resume_pct"] = float(disk_thresholds["resume_pct"])
    for key in IO_THRESHOLD_KEYS:
        if key in node:
            effective[key] = node[key]
    return effective


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


GRADED_KEYS = {
    "min_open_slots",
    "load_per_cpu_full",
    "memory_full_pct",
    "io_full_pct",
    "open_samples",
    "close_samples",
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def graded_headroom(
    current: NodeSnapshot,
    pressure: Pressure,
    thresholds: dict[str, float],
    graded: dict[str, Any],
) -> float:
    """How much of a host's slots may be open, 0.0 (none) to 1.0 (all).

    Each signal is linear between its "full" level (everything open) and the node's stop
    threshold (nothing open); the scarcest signal decides. A signal that is unknown or has no
    threshold does not limit the host, the same rule the hard drain follows.
    """
    shares = []
    load_stop = thresholds.get("load_per_cpu_stop")
    if load_stop is not None and current.load_per_cpu is not None:
        full = float(graded.get("load_per_cpu_full", thresholds["load_per_cpu_resume"]))
        shares.append(_clamp01((load_stop - current.load_per_cpu) / (load_stop - full)))
    elif pressure.cpu_pct is not None:
        stop = thresholds["cpu_stop_pct"]
        full = float(graded.get("cpu_full_pct", thresholds["cpu_resume_pct"]))
        shares.append(_clamp01((stop - pressure.cpu_pct) / (stop - full)))
    memory_stop = thresholds["memory_stop_pct"]
    memory_full = float(graded.get("memory_full_pct", thresholds["memory_resume_pct"]))
    shares.append(_clamp01((pressure.memory_available_pct - memory_stop) / (memory_full - memory_stop)))
    io_stop = thresholds.get("io_pressure_stop_pct")
    if io_stop is not None and pressure.io_pressure_pct is not None:
        io_full = float(graded.get("io_full_pct", thresholds.get("io_pressure_resume_pct", 0)))
        shares.append(_clamp01((io_stop - pressure.io_pressure_pct) / (io_stop - io_full)))
    return min(shares)


def agent_slots(agent: dict[str, Any]) -> int:
    capacity = agent.get("capacity")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        return 1
    return capacity


def graded_step(
    node_state: dict[str, Any],
    ordered_agents: list[dict[str, Any]],
    fresh_ids: set[str],
    managed_ids: set[str],
    headroom: float,
    graded: dict[str, Any],
    hard_breach: bool,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    """Decide at most one admission step for a graded host: close its last open agent when it is
    over its share, or reopen the first agent this controller closed when it has room.

    Closing needs close_samples consecutive cycles over share, opening needs open_samples under
    it (and no hard breach), so a host near a boundary does not flap. Agents are used in config
    order: the first listed is the last to close. Only IDs the controller closed are reopened,
    so an operator's manual hold survives. Returns (action, agent, facts for the log)."""
    min_open = int(graded.get("min_open_slots", 1))
    usable = [agent for agent in ordered_agents if str(agent.get("id")) in fresh_ids]
    total = sum(agent_slots(agent) for agent in usable)
    desired = min(total, max(min_open, int(round(headroom * total))))
    open_agents = [agent for agent in usable if not bool(agent.get("no_schedule"))]
    open_slots = sum(agent_slots(agent) for agent in open_agents)
    facts = {"headroom": round(headroom, 3), "desired_slots": desired, "open_slots": open_slots, "total_slots": total}

    close_candidate = open_agents[-1] if len(open_agents) > 1 else None
    open_candidate = next(
        (
            agent
            for agent in usable
            if bool(agent.get("no_schedule")) and str(agent.get("id")) in managed_ids
        ),
        None,
    )
    want_close = (
        close_candidate is not None
        and open_slots - agent_slots(close_candidate) >= min_open
        and open_slots - agent_slots(close_candidate) / 2 >= desired
    )
    want_open = (
        not want_close
        and not hard_breach
        and open_candidate is not None
        and open_slots + agent_slots(open_candidate) / 2 <= desired
    )
    node_state["graded_close_samples"] = int(node_state.get("graded_close_samples", 0)) + 1 if want_close else 0
    node_state["graded_open_samples"] = int(node_state.get("graded_open_samples", 0)) + 1 if want_open else 0
    if want_close and node_state["graded_close_samples"] >= int(graded.get("close_samples", 2)):
        node_state["graded_close_samples"] = 0
        return "close", close_candidate, facts
    if want_open and node_state["graded_open_samples"] >= int(graded.get("open_samples", 6)):
        node_state["graded_open_samples"] = 0
        return "open", open_candidate, facts
    return None, None, facts


class HTTPClient:
    AGENTS_PER_PAGE = 50

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
        agents: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self._request(
                f"/api/agents?page={page}&perPage={self.AGENTS_PER_PAGE}"
            )
            if not isinstance(result, list):
                raise ValueError("Woodpecker agents response must be a JSON array")
            agents.extend(result)
            if len(result) < self.AGENTS_PER_PAGE:
                return agents
            page += 1

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


LEGACY_SNAPSHOT_FIELDS = (
    "cpu_idle_seconds",
    "cpu_total_seconds",
    "memory_available_pct",
    "disk_available_pct",
    "disk_available_bytes",
    "load_per_cpu",
)


def snapshot_to_state(agent_state: dict[str, Any], current: NodeSnapshot) -> None:
    """Persist the scrape. The I/O counter goes under its own key so the previous_snapshot
    object keeps exactly the fields older controller versions read: rolling the script back
    must not make it fail on a state file this version wrote."""
    agent_state["previous_snapshot"] = {name: getattr(current, name) for name in LEGACY_SNAPSHOT_FIELDS}
    if current.io_waiting_seconds is not None and current.taken_at is not None:
        agent_state["previous_io"] = {
            "waiting_seconds": current.io_waiting_seconds,
            "taken_at": current.taken_at,
        }
    else:
        agent_state.pop("previous_io", None)


def snapshot_from_state(agent_state: dict[str, Any]) -> NodeSnapshot | None:
    raw = agent_state.get("previous_snapshot")
    if not isinstance(raw, dict):
        return None
    fields = {name: raw[name] for name in LEGACY_SNAPSHOT_FIELDS if name in raw}
    io = agent_state.get("previous_io")
    if isinstance(io, dict):
        fields["io_waiting_seconds"] = io.get("waiting_seconds")
        fields["taken_at"] = io.get("taken_at")
    return NodeSnapshot(**fields)


def log_event(**fields: Any) -> None:
    fields["timestamp"] = int(time.time())
    print(json.dumps(fields, sort_keys=True), flush=True)


def agent_readiness(
    agent: dict[str, Any],
    now: float,
    stale_after_seconds: int,
    controller_managed: bool = False,
) -> dict[str, Any]:
    """Explain API scheduling state separately from a fresh worker heartbeat."""
    raw_contact = agent.get("last_contact")
    contact_age = None
    heartbeat_state = "unknown"
    if raw_contact is not None:
        try:
            contact = float(raw_contact)
        except (TypeError, ValueError):
            heartbeat_state = "invalid"
        else:
            if not math.isfinite(contact) or contact <= 0:
                heartbeat_state = "invalid"
            else:
                contact_age = round(now - contact, 1)
                if contact_age < 0:
                    heartbeat_state = "future"
                elif contact_age > stale_after_seconds:
                    heartbeat_state = "stale"
                else:
                    heartbeat_state = "fresh"

    no_schedule = bool(agent.get("no_schedule"))
    reasons = []
    if no_schedule:
        reasons.append(
            "controller_drained" if controller_managed else "manual_or_external_no_schedule"
        )
    if heartbeat_state != "fresh":
        reasons.append(f"heartbeat_{heartbeat_state}")
    api_enabled = not no_schedule
    heartbeat_fresh = heartbeat_state == "fresh"
    capacity = agent.get("capacity")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        capacity = None
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "api_enabled": api_enabled,
        "heartbeat_state": heartbeat_state,
        "last_contact_age_seconds": contact_age,
        "capacity": capacity,
        "ready_to_poll": api_enabled and heartbeat_fresh,
        "controller_managed": controller_managed,
        "reasons": reasons,
    }


def log_agent_readiness(
    node: str,
    agents: list[dict[str, Any]],
    managed_ids: set[str],
    stale_after_seconds: int,
) -> None:
    now = time.time()
    inventory = [
        agent_readiness(
            agent,
            now,
            stale_after_seconds,
            str(agent.get("id")) in managed_ids,
        )
        for agent in agents
    ]
    log_event(
        event="agent_readiness",
        node=node,
        registered_agents=len(inventory),
        api_enabled_agents=sum(item["api_enabled"] for item in inventory),
        fresh_heartbeats=sum(item["heartbeat_state"] == "fresh" for item in inventory),
        ready_to_poll_agents=sum(item["ready_to_poll"] for item in inventory),
        ready_capacity=sum(item["capacity"] or 0 for item in inventory if item["ready_to_poll"]),
        stale_after_seconds=stale_after_seconds,
        agents=inventory,
    )


def run_cycle_v1(
    config: dict[str, Any],
    state: dict[str, Any],
    client: HTTPClient,
    apply: bool,
) -> bool:
    remote_agents: dict[str, list[dict[str, Any]]] = {}
    for agent in client.agents():
        name = agent.get("name")
        if isinstance(name, str):
            remote_agents.setdefault(name, []).append(agent)
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
        for name in configured_names
        if any(not bool(agent.get("no_schedule")) for agent in remote_agents.get(name, []))
    )
    missing = sorted(configured_names - remote_agents.keys())
    for name in missing:
        log_event(event="agent_missing", agent=name)

    observations = []
    for entry in config["agents"]:
        name = entry["name"]
        agents = remote_agents.get(name, [])
        if not agents:
            continue
        agent_state = state.setdefault("agents", {}).setdefault(name, {})
        by_id = {str(agent.get("id")): agent for agent in agents if agent.get("id") is not None}
        managed_ids = {
            str(agent_id)
            for agent_id in agent_state.get("managed_agent_ids", [])
            if str(agent_id) in by_id and bool(by_id[str(agent_id)].get("no_schedule"))
        }
        agent_state["managed_agent_ids"] = sorted(managed_ids)
        schedulable = [agent for agent in agents if not bool(agent.get("no_schedule"))]
        log_agent_readiness(
            name,
            agents,
            managed_ids,
            int(config.get("agent_stale_after_seconds", 120)),
        )
        backing_config = entry.get("backing_storage")
        try:
            current = node_snapshot(
                fetch_text(entry["metrics_url"]),
                entry.get("filesystem_mountpoint", "/"),
            )
            backing_snapshot = None
            if backing_config is not None:
                backing_snapshot = filesystem_snapshot(
                    fetch_text(backing_config["metrics_url"]),
                    backing_config["filesystem_mountpoint"],
                    backing_config["filesystem_device"],
                )
            pressure = evaluate_pressure(
                current,
                snapshot_from_state(agent_state),
                thresholds_for_node(thresholds, entry),
                backing_config,
                backing_snapshot,
            )
        except (OSError, ValueError, urllib.error.URLError) as exc:
            fail_closed = bool(
                backing_config is not None
                or entry.get("fail_closed_on_metrics_error", False)
            )
            log_event(
                event="metrics_error",
                agent=name,
                fail_closed=fail_closed,
                error=str(exc),
            )
            if fail_closed:
                agent_state["previous_snapshot"] = None
                agent_state["overload_samples"] = 0
                agent_state["recovery_samples"] = 0
                agent_state["managed_no_schedule"] = bool(managed_ids)
                observations.append(
                    (name, agents, schedulable, agent_state, None, "telemetry_quarantine")
                )
            continue

        snapshot_to_state(agent_state, current)
        agent_state["managed_no_schedule"] = bool(managed_ids)
        action = update_hysteresis(
            agent_state,
            pressure,
            not bool(schedulable),
            int(config["stop_samples"]),
            int(config["resume_samples"]),
        )
        if (
            managed_ids
            and pressure.recovered
            and agent_state["recovery_samples"] >= int(config["resume_samples"])
        ):
            action = "restore"
        elif pressure.critical_storage_breach:
            action = "critical_drain"
        log_event(
            event="agent_observed",
            agent=name,
            agent_count=len(agents),
            schedulable_agents=len(schedulable),
            cpu_pct=None if pressure.cpu_pct is None else round(pressure.cpu_pct, 2),
            load_per_cpu=None if current.load_per_cpu is None else round(current.load_per_cpu, 3),
            io_pressure_pct=None if pressure.io_pressure_pct is None else round(pressure.io_pressure_pct, 2),
            memory_available_pct=round(pressure.memory_available_pct, 2),
            disk_available_pct=round(pressure.disk_available_pct, 2),
            backing_storage_available_pct=pressure.backing_storage_available_pct,
            backing_storage_available_bytes=pressure.backing_storage_available_bytes,
            breaches=list(pressure.breaches),
            no_schedule=not bool(schedulable),
            overload_samples=agent_state["overload_samples"],
            recovery_samples=agent_state["recovery_samples"],
            proposed_action=action,
            apply=apply,
        )
        observations.append((name, agents, schedulable, agent_state, pressure, action))

    if not apply:
        return False

    # Restore only IDs this controller actually drained; IDs paused by an
    # operator are never inferred from a group-level boolean.
    restores = [item for item in observations if item[5] == "restore"]
    for name, agents, schedulable, agent_state, _pressure, _action in restores:
        managed_ids = {str(agent_id) for agent_id in agent_state.get("managed_agent_ids", [])}
        restored = []
        for agent in agents:
            agent_id = str(agent.get("id"))
            if agent_id in managed_ids and bool(agent.get("no_schedule")):
                client.set_no_schedule(agent, False)
                restored.append(agent_id)
                log_event(event="agent_restored", agent=name, agent_id=agent.get("id"))
        if restored:
            agent_state["managed_agent_ids"] = sorted(managed_ids - set(restored))
            agent_state["managed_no_schedule"] = bool(agent_state["managed_agent_ids"])
            if not schedulable:
                schedulable_remaining += 1
            changed = True

    minimum_schedulable = int(config.get("minimum_schedulable_agents", 1))
    drains = sorted(
        (
            item
            for item in observations
            if item[5] in {"drain", "critical_drain", "telemetry_quarantine"}
        ),
        key=lambda item: (
            item[5] not in {"critical_drain", "telemetry_quarantine"},
            drain_priority(item[4]) if item[4] is not None else (0, 0, 0, 0),
        ),
        reverse=True,
    )
    for name, agents, schedulable, agent_state, _pressure, action in drains:
        critical = action in {"critical_drain", "telemetry_quarantine"}
        if not schedulable:
            continue
        if not critical and not drain_allowed(schedulable_remaining, minimum_schedulable):
            log_event(
                event="agent_drain_skipped",
                agent=name,
                reason="minimum_schedulable_agents",
                schedulable_agents=schedulable_remaining,
                minimum_schedulable_agents=minimum_schedulable,
            )
            continue
        managed_ids = {str(agent_id) for agent_id in agent_state.get("managed_agent_ids", [])}
        drained = []
        for agent in schedulable:
            client.set_no_schedule(agent, True)
            agent_id = str(agent.get("id"))
            managed_ids.add(agent_id)
            drained.append(agent_id)
            log_event(event="agent_drained", agent=name, agent_id=agent.get("id"), reason=action)
        if drained:
            agent_state["managed_agent_ids"] = sorted(managed_ids)
            agent_state["managed_no_schedule"] = True
            schedulable_remaining -= 1
            changed = True
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
    graded_inputs: dict[str, tuple[dict[str, Any], NodeSnapshot, Pressure]] = {}
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
        log_agent_readiness(
            name,
            agents,
            managed_ids,
            int(config.get("agent_stale_after_seconds", 120)),
        )

        backing_config = entry.get("backing_storage")
        try:
            current = node_snapshot(
                fetch_text(entry["metrics_url"]),
                entry.get("filesystem_mountpoint", "/"),
            )
            backing_snapshot = None
            if backing_config is not None:
                backing_snapshot = filesystem_snapshot(
                    fetch_text(backing_config["metrics_url"]),
                    backing_config["filesystem_mountpoint"],
                    backing_config["filesystem_device"],
                )
            pressure = evaluate_pressure(
                current,
                snapshot_from_state(node_state),
                thresholds_for_node(thresholds, entry),
                backing_config,
                backing_snapshot,
            )
        except (OSError, ValueError, urllib.error.URLError) as exc:
            fail_closed = bool(
                backing_config is not None
                or entry.get("fail_closed_on_metrics_error", False)
            )
            log_event(
                event="metrics_error",
                node=name,
                fail_closed=fail_closed,
                error=str(exc),
            )
            if fail_closed:
                node_state["previous_snapshot"] = None
                node_state["overload_samples"] = 0
                node_state["recovery_samples"] = 0
                node_state["managed_no_schedule"] = bool(managed_ids)
                observations.append(
                    (name, agents, schedulable, node_state, None, "telemetry_quarantine")
                )
            continue

        snapshot_to_state(node_state, current)
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
            # Restore only IDs this controller managed after all required
            # guest and backing-storage signals recover for the full window.
            action = "restore"
        elif pressure.critical_storage_breach:
            action = "critical_drain"
        if entry.get("graded") is not None:
            # A graded host reopens one agent at a time (see graded_step), not all at once.
            if action == "restore":
                action = None
            graded_inputs[name] = (entry, current, pressure)
        log_event(
            event="node_observed",
            node=name,
            agent_count=len(agents),
            schedulable_agents=len(schedulable),
            cpu_pct=None if pressure.cpu_pct is None else round(pressure.cpu_pct, 2),
            load_per_cpu=None if current.load_per_cpu is None else round(current.load_per_cpu, 3),
            io_pressure_pct=None if pressure.io_pressure_pct is None else round(pressure.io_pressure_pct, 2),
            memory_available_pct=round(pressure.memory_available_pct, 2),
            disk_available_pct=round(pressure.disk_available_pct, 2),
            backing_storage_available_pct=pressure.backing_storage_available_pct,
            backing_storage_available_bytes=pressure.backing_storage_available_bytes,
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
    # Restore healthy capacity before drains; only controller-managed IDs can be
    # restored, so a manual no_schedule hold survives recovery.
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

    critical_drains = sorted(
        (
            item
            for item in observations
            if item[5] in {"critical_drain", "telemetry_quarantine"}
        ),
        key=lambda item: (
            item[5] != "telemetry_quarantine",
            drain_priority(item[4]) if item[4] is not None else (0, 0, 0, 0),
        ),
        reverse=True,
    )
    normal_drains = sorted(
        (item for item in observations if item[5] == "drain"),
        key=lambda item: drain_priority(item[4]),
        reverse=True,
    )
    for name, agents, schedulable, node_state, _pressure, action in critical_drains + normal_drains:
        if not schedulable:
            continue
        critical = action in {"critical_drain", "telemetry_quarantine"}
        if not critical and schedulable_nodes <= minimum_schedulable:
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
            log_event(
                event="agent_drained",
                node=name,
                agent_id=agent.get("id"),
                agent=agent.get("name"),
                reason=action,
            )
        if drained:
            node_state["managed_agent_ids"] = sorted(managed_ids)
            node_state["managed_no_schedule"] = True
            schedulable_nodes -= 1
            changed = True

    # Graded hosts that were not drained outright: open or close one agent to follow headroom.
    stale_after = int(config.get("agent_stale_after_seconds", 120))
    now = time.time()
    for name, agents, _schedulable, node_state, pressure, action in observations:
        if name not in graded_inputs or pressure is None:
            continue
        if action in {"drain", "critical_drain", "telemetry_quarantine"}:
            continue
        entry, current, _ = graded_inputs[name]
        graded = entry["graded"]
        order = {agent_name: index for index, agent_name in enumerate(entry["agent_names"])}
        ordered = sorted(agents, key=lambda agent: (order.get(agent.get("name"), len(order)), int(agent.get("id") or 0)))
        fresh_ids = {
            str(agent.get("id"))
            for agent in agents
            if agent_readiness(agent, now, stale_after)["heartbeat_state"] == "fresh"
        }
        managed_ids = {str(agent_id) for agent_id in node_state.get("managed_agent_ids", [])}
        headroom = graded_headroom(current, pressure, thresholds_for_node(thresholds, entry), graded)
        step, agent, facts = graded_step(
            node_state, ordered, fresh_ids, managed_ids, headroom, graded, bool(pressure.breaches)
        )
        log_event(event="node_graded", node=name, proposed_action=step, **facts)
        if step is None or agent is None:
            continue
        agent_id = str(agent.get("id"))
        if step == "close":
            client.set_no_schedule(agent, True)
            managed_ids.add(agent_id)
        else:
            client.set_no_schedule(agent, False)
            managed_ids.discard(agent_id)
        node_state["managed_agent_ids"] = sorted(managed_ids)
        node_state["managed_no_schedule"] = bool(managed_ids)
        log_event(
            event="agent_graded_closed" if step == "close" else "agent_graded_opened",
            node=name,
            agent_id=agent.get("id"),
            agent=agent.get("name"),
            **facts,
        )
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


def validate_backing_storage(entry: dict[str, Any]) -> None:
    backing = entry.get("backing_storage")
    if backing is None:
        return
    if not isinstance(backing, dict):
        raise ValueError("backing_storage must be an object")
    for key in ("metrics_url", "filesystem_mountpoint", "filesystem_device"):
        if not isinstance(backing.get(key), str) or not backing[key]:
            raise ValueError(f"backing_storage must declare {key}")
    for key in ("disk_stop_pct", "disk_resume_pct"):
        value = backing.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
            raise ValueError(f"backing_storage {key} must be between 0 and 100")
    if backing["disk_resume_pct"] <= backing["disk_stop_pct"]:
        raise ValueError("backing_storage resume percentage must be higher than stop percentage")
    minimum_free_bytes = backing.get("minimum_free_bytes")
    if minimum_free_bytes is not None and (
        isinstance(minimum_free_bytes, bool)
        or not isinstance(minimum_free_bytes, int)
        or minimum_free_bytes <= 0
    ):
        raise ValueError("backing_storage minimum_free_bytes must be a positive integer when set")


def validate_io_thresholds(source: dict[str, Any], where: str) -> None:
    """The I/O pressure limits are optional, but a stop without a resume (or the reverse)
    would drain without a way back, and a resume at or above the stop would flap."""
    present = [key for key in IO_THRESHOLD_KEYS if key in source]
    if not present:
        return
    if len(present) != len(IO_THRESHOLD_KEYS):
        raise ValueError(f"{where}: io_pressure_stop_pct and io_pressure_resume_pct must be set together")
    stop, resume = (source[key] for key in IO_THRESHOLD_KEYS)
    for value in (stop, resume):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{where}: I/O pressure thresholds must be finite numbers")
    if not 0 < resume < stop <= 100:
        raise ValueError(f"{where}: I/O pressure thresholds need 0 < resume < stop <= 100")


def validate_config(config: dict[str, Any]) -> None:
    version = config.get("version")
    if version not in {1, 2}:
        raise ValueError("unsupported controller config version")
    if not config.get("woodpecker_url"):
        raise ValueError("config must declare woodpecker_url")
    stale_after = config.get("agent_stale_after_seconds", 120)
    if isinstance(stale_after, bool) or not isinstance(stale_after, int) or stale_after <= 0:
        raise ValueError("agent_stale_after_seconds must be a positive integer")
    if version == 1:
        agents = config.get("agents")
        if not isinstance(agents, list) or not agents:
            raise ValueError("version 1 config must declare agents")
        if any(not isinstance(entry, dict) for entry in agents):
            raise ValueError("each version 1 agent configuration must be an object")
        agent_names = [entry.get("name") for entry in agents]
        if any(not isinstance(name, str) or not name for name in agent_names):
            raise ValueError("each version 1 agent must declare a name")
        if len(agent_names) != len(set(agent_names)):
            raise ValueError("version 1 agent names must be unique; duplicate live IDs are grouped by name")
        for entry in agents:
            validate_backing_storage(entry)
        minimum_schedulable = int(config.get("minimum_schedulable_agents", 1))
        if minimum_schedulable < 1 or minimum_schedulable > len(agents):
            raise ValueError("minimum_schedulable_agents must be between 1 and the agent count")
    else:
        nodes = config.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("version 2 config must declare nodes")
        minimum_schedulable = int(config.get("minimum_schedulable_nodes", 1))
        if minimum_schedulable < 0 or minimum_schedulable > len(nodes):
            raise ValueError("minimum_schedulable_nodes must be between 0 and the node count")
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
        for node in nodes:
            validate_backing_storage(node)
            disk_thresholds = node.get("disk_thresholds")
            if disk_thresholds is not None:
                if not isinstance(disk_thresholds, dict):
                    raise ValueError("node disk_thresholds must be an object")
                if set(disk_thresholds) != {"stop_pct", "resume_pct"}:
                    raise ValueError("node disk_thresholds must declare stop_pct and resume_pct only")
                for key, value in disk_thresholds.items():
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or not 0 <= value <= 100
                    ):
                        raise ValueError(f"node disk_thresholds {key} must be between 0 and 100")
                if disk_thresholds["resume_pct"] <= disk_thresholds["stop_pct"]:
                    raise ValueError("node disk resume percentage must be higher than stop percentage")
            if node.get("graded") is not None:
                validate_graded(node, config["thresholds"])
            if (
                "fail_closed_on_metrics_error" in node
                and not isinstance(node["fail_closed_on_metrics_error"], bool)
            ):
                raise ValueError("fail_closed_on_metrics_error must be a boolean")
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
    load_stop = config["thresholds"].get("load_per_cpu_stop")
    load_resume = config["thresholds"].get("load_per_cpu_resume")
    if (load_stop is None) != (load_resume is None):
        raise ValueError("normalized load stop and resume thresholds must be configured together")
    if load_stop is not None:
        if (
            isinstance(load_stop, bool)
            or not isinstance(load_stop, (int, float))
            or not math.isfinite(load_stop)
            or load_stop <= 0
        ):
            raise ValueError("load_per_cpu_stop must be a positive number")
        if (
            isinstance(load_resume, bool)
            or not isinstance(load_resume, (int, float))
            or not math.isfinite(load_resume)
            or load_resume < 0
            or load_resume >= load_stop
        ):
            raise ValueError("load_per_cpu_resume must be non-negative and lower than stop")
    if config["thresholds"]["memory_resume_pct"] <= config["thresholds"]["memory_stop_pct"]:
        raise ValueError("memory resume threshold must be higher than stop threshold")
    if config["thresholds"]["disk_resume_pct"] <= config["thresholds"]["disk_stop_pct"]:
        raise ValueError("disk resume threshold must be higher than stop threshold")
    validate_io_thresholds(config["thresholds"], "controller thresholds")
    for entry in config["nodes"] if version == 2 else config["agents"]:
        validate_io_thresholds(entry, f"{entry.get('name')!r}")


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
