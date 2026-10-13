#!/usr/bin/env python3
"""
fleet-selftest-poll — poll every fleet service's /selftest and export the
result as Prometheus metrics (node_exporter textfile collector).

Why this exists: every fleet service ships `GET /selftest`, and the alert rule
`SelftestCheckFailing` (fleet_selftest_check_pass == 0) has been waiting for
data since it was written, but nothing ever produced the series:

  * go-common only reports a selftest to Prometheus when a service passes
    `selftest.WithObserver(...)` explicitly, and none of them do;
  * go-fleet-selftest-aggregator polls /selftest, but production runs it in
    the one-service graph-pilot mode (POLL_SERVICE_ALLOWLIST=fleet-preflight)
    and it exports no selftest series anyway.

So the alert was inert. This poller closes the loop from outside, with the
same plumbing as fleet-edge-probe (systemd timer -> textfile -> Prometheus).

What it exports (all gauges, rewritten atomically on every run):

  fleet_selftest_check_pass{service,check}          1 pass / 0 fail, one per named check
                                                    ("_overall" when the body has no check list)
  fleet_selftest_service_state{service,state}       1 for the current state:
                                                    pass | fail | unreachable | skip | gated
  fleet_selftest_consecutive_failures{service}      polls in a row that were fail/unreachable
  fleet_selftest_services{state}                    tally per state
  fleet_selftest_poll_targets                       services polled this run
  fleet_selftest_poll_last_run_timestamp_seconds
  fleet_selftest_poll_duration_seconds
  fleet_selftest_poll_up                            1 when the run completed

Where it probes: compose services (0docker) sit behind a gateway that wants an API key, so the
poller reaches them directly on the docker hosts' LAN (`--direct-hosts`, registry `host_port`,
tried in order); `/selftest` is public on the service itself, so no credential is needed. Services
without a LAN route (coolify, 0mcp) are probed on their public edge, where /selftest is also open.

State semantics (same taxonomy as go-fleet-selftest-aggregator, ADR-0015):

  pass         2xx (and body `ok` is not false)
  fail         5xx, other 4xx except 401/403/404, or 2xx with `"ok": false`
  skip         404: service has not implemented /selftest yet
  gated        401/403: endpoint is behind auth and this poller has no key
  unreachable  DNS / TLS / connect / timeout (for a compose service: no docker host answers on its port)

`skip` and `gated` are visible but never alert: they say "not measured", not "broken".

Exit: 0 = run completed (whatever the services said); 2 = usage / registry fetch error.

stdlib only. Python >= 3.8.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

REGISTRY_URL = "https://raw.githubusercontent.com/baditaflorin/services-registry/main/services.json"
MAX_BODY = 64 * 1024
STATES = ("pass", "fail", "unreachable", "skip", "gated")
SKIP_LIFECYCLE_STATUSES = {"sunset"}
UA = "fleet-selftest-poll/1.0 (+services-registry)"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_registry(src):
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    else:
        with open(src) as f:
            data = json.load(f)
    items = data if isinstance(data, list) else data.get("services", [])
    if not items:
        raise ValueError("registry is empty")
    return items


def eligible(svc):
    """Container services with a public https URL that are not retired or LAN-only."""
    if svc.get("kind") != "container":
        return False
    if not str(svc.get("url", "")).startswith("https://"):
        return False
    if (svc.get("lifecycle") or {}).get("status") in SKIP_LIFECYCLE_STATUSES:
        return False
    if svc.get("runtime") == "external" or svc.get("network_exposure") == "lan-internal":
        return False
    return True


def selftest_url(svc):
    """health_url (preferred) or url, path forced to /selftest, no query, no credentials."""
    base = svc.get("health_url") or svc.get("url")
    parts = urlsplit(base)
    host = parts.hostname or ""
    netloc = host + (":%d" % parts.port if parts.port else "")
    return urlunsplit((parts.scheme, netloc, "/selftest", "", ""))


def classify(http_status, body):
    """-> (state, [(check_name, passed_bool), ...]); http_status None means unreachable."""
    if http_status is None:
        return "unreachable", []
    obj = None
    try:
        parsed = json.loads(body) if body else None
        if isinstance(parsed, dict):
            obj = parsed
    except ValueError:
        pass
    checks = []
    if obj and isinstance(obj.get("checks"), list):
        for c in obj["checks"]:
            if isinstance(c, dict) and c.get("name") and isinstance(c.get("pass"), bool):
                checks.append((str(c["name"]), c["pass"]))
    if http_status == 404:
        return "skip", []
    if http_status in (401, 403):
        return "gated", []
    if 200 <= http_status < 300:
        if obj is not None and obj.get("ok") is False:
            return "fail", checks or [("_overall", False)]
        return "pass", checks or [("_overall", True)]
    return "fail", checks or [("_overall", False)]


def fetch(url, timeout, api_key=None):
    """-> (http_status or None, body text, error text)."""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(MAX_BODY).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BODY).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, ""
    except Exception as e:  # noqa: BLE001  (DNS, TLS, refused, timeout)
        return None, "", "%s: %s" % (type(e).__name__, e)


def probe(svc, timeout, api_key=None, direct_hosts=()):
    sid = svc.get("id") or svc.get("slug")
    t0 = time.time()
    port = svc.get("host_port")
    if direct_hosts and svc.get("runtime") == "compose" and port:
        status, body, err, url = None, "", "", ""
        for h in direct_hosts:
            url = "http://%s:%d/selftest" % (h, int(port))
            status, body, err = fetch(url, timeout)
            if status is not None:
                break
        via = "lan"
    else:
        url = selftest_url(svc)
        status, body, err = fetch(url, timeout, api_key)
        via = "edge"
    state, checks = classify(status, body)
    return {"id": sid, "url": url, "via": via, "http": status, "state": state, "checks": checks,
            "ms": int((time.time() - t0) * 1000), "error": err}


def update_failures(prev, results):
    """Consecutive fail/unreachable polls per service. pass/skip/gated reset the count."""
    out = {}
    for r in results:
        if r["state"] in ("fail", "unreachable"):
            out[r["id"]] = int(prev.get(r["id"], 0)) + 1
        else:
            out[r["id"]] = 0
    return out


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_prom(results, failures, started, duration):
    L = []
    add = L.append
    add("# HELP fleet_selftest_check_pass Last-observed state of a named /selftest check (1 pass, 0 fail).")
    add("# TYPE fleet_selftest_check_pass gauge")
    for r in sorted(results, key=lambda x: x["id"]):
        for name, ok in r["checks"]:
            add('fleet_selftest_check_pass{service="%s",check="%s"} %d' % (_esc(r["id"]), _esc(name), 1 if ok else 0))
    add("# HELP fleet_selftest_service_state 1 for the service's current /selftest state.")
    add("# TYPE fleet_selftest_service_state gauge")
    for r in sorted(results, key=lambda x: x["id"]):
        add('fleet_selftest_service_state{service="%s",state="%s"} 1' % (_esc(r["id"]), r["state"]))
    add("# HELP fleet_selftest_consecutive_failures Polls in a row that ended fail or unreachable.")
    add("# TYPE fleet_selftest_consecutive_failures gauge")
    for r in sorted(results, key=lambda x: x["id"]):
        add('fleet_selftest_consecutive_failures{service="%s"} %d' % (_esc(r["id"]), failures.get(r["id"], 0)))
    add("# HELP fleet_selftest_services Services per /selftest state in the last run.")
    add("# TYPE fleet_selftest_services gauge")
    tally = {s: 0 for s in STATES}
    for r in results:
        tally[r["state"]] += 1
    for s in STATES:
        add('fleet_selftest_services{state="%s"} %d' % (s, tally[s]))
    add("# TYPE fleet_selftest_poll_targets gauge")
    add("fleet_selftest_poll_targets %d" % len(results))
    add("# TYPE fleet_selftest_poll_last_run_timestamp_seconds gauge")
    add("fleet_selftest_poll_last_run_timestamp_seconds %d" % int(started))
    add("# TYPE fleet_selftest_poll_duration_seconds gauge")
    add("fleet_selftest_poll_duration_seconds %.1f" % duration)
    add("# TYPE fleet_selftest_poll_up gauge")
    add("fleet_selftest_poll_up 1")
    return "\n".join(L) + "\n"


def write_atomic(path, text):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f).get("failures", {})
    except (OSError, ValueError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--services", default=os.environ.get("SELFTEST_POLL_SERVICES", REGISTRY_URL),
                    help="services.json path or URL")
    ap.add_argument("--prom", default=os.environ.get("SELFTEST_POLL_PROM", ""),
                    help="write textfile-collector metrics to this path")
    ap.add_argument("--state", default=os.environ.get("SELFTEST_POLL_STATE", ""),
                    help="state file holding consecutive-failure counts")
    ap.add_argument("--api-key-file", default=os.environ.get("SELFTEST_POLL_API_KEY_FILE", ""),
                    help="file with an API key sent as X-API-Key (optional; gated services stay 'gated' without it)")
    ap.add_argument("--direct-hosts", default=os.environ.get("SELFTEST_POLL_DIRECT_HOSTS", ""),
                    help="comma-separated docker-host IPs to try, in order, for compose services (registry host_port)")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--only", default="", help="comma-separated service ids (debug)")
    ap.add_argument("--json", action="store_true", help="print per-service results as JSON")
    a = ap.parse_args(argv)

    try:
        items = load_registry(a.services)
    except Exception as e:  # noqa: BLE001
        log("cannot load registry %s: %s" % (a.services, e))
        return 2
    targets = [s for s in items if eligible(s)]
    if a.only:
        want = set(a.only.split(","))
        targets = [s for s in targets if (s.get("id") or s.get("slug")) in want]
    api_key = None
    if a.api_key_file:
        try:
            api_key = open(a.api_key_file).read().strip() or None
        except OSError as e:
            log("cannot read api key file: %s" % e)

    direct = tuple(h.strip() for h in a.direct_hosts.split(",") if h.strip())
    started = time.time()
    log("polling /selftest of %d services (timeout=%.0fs, concurrency=%d) ..." % (len(targets), a.timeout, a.concurrency))
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        results = list(ex.map(lambda s: probe(s, a.timeout, api_key, direct), targets))
    duration = time.time() - started

    failures = update_failures(load_state(a.state) if a.state else {}, results)
    tally = {s: 0 for s in STATES}
    for r in results:
        tally[r["state"]] += 1
    for r in sorted(results, key=lambda x: x["id"]):
        if r["state"] in ("fail", "unreachable"):
            bad = [n for n, ok in r["checks"] if not ok]
            log("%-11s %-34s via=%s http=%s failing=%s %s" % (r["state"].upper(), r["id"], r["via"], r["http"], ",".join(bad) or "-", r["error"]))
    log("%s  %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
                    "  ".join("%s=%d" % (s, tally[s]) for s in STATES)))

    if a.prom:
        try:
            write_atomic(a.prom, render_prom(results, failures, started, duration))
        except OSError as e:
            log("cannot write prom textfile %s: %s" % (a.prom, e))
            return 2
    if a.state:
        try:
            write_atomic(a.state, json.dumps({"ts": int(started), "failures": failures}))
        except OSError as e:
            log("cannot write state %s: %s" % (a.state, e))
    if a.json:
        print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
