#!/usr/bin/env python3
"""verify-examples: call each service's registry `example_path` and record what the service answered.

Why. The hub's "Open" and "Run example" links come from `example_path` (see bin/sync-service-params.py). Checking
the address against the public gateway proves only the host: the gateway answers 401 for any path. So this probes
the service itself (registry `host_port` on the docker hosts' LAN, the same route bin/fleet-selftest-poll.py
uses), sends one API key in X-API-Key, and records the status and a verdict per service. A 200 means the route and
its inputs are right; a 400 that names a missing parameter means the example lacks an input; a 404 means the route
is wrong.

Output (default services.examples.json, derived data: never edit by hand, regenerate by running this):

  {"checked_at": "...", "services": {"<id>": {"path", "http", "state", "via", "ms", "error", "detail"?,
                                              "replicas"?, "diverged"?}}}

Replicas. A service marked placement "replicable" runs on several hosts behind the gateway (nginx upstream, same
host_port on every member), and the gateway may send any user to any of them. So every host of --direct-hosts is
called, not only the first that answers: a host "answers" when it returns any HTTP status (a refused connection or a
timeout means the service does not run there). With one answering host the row is as before. With two or more it also
carries `replicas: [{"host", "http", "state"}]` in --direct-hosts order, and `state`, `http`, `error` and `detail`
are those of the WORST replica (order: WORST_FIRST below, the first host on a tie), so a broken or stale replica is
never hidden by a healthy one. `diverged: true` is set when the replicas answer with different states. Which
replicas exist is the gateway's business (fleet-runner pool-status): a replica on a host that is not in
--direct-hosts, or that this machine cannot reach, is not seen.

States:
  ok             2xx or 3xx
  missing-param  400/422 whose body says a parameter is missing or required: the example needs another input
  rejected       any other 400/422: the route exists, the service refused this input
  no-data        404 with a JSON body: the route is right, the service has no data for this input (many extractors answer 404
                 "no data" on purpose so a consumer does not record a false result)
  not-found      404 without a JSON body (the mux's "404 page not found"): the route is wrong
  method         405: the route exists but not for GET
  gated          401/403: the key was not accepted (or none was given)
  error          5xx or 429: the route exists, the service or its backend failed
  unreachable    DNS / connect / timeout
  unprobed       no LAN route and no key for the public edge, or no example_path

The key is read from --api-key-file, sent only to the registry's own hosts, and never printed or written out.
stdlib only. Python >= 3.8.
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

REGISTRY_URL = "https://raw.githubusercontent.com/baditaflorin/services-registry/main/services.json"
MAX_BODY = 16 * 1024
UA = "verify-examples/1.0 (+services-registry)"
SKIP_LIFECYCLE_STATUSES = {"sunset"}
# Worst last. `gated` ranks low because it says "not measured" (our key was not accepted), not "the service is broken";
# a replica that answers 5xx or the wrong route outranks a healthy one, and `unreachable` is never among answered replicas.
WORST_FIRST = ("ok", "no-data", "gated", "method", "missing-param", "rejected", "not-found", "error", "unreachable")
MISSING = re.compile(r"missing|required|must (?:be )?(?:provide|supplied|set)|not provided", re.I)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


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


def _is_json_object(body):
    try:
        return isinstance(json.loads(body or ""), dict)
    except ValueError:
        return False


def detail(body, limit=120):
    """Why a call was not ok, in a few words: the JSON error message when there is one, else the start of the body."""
    try:
        obj = json.loads(body or "")
    except ValueError:
        obj = None
    msg = ""
    if isinstance(obj, dict):
        e = obj.get("error")
        msg = (e.get("message") if isinstance(e, dict) else e) or obj.get("message") or obj.get("status") or ""
    return " ".join(str(msg or body or "").split())[:limit]


def classify(http_status, body):
    """-> state for one answered (or unanswered) example call."""
    if http_status is None:
        return "unreachable"
    if 200 <= http_status < 400:
        return "ok"
    if http_status in (401, 403):
        return "gated"
    if http_status == 404:
        return "no-data" if _is_json_object(body) else "not-found"
    if http_status == 405:
        return "method"
    if http_status in (400, 422):
        return "missing-param" if MISSING.search(body or "") else "rejected"
    return "error"


def fetch(url, timeout, api_key):
    """-> (http status or None, body text, error text). Redirects are not followed (the key must not leave the host)."""
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.read(MAX_BODY).decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BODY).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body, ""
    except Exception as e:  # noqa: BLE001
        return None, "", "%s: %s" % (type(e).__name__, e)


def eligible(svc):
    if svc.get("kind") != "container" or svc.get("runtime") == "external":
        return False
    if svc.get("network_exposure") == "lan-internal" or svc.get("served_by"):
        return False
    return (svc.get("lifecycle") or {}).get("status") not in SKIP_LIFECYCLE_STATUSES


def severity(state):
    return WORST_FIRST.index(state) if state in WORST_FIRST else len(WORST_FIRST)


def judge(host, status, body, err):
    """One host's answer as a replica record: (host, http, state, error, detail)."""
    state = classify(status, body)
    return {"host": host, "http": status, "state": state, "error": err,
            "detail": detail(body) if state not in ("ok", "unreachable") else ""}


