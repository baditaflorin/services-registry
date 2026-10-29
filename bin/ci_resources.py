#!/usr/bin/env python3
"""Derive per-repo CI build resource needs from Prometheus into ci-resources.json.

Source: woodpecker_build_step_memory_peak_bytes_24h (one series per step-container
run, peak memory of that run in bytes) and woodpecker_build_step_cpu_seconds_24h.
The file is derived: never hand-edit it, rerun this script.

    ssh -f -N -L 19090:127.0.0.1:9090 0mcp_monitoring
    python3 bin/ci_resources.py --write          # query + write ci-resources.json
    python3 bin/ci_resources.py                  # print a summary, write nothing
    python3 bin/ci_resources.py --check          # offline validation of the file
    python3 bin/ci_resources.py --input m.json   # offline, saved instant-query result

Stdlib only, python3.9+. Percentile = nearest-rank. Output is deterministic
(sorted keys, 2-space indent, trailing newline) except `generated_at`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "ci-resources.json"
SERVICES_JSON = ROOT / "services.json"

MEM_METRIC = "woodpecker_build_step_memory_peak_bytes_24h"
CPU_METRIC = "woodpecker_build_step_cpu_seconds_24h"
SOURCE = "prometheus " + MEM_METRIC
RUN_LABELS = "repo, step_name, pipeline_number, step_number, host, control_plane"

MIB = 1024 * 1024
GIB = 1024 * MIB
# (class, upper bound inclusive); "l" is everything above the last bound.
CLASS_BOUNDS = (("xs", 256 * MIB), ("s", 512 * MIB), ("m", 4 * GIB))
CLASSES = {"xs": "<=256 MiB", "s": "<=512 MiB", "m": "<=4 GiB", "l": ">4 GiB"}
OOM_SUSPECT_BYTES = int(4.9 * GIB)  # builders cap a container at 5 GiB; a kill reports a truncated peak
MAX_STEPS = 12
LOW_CONFIDENCE_RUNS = 20
VERSION = 1

# Strings that must never appear in a derived file that is committed to a public repo.
SECRET_RE = re.compile(
    r"(?i)(://[^/\s:@]+:[^/\s@]+@|\bBearer\s+[A-Za-z0-9._-]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|\bgh[pousr]_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b"
    r"|(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S{6,})"
)


def percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 1..100) of a non-empty list."""
    if not values:
        raise ValueError("percentile of empty list")
    s = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[rank - 1]


def mem_class(p95_bytes: int) -> str:
    for name, bound in CLASS_BOUNDS:
        if p95_bytes <= bound:
            return name
    return "l"


# ---------------------------------------------------------------- Prometheus

def prom_query(prom_url: str, query: str, timeout: int = 180) -> List[dict]:
    url = prom_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - operator-supplied URL
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError("prometheus query failed: %s" % body.get("error", "unknown"))
    return body["data"]["result"]


def per_run_query(metric: str, window: str) -> str:
    return "max by (%s) (max_over_time(%s[%s]))" % (RUN_LABELS, metric, window)


def last_seen_query(window: str) -> str:
    return "max by (repo) (max_over_time(timestamp(%s)[%s:15m]))" % (MEM_METRIC, window)


def fetch(prom_url: str, window: str) -> dict:
    return {
        "memory": prom_query(prom_url, per_run_query(MEM_METRIC, window)),
        "cpu": prom_query(prom_url, per_run_query(CPU_METRIC, window)),
        "last_seen": prom_query(prom_url, last_seen_query(window)),
    }


def load_input(path: Path) -> dict:
    """Accept {"memory": [...], "cpu": [...], "last_seen": [...]}, a raw instant-query
    response ({"data": {"result": [...]}}) or a bare result list (memory only)."""
    raw = json.loads(path.read_text())
    if isinstance(raw, list):
        return {"memory": raw, "cpu": [], "last_seen": []}
    if "data" in raw:
        return {"memory": raw["data"]["result"], "cpu": [], "last_seen": []}
    return {"memory": raw.get("memory", []), "cpu": raw.get("cpu", []), "last_seen": raw.get("last_seen", [])}


# ------------------------------------------------------------------- derive