def probe(svc, timeout, api_key, direct_hosts):
    sid = svc.get("id") or svc.get("slug")
    path = svc.get("example_path") or ""
    out = {"path": path, "http": None, "state": "unprobed", "via": "", "ms": 0, "error": ""}
    if not path.startswith("/"):
        return sid, out
    t0 = time.time()
    port = svc.get("host_port")
    if direct_hosts and svc.get("runtime") == "compose" and port:
        # Every host, in order: the key only ever goes to a --direct-hosts address (a registry host), on the service's own port.
        calls = [judge(h, *fetch("http://%s:%d%s" % (h, int(port), path), timeout, api_key)) for h in direct_hosts]
        answered = [c for c in calls if c["http"] is not None]
        out["via"] = "lan"
        if not answered:                               # nobody runs it there: the last host's reason, as before
            judged = calls[-1]
        else:
            judged = min(answered, key=lambda c: (-severity(c["state"]), answered.index(c)))
            if len(answered) > 1:
                out["replicas"] = [{"host": c["host"], "http": c["http"], "state": c["state"]} for c in answered]
                if len({c["state"] for c in answered}) > 1:
                    out["diverged"] = True
    elif api_key and str(svc.get("url", "")).startswith("https://"):
        host = urlsplit(svc["url"]).netloc
        judged = judge(host, *fetch("https://%s%s" % (host, path), timeout, api_key))
        out["via"] = "edge"
    else:
        return sid, out
    out.update(http=judged["http"], state=judged["state"], ms=int((time.time() - t0) * 1000), error=judged["error"])
    if judged["state"] not in ("ok", "unreachable"):
        out["detail"] = judged["detail"]
    return sid, out


def render(results, now=None):
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"checked_at": now, "services": {k: results[k] for k in sorted(results)}}, indent=2) + "\n"


def tally(results):
    t = {}
    for r in results.values():
        t[r["state"]] = t.get(r["state"], 0) + 1
    return dict(sorted(t.items()))


def replica_summary(results):
    """-> (services with more than one answering replica, ids whose replicas diverge)."""
    multi = [k for k, r in results.items() if r.get("replicas")]
    return len(multi), sorted(k for k, r in results.items() if r.get("diverged"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--services", default=os.environ.get("VERIFY_EXAMPLES_SERVICES", REGISTRY_URL),
                    help="services.json path or URL")
    ap.add_argument("--out", default="services.examples.json", help="where to write the result")
    ap.add_argument("--api-key-file", default=os.environ.get("VERIFY_EXAMPLES_API_KEY_FILE", ""),
                    help="file with the API key sent as X-API-Key")
    ap.add_argument("--direct-hosts", default=os.environ.get("VERIFY_EXAMPLES_DIRECT_HOSTS", "10.10.10.20,10.10.10.30"),
                    help="comma-separated docker-host addresses for compose services: every one is called on the registry "
                         "host_port, and each that answers is a replica (the key is sent to these hosts only)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--only", default="", help="comma-separated service ids (debug)")
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
            return 2
    direct = tuple(h.strip() for h in a.direct_hosts.split(",") if h.strip())
    log("checking the example of %d services (timeout=%.0fs, concurrency=%d, key=%s) ..." % (
        len(targets), a.timeout, a.concurrency, "yes" if api_key else "no"))
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        results = dict(ex.map(lambda s: probe(s, a.timeout, api_key, direct), targets))
    with open(a.out, "w") as f:
        f.write(render(results))
    n_multi, diverged = replica_summary(results)
    log("wrote %s: %s" % (a.out, json.dumps(tally(results))))
    log("replicas: %d service(s) answered on more than one host; %d diverged%s" % (
        n_multi, len(diverged), (": " + ", ".join(diverged)) if diverged else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