def repo_name(repo_url: str) -> str:
    return repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def id_map(services: List[dict]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for e in services:
        url = e.get("repo_url")
        if url and e.get("id"):
            out.setdefault(repo_name(url), []).append(e["id"])
    return {k: sorted(set(v)) for k, v in out.items()}


def _run_key(m: dict) -> Tuple[str, ...]:
    return tuple(m.get(k, "") for k in ("repo", "step_name", "pipeline_number", "step_number", "host", "control_plane"))


def _samples(result: List[dict]) -> Dict[Tuple[str, ...], float]:
    out: Dict[Tuple[str, ...], float] = {}
    for r in result:
        try:
            v = float(r["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if math.isnan(v) or v < 0:
            continue
        k = _run_key(r["metric"])
        out[k] = max(v, out.get(k, v))
    return out


def derive(data: dict, services: List[dict], now: dt.datetime, window_days: int = 7) -> dict:
    """Build the ci-resources document. Zero-byte memory samples are 'no reading'
    (a container that exited before the first sample) and are ignored."""
    mem = {k: v for k, v in _samples(data["memory"]).items() if v > 0}
    cpu = _samples(data.get("cpu", []))
    seen_ts: Dict[str, float] = {}
    for r in data.get("last_seen", []):
        try:
            seen_ts[r["metric"]["repo"].rsplit("/", 1)[-1]] = float(r["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    ids = id_map(services)

    per_repo: Dict[str, Dict[str, Any]] = {}
    for key, v in mem.items():
        repo, step, pipeline, _num, _host, plane = key
        name = repo.rsplit("/", 1)[-1]
        r = per_repo.setdefault(name, {"pipelines": set(), "steps": {}})
        r["pipelines"].add((plane, pipeline))
        r["steps"].setdefault(step, []).append((v, cpu.get(key)))

    repos: Dict[str, Any] = {}
    for name, r in per_repo.items():
        steps: Dict[str, Any] = {}
        for step, rows in r["steps"].items():
            mems = [m for m, _ in rows]
            cpus = [c for _, c in rows if c is not None]
            steps[step] = {
                "runs": len(rows),
                "mem_peak_p95_bytes": int(percentile(mems, 95)),
                "mem_peak_max_bytes": int(max(mems)),
                "cpu_seconds_p95": round(percentile(cpus, 95), 2) if cpus else None,
                "oom_suspect": max(mems) >= OOM_SUSPECT_BYTES,
            }
        # Repo-level numbers come from every step, before the list is capped.
        p95 = max(s["mem_peak_p95_bytes"] for s in steps.values())
        heaviest = sorted(steps, key=lambda s: (-steps[s]["mem_peak_p95_bytes"], s))[:MAX_STEPS]
        runs = len(r["pipelines"])
        ts = seen_ts.get(name)
        last = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else now
        repos[name] = {
            "ids": ids.get(name, []),
            "runs": runs,
            "steps": {s: steps[s] for s in sorted(heaviest)},
            "mem_peak_p95_bytes": p95,
            "mem_peak_max_bytes": max(s["mem_peak_max_bytes"] for s in steps.values()),
            "mem_class": mem_class(p95),
            "confidence": "low" if runs < LOW_CONFIDENCE_RUNS else "ok",
            "oom_suspect": any(s["oom_suspect"] for s in steps.values()),
            "last_seen": last.strftime("%Y-%m-%d"),
        }
    # This file is committed to a PUBLIC repository: only repositories that have a registry id (so are already public in
    # services.json) are named. Every other repository (private or infrastructure) appears only in an anonymous aggregate.
    mapped = {n: r for n, r in repos.items() if r["ids"]}
    hidden = [r for r in repos.values() if not r["ids"]]
    return {
        "version": VERSION,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days,
        "source": SOURCE,
        "repos": mapped,
        "unmapped": {
            "count": len(hidden),
            "runs": sum(r["runs"] for r in hidden),
            "mem_peak_p95_max_bytes": max((r["mem_peak_p95_bytes"] for r in hidden), default=0),
        },
        "classes": dict(CLASSES),
    }


def render(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


# -------------------------------------------------------------------- check

def check(doc: Any, services: List[dict], now: dt.datetime, max_age_days: float = 8) -> List[str]:
    """Return a list of problems (empty = valid)."""
    errs: List[str] = []
    if not isinstance(doc, dict):
        return ["top level is not an object"]
    if doc.get("version") != VERSION:
        errs.append("version is %r, expected %d" % (doc.get("version"), VERSION))
    if doc.get("classes") != CLASSES:
        errs.append("classes table does not match the script's class table")
    try:
        gen = dt.datetime.strptime(doc.get("generated_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        age = (now - gen).total_seconds() / 86400
        if age > max_age_days:
            errs.append("generated_at %s is %.1f days old (max %s): refresh with bin/ci_resources.py --write"
                        % (doc["generated_at"], age, max_age_days))
    except ValueError:
        errs.append("generated_at missing or not YYYY-MM-DDTHH:MM:SSZ")
    repos = doc.get("repos")
    if not isinstance(repos, dict):
        return errs + ["repos is not an object"]
    known = {e.get("id") for e in services}
    for name, r in sorted(repos.items()):
        where = "repos.%s" % name
        for k in ("ids", "runs", "steps", "mem_peak_p95_bytes", "mem_peak_max_bytes", "mem_class",
                  "confidence", "oom_suspect", "last_seen"):
            if k not in r:
                errs.append("%s: missing %s" % (where, k))
        if any(k not in r for k in ("steps", "mem_peak_p95_bytes", "mem_class", "ids", "runs")):
            continue
        for i in r["ids"]:
            if i not in known:
                errs.append("%s: id %r is not in services.json" % (where, i))
        if not r["ids"]:
            errs.append("%s: no registry ids; only repositories already public in services.json may be named in this file" % where)
        if r["mem_class"] != mem_class(r["mem_peak_p95_bytes"]):
            errs.append("%s: mem_class %r inconsistent with mem_peak_p95_bytes %d (expected %r)"
                        % (where, r["mem_class"], r["mem_peak_p95_bytes"], mem_class(r["mem_peak_p95_bytes"])))
        if r["confidence"] != ("low" if r["runs"] < LOW_CONFIDENCE_RUNS else "ok"):
            errs.append("%s: confidence %r inconsistent with runs %d" % (where, r["confidence"], r["runs"]))
        if r["mem_peak_p95_bytes"] > r["mem_peak_max_bytes"]:
            errs.append("%s: p95 above max" % where)
        if not r["steps"] or len(r["steps"]) > MAX_STEPS:
            errs.append("%s: steps must hold 1..%d entries" % (where, MAX_STEPS))
        for sname, s in r["steps"].items():
            if s.get("runs", 0) < 1 or s.get("mem_peak_p95_bytes", 0) > s.get("mem_peak_max_bytes", -1):
                errs.append("%s.steps.%s: bad runs or p95/max" % (where, sname))
            if s.get("oom_suspect") != (s.get("mem_peak_max_bytes", 0) >= OOM_SUSPECT_BYTES):
                errs.append("%s.steps.%s: oom_suspect inconsistent with max" % (where, sname))
    um = doc.get("unmapped")
    if not isinstance(um, dict) or any(not isinstance(um.get(k), int) or um.get(k) < 0 for k in ("count", "runs", "mem_peak_p95_max_bytes")):
        errs.append("unmapped must be an object with non-negative integer count, runs and mem_peak_p95_max_bytes (no repository names)")
    if "unmapped_repos" in doc:
        errs.append("unmapped_repos must not exist: it would publish private repository names")
    if SECRET_RE.search(json.dumps(doc)):
        errs.append("a secret-looking string is present in the file")
    return errs


# --------------------------------------------------------------------- main

def load_services() -> List[dict]:
    return json.loads(SERVICES_JSON.read_text())


def summary(doc: dict) -> str:
    repos = doc["repos"]
    hist = {c: 0 for c in CLASSES}
    for r in repos.values():
        hist[r["mem_class"]] += 1
    rows = sorted(((s["mem_peak_p95_bytes"], n, sn) for n, r in repos.items() for sn, s in r["steps"].items()),
                  reverse=True)[:10]
    lines = ["repos: %d  runs: %d  unmapped: %d  oom_suspect repos: %d" % (
        len(repos), sum(r["runs"] for r in repos.values()), doc["unmapped"]["count"],
        sum(1 for r in repos.values() if r["oom_suspect"])),
        "classes: " + "  ".join("%s=%d" % (c, hist[c]) for c in CLASSES), "heaviest steps (p95):"]
    lines += ["  %8.1f MiB  %s / %s" % (b / MIB, n, sn) for b, n, sn in rows]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prom-url", default="http://127.0.0.1:19090")
    ap.add_argument("--window", default="7d", help="Prometheus range, whole days (default 7d)")
    ap.add_argument("--input", type=Path, help="offline: saved instant-query result instead of Prometheus")
    ap.add_argument("--write", action="store_true", help="write ci-resources.json")
    ap.add_argument("--check", action="store_true", help="offline: validate ci-resources.json and exit")
    ap.add_argument("--max-age-days", type=float, default=8)
    ap.add_argument("--json", action="store_true", help="print the document as JSON")
    args = ap.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    services = load_services()

    if args.check:
        if not OUT_JSON.exists():
            print("FAIL: %s does not exist" % OUT_JSON.name, file=sys.stderr)
            return 1
        errs = check(json.loads(OUT_JSON.read_text()), services, now, args.max_age_days)
        for e in errs:
            print("FAIL: " + e, file=sys.stderr)
        if errs:
            return 1
        print("ci-resources.json ok")
        return 0

    m = re.fullmatch(r"(\d+)d", args.window)
    if not m:
        ap.error("--window must be whole days, like 7d")
    data = load_input(args.input) if args.input else fetch(args.prom_url, args.window)
    if not data["memory"]:
        print("FAIL: no memory series returned; refusing to write an empty file", file=sys.stderr)
        return 1
    doc = derive(data, services, now, int(m.group(1)))
    if args.write:
        OUT_JSON.write_text(render(doc))
        print("wrote %s (%d repos)" % (OUT_JSON.name, len(doc["repos"])))
    if args.json:
        sys.stdout.write(render(doc))
    elif not args.write:
        print(summary(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
