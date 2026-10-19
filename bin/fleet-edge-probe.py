#!/usr/bin/env python3
"""
fleet-edge-probe — strict external synthetic prober for every fleet service's
PUBLIC edge (DNS -> TLS -> redirects -> body), driven by services.json.

Why this exists: the 2026-09-09 cookie-checker outage (whole coolify-0crawl
batch serving the wrong TLS cert, HTTP :80 redirecting cross-domain to
nginx.0mcp.com/health -> 200) survived for days because the existing checks
follow redirects and only assert HTTP 200, and `fleet-runner smoke` can run
with -insecure. This asserts the things those skip:

  1  dns_nxdomain        hostname resolves
  2  gateway_drift       A record is outside the accepted public edge set for the
                         service (built-in per-runtime default, extended by the optional
                         edges data file and by EDGE_PROBE_GATEWAY_EXTRA; see the README)
  3  tls_untrusted       TLS chain validates with the system trust store (no -k)
  4  cert_host_mismatch  leaf cert SAN actually covers this hostname
  5  cert_registry_mismatch  SAN matches registry `cert_domain`
                              (wildcard.0crawl.com => SAN must include *.0crawl.com)
  6  cert_expiring       notAfter > now + --expiry-days
  7  offsite_redirect    GET /health may redirect, but must stay on the same host
  8  health_status       final /health status == 200
  9  health_body         /health body is JSON with status ok|healthy|up|success, or
                         {"ok": true}, or carries a service/version key (not a landing page)
 10  fallback_vhost      body is not the known default-vhost fingerprint (<title>LV3 ...)
 11  example_unreachable GET <url><example_path> -> 200/401/403 and same host
                         (401/403 == "edge wired, auth on" == fine)

Services whose registry entry has lifecycle.status == "sunset" are not probed
(they are deliberately retired and answer 410); they are reported as `skipped`
and stay visible via fleet_edge_probe_service_skipped / fleet_edge_probe_skipped_total.

Exit: 0 = all probed services pass ; 1 = >=1 failure ; 2 = usage/fetch error ;
3 = nothing eligible to probe (the filters left zero services: this vantage is blind).
Exit 3 still writes fleet_edge_probe_eligible_services 0 to the --prom textfile and, with
--state and --ntfy, sends one alert when the run enters that state.

stdlib only. Needs: python3, curl.
"""

from __future__ import annotations
import argparse, ipaddress, json, os, socket, ssl, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlsplit

REGISTRY_URL = "https://raw.githubusercontent.com/baditaflorin/services-registry/main/services.json"

# Built-in default: known public gateway IPs by runtime. This is the first layer of the
# accepted-edge table (see EdgeTable): the optional edges data file and the per-vantage
# EDGE_PROBE_GATEWAY_EXTRA only ADD to it, so deleting a file or unsetting a variable can
# never make the check stricter than this default.
GATEWAY_IPS = {
    "compose": {"176.9.123.221"},
    "coolify": {"65.108.75.123"},
}

# Optional data file (JSON, schema in the README) next to the script / checkout, and the
# per-vantage environment variable carrying extra accepted addresses.
DEFAULT_EDGES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "fleet-edge-probe.edges.json")
EDGES_ENV = "EDGE_PROBE_EDGES"
GATEWAY_EXTRA_ENV = "EDGE_PROBE_GATEWAY_EXTRA"
EDGES_SCHEMA_VERSION = 1
EDGES_FILE_KEYS = {"schema", "description", "runtime", "mesh", "any"}

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_NOTHING_ELIGIBLE = 0, 1, 2, 3

# registry `network_exposure` values that mean "no public edge to probe". The schema enum
# (schema/v1.json) spells them lan-internal / loopback; "internal" is the older spelling
# this script used to compare against, which no registry value ever matched.
NOT_PUBLIC_EXPOSURES = {"internal", "lan-internal", "loopback"}

# registry `lifecycle.status` values that mean "deliberately retired, do not probe".
# schema/v1.json defines active|sunset only; extend here if the schema grows more.
SKIP_LIFECYCLE_STATUSES = {"sunset"}

# JSON `status` values (case-insensitive) accepted as a healthy /health body.
HEALTHY_STATUS_VALUES = ("ok", "healthy", "up", "success")

FALLBACK_FINGERPRINTS = ("<title>lv3", "nginx.0mcp.com", "default backend - 404")

SEVERITY = {  # codes not listed default to "fail"
    "gateway_drift": "warn",
    "cert_expiring": "warn",
    "example_unreachable": "warn",   # stale example_path in the registry is a data bug, not a page
    "health_forbidden": "warn",      # 401/403 on /health = edge up, endpoint gated (infra services)
    "health_notfound": "warn",       # 404 on /health = wrong path / missing health_url in registry
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class EdgeConfigError(ValueError):
    """The edges data file or EDGE_PROBE_GATEWAY_EXTRA is malformed (a usage error, exit 2)."""


def parse_ipv4_list(values, where: str) -> frozenset[str]:
    """Validate IPv4 addresses (only A records are compared). Raises EdgeConfigError."""
    out = set()
    for v in values:
        if not isinstance(v, str):
            raise EdgeConfigError(f"{where}: {v!r} is not an IP address string")
        try:
            ip = ipaddress.ip_address(v.strip())
        except ValueError:
            raise EdgeConfigError(f"{where}: {v!r} is not an IP address") from None
        if ip.version != 4:
            raise EdgeConfigError(f"{where}: {v!r} is not IPv4 (the probe compares A records)")
        out.add(str(ip))
    return frozenset(out)


def parse_gateway_extra(spec: str) -> frozenset[str]:
    """`EDGE_PROBE_GATEWAY_EXTRA` / --gateway-extra: comma-separated IPv4 addresses."""
    items = [t.strip() for t in (spec or "").split(",") if t.strip()]
    return parse_ipv4_list(items, GATEWAY_EXTRA_ENV)


class EdgeTable:
    """Accepted public edge addresses, layered. Every layer only ADDS addresses.

      1  runtime/mesh base sets  - the built-in GATEWAY_IPS plus the edges file's
                                   `runtime` and `mesh` maps. A service is checked
                                   for gateway_drift only if its runtime or mesh has
                                   a base set (an unknown runtime is never checked).
      2  any                     - edges file `any`: accepted for every checked service
                                   (a second public edge that fronts everything).
      3  extra                   - per-vantage EDGE_PROBE_GATEWAY_EXTRA: the internal
                                   split-horizon address an internal vantage resolves to.

    An A record that intersects the union passes; one outside it is gateway_drift.
    """

    def __init__(self, runtime=None, mesh=None, any_ips=(), extra=()):
        self.runtime = {k: frozenset(v) for k, v in (runtime or {}).items()}
        self.mesh = {k: frozenset(v) for k, v in (mesh or {}).items()}
        self.any = frozenset(any_ips)
        self.extra = frozenset(extra)

    def accepted(self, svc: dict) -> frozenset[str]:
        """Addresses an A record may carry for svc; empty means "no expectation, not checked"."""
        base = (self.runtime.get(svc.get("runtime", ""), frozenset())
                | self.mesh.get(svc.get("mesh", ""), frozenset()))
        if not base:
            return frozenset()
        return base | self.any | self.extra


def default_edge_table() -> EdgeTable:
    """Built-in layer only (reads GATEWAY_IPS at call time)."""
    return EdgeTable(runtime=GATEWAY_IPS)


def load_edges_file(path: str) -> dict:
    """Parse + validate an edges data file (schema in the README). Raises EdgeConfigError."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except OSError as e:
        raise EdgeConfigError(f"cannot read edges file {path}: {e.strerror or e}") from None
    except ValueError as e:
        raise EdgeConfigError(f"edges file {path} is not valid JSON: {e}") from None
    if not isinstance(doc, dict):
        raise EdgeConfigError(f"edges file {path}: top level must be an object")
    unknown = sorted(set(doc) - EDGES_FILE_KEYS)
    if unknown:  # a typo like "runtimes" must not silently disable an edge
        raise EdgeConfigError(f"edges file {path}: unknown key(s) {unknown}; "
                              f"allowed: {sorted(EDGES_FILE_KEYS)}")
    if doc.get("schema") != EDGES_SCHEMA_VERSION:
        raise EdgeConfigError(f"edges file {path}: \"schema\" must be {EDGES_SCHEMA_VERSION}, "
                              f"got {doc.get('schema')!r}")
    out = {"runtime": {}, "mesh": {}, "any": frozenset()}
    for axis in ("runtime", "mesh"):
        m = doc.get(axis, {})
        if not isinstance(m, dict):
            raise EdgeConfigError(f"edges file {path}: \"{axis}\" must be an object")
        for key, ips in m.items():
            if not key or not isinstance(ips, list):
                raise EdgeConfigError(f"edges file {path}: \"{axis}\".{key!r} must be a list of IPs")
            out[axis][key] = parse_ipv4_list(ips, f"edges file {path} {axis}.{key}")
    any_ips = doc.get("any", [])
    if not isinstance(any_ips, list):
        raise EdgeConfigError(f"edges file {path}: \"any\" must be a list of IPs")
    out["any"] = parse_ipv4_list(any_ips, f"edges file {path} any")
    return out


def build_edge_table(edges_path: str = "", extra_spec: str = "") -> EdgeTable:
    """Layer: built-in GATEWAY_IPS -> edges file (explicit path, else the default file if
    present) -> extra addresses. An explicit path that is unreadable or invalid raises
    EdgeConfigError; so does an invalid default file (never silently ignored)."""
    runtime = {k: set(v) for k, v in GATEWAY_IPS.items()}
    mesh: dict[str, set[str]] = {}
    any_ips: frozenset[str] = frozenset()
    path = edges_path or (DEFAULT_EDGES_FILE if os.path.exists(DEFAULT_EDGES_FILE) else "")
    if path:
        data = load_edges_file(path)
        for k, v in data["runtime"].items():
            runtime.setdefault(k, set()).update(v)
        for k, v in data["mesh"].items():
            mesh.setdefault(k, set()).update(v)
        any_ips = data["any"]
    return EdgeTable(runtime, mesh, any_ips, parse_gateway_extra(extra_spec))


def parse_runtime_filter(spec: str) -> frozenset[str] | None:
    """`--runtime`: comma list of runtimes, or `all`/empty for no runtime filter (None)."""
    tokens = [t.strip().lower() for t in (spec or "").split(",") if t.strip()]
    if not tokens or "all" in tokens:
        return None
    return frozenset(tokens)


def select_services(reg: list[dict], runtimes, only: set[str], sample: int = 0):
    """Apply the eligibility filters. Returns (eligible, detail).

    `eligible` keeps services whose registry lifecycle is sunset (probe_or_skip reports
    them as skipped, as before). `detail` is JSON-able and explains an empty selection:
    every registry entry is counted once, under the first reason that excluded it, so
    `registry_entries == sum(excluded) + matched`.
    """
    excluded = {"kind": 0, "external": 0, "url": 0, "network_exposure": 0, "runtime": 0, "only": 0}
    elig: list[dict] = []
    runtime_counts: dict[str, int] = {}
    for e in reg:
        rt = str(e.get("runtime") or "").lower()
        runtime_counts[rt or "(none)"] = runtime_counts.get(rt or "(none)", 0) + 1
        if e.get("kind") != "container":
            excluded["kind"] += 1
        elif rt == "external":
            excluded["external"] += 1
        elif not str(e.get("url", "")).startswith("https://"):
            excluded["url"] += 1
        elif e.get("network_exposure") in NOT_PUBLIC_EXPOSURES:
            excluded["network_exposure"] += 1
        elif runtimes is not None and rt not in runtimes:
            excluded["runtime"] += 1
        elif only and e.get("id") not in only:
            excluded["only"] += 1
        else:
            elig.append(e)
    if sample:
        elig = elig[:sample]
    detail = {
        "registry_entries": len(reg),
        "filters": {"runtime": sorted(runtimes) if runtimes is not None else "all",
                    "only": sorted(only), "sample": sample},
        "excluded": excluded,
        "matched": len(elig),
        "lifecycle_skipped": sum(1 for e in elig if lifecycle_skip_status(e)),
        "registry_runtimes": dict(sorted(runtime_counts.items())),
        "requested_runtime_counts": ({r: runtime_counts.get(r, 0) for r in sorted(runtimes)}
                                     if runtimes is not None else {}),
    }
    return elig, detail


def describe_selection(detail: dict) -> str:
    """One human line naming the filters and how many services each one excluded."""
    f, x = detail["filters"], detail["excluded"]
    rt = f["runtime"] if f["runtime"] == "all" else ",".join(f["runtime"])
    parts = [
        f"of {detail['registry_entries']} registry entries, {detail['matched']} matched "
        f"({detail['lifecycle_skipped']} of them sunset, never probed)",
        f"filters: runtime={rt} only={','.join(f['only']) or '-'} sample={f['sample'] or '-'}",
        "excluded by kind != container: %d; by runtime external (never probed): %d; "
        "by url not https: %d; by network_exposure not public: %d; "
        "by runtime not in filter: %d; by --only: %d" % (
            x["kind"], x["external"], x["url"], x["network_exposure"], x["runtime"], x["only"]),
    ]
    if detail["requested_runtime_counts"]:
        parts.append("requested runtimes in this registry: " + ", ".join(
            f"{k}={v}" for k, v in detail["requested_runtime_counts"].items()))
    parts.append("registry runtimes: " + ", ".join(
        f"{k}={v}" for k, v in detail["registry_runtimes"].items()))
    return "; ".join(parts)


def load_registry(src: str) -> list[dict]:
    if src.startswith("http"):
        with urllib.request.urlopen(src, timeout=30) as r:
            return json.load(r)
    with open(src) as f:
        return json.load(f)


def resolve_a(host: str) -> set[str]:
    for attempt in range(3):  # resolver flakes under a wide concurrent burst
        try:
            return {ai[4][0] for ai in socket.getaddrinfo(
                host, 443, socket.AF_INET, socket.SOCK_STREAM)}
        except socket.gaierror:
            if attempt == 2:
                return set()
            time.sleep(0.4 * (attempt + 1))
        except OSError:
            return set()
    return set()


def san_covers(san_dns: list[str], host: str) -> bool:
    h = host.lower().rstrip(".")
    for entry in san_dns:
        e = entry.lower().rstrip(".")
        if e == h:
            return True
        if e.startswith("*."):
            # one label only, per RFC 6125
            if h.count(".") == e.count(".") and h.split(".", 1)[1] == e[2:]:
                return True
    return False


def expected_san_from_cert_domain(cert_domain: str) -> str | None:
    if not cert_domain:
        return None
    if cert_domain.startswith("wildcard."):
        return "*." + cert_domain[len("wildcard."):]
    return cert_domain


def inspect_cert(host: str, timeout: float):
    """Return (verified: bool, san_dns: list[str], not_after_epoch: float|None, err: str)."""
    ctx = ssl.create_default_context()
    for attempt in range(3):
        try:
            with socket.create_connection((host, 443), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
            san = [v for (k, v) in cert.get("subjectAltName", ()) if k == "DNS"]
            na = cert.get("notAfter")
            na_epoch = ssl.cert_time_to_seconds(na) if na else None
            return True, san, na_epoch, ""
        except ssl.SSLCertVerificationError as e:
            # verification failed — capture what was actually served, unverified
            san = []
            try:
                unv = ssl._create_unverified_context()
                with socket.create_connection((host, 443), timeout=timeout) as sock:
                    with unv.wrap_socket(sock, server_hostname=host) as ssock:
                        cert = ssock.getpeercert()  # may be empty on unverified
                        der = ssock.getpeercert(binary_form=True)
                san = [v for (k, v) in (cert or {}).get("subjectAltName", ()) if k == "DNS"]
                if not san and der:
                    san = _san_from_der(der)
            except Exception:
                pass
            return False, san, None, f"{e.__class__.__name__}: {e}".split("(")[0].strip()
        except socket.gaierror as e:
            if attempt == 2:
                return False, [], None, f"DNS: {e}"
            time.sleep(0.4 * (attempt + 1))
        except (OSError, ssl.SSLError) as e:
            if attempt == 2:
                return False, [], None, f"{e.__class__.__name__}: {e}"
            time.sleep(0.4 * (attempt + 1))
    return False, [], None, "unreachable"


def _san_from_der(der: bytes) -> list[str]:
    """Best-effort SAN extraction from DER via `openssl` if present."""
    try:
        p = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-noout", "-ext", "subjectAltName"],
            input=der, capture_output=True, timeout=10,
        )
        out = p.stdout.decode(errors="replace")
        return [t.strip()[4:] for t in out.split(",") if t.strip().startswith("DNS:")]
    except Exception:
        return []


def curl(url: str, timeout: float, want_body: bool):
    """GET with redirects followed. Returns (code:int, final_url:str, body:str)."""
    body_path = "/dev/null"
    tmp = None
    if want_body:
        tmp = f"/tmp/.edgeprobe.{os.getpid()}.{abs(hash(url))}"
        body_path = tmp
    try:
        p = subprocess.run(
            ["curl", "-sS", "-L", "--max-redirs", "10", "-m", str(timeout),
             "-o", body_path, "-w", "%{http_code}\t%{url_effective}", url],
            capture_output=True, timeout=timeout + 5, text=True,
        )
        code_s, _, final = (p.stdout or "\t").partition("\t")
        code = int(code_s) if code_s.isdigit() else 0
        body = ""
        if want_body and tmp and os.path.exists(tmp):
            with open(tmp, "rb") as f:
                body = f.read(8192).decode(errors="replace")
        return code, final.strip() or url, body
    except (subprocess.SubprocessError, ValueError):
        return 0, url, ""
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def health_body_ok(hbody: str) -> bool:
    """True if a 200 /health body looks like a real app health response.

    JSON object: status in HEALTHY_STATUS_VALUES (case-insensitive), or
    ok is exactly true, or a `service` / `version` key is present.
    Non-JSON: plain `ok` / `healthy` / `up` (or starting with `ok`).
    Anything else (landing page, {"ok": false}, empty body) is not healthy.
    """
    try:
        j = json.loads(hbody)
    except ValueError:
        t = hbody.strip().lower()
        return t in ("ok", "healthy", "up") or t.startswith("ok")
    return isinstance(j, dict) and (
        str(j.get("status", "")).lower() in HEALTHY_STATUS_VALUES
        or j.get("ok") is True
        or "service" in j or "version" in j
    )


def lifecycle_skip_status(svc: dict) -> str | None:
    """Return the registry lifecycle status if this service must not be probed."""
    lc = svc.get("lifecycle")
    if isinstance(lc, dict):
        st = str(lc.get("status", "")).strip().lower()
        if st in SKIP_LIFECYCLE_STATUSES:
            return st
    return None


def skipped_result(svc: dict, status: str) -> dict:
    url = svc.get("url", "")
    return {
        "slug": svc.get("id", "?"), "host": urlsplit(url).hostname or "",
        "runtime": svc.get("runtime", ""),
        "fails": [], "hard_fail": False, "ok": True,
        "skipped": True, "lifecycle": status, "meta": {},
    }


def probe_or_skip(svc: dict, args) -> dict:
    status = lifecycle_skip_status(svc)
    if status:
        return skipped_result(svc, status)
    return probe(svc, args)


def probe(svc: dict, args) -> dict:
    slug = svc.get("id", "?")
    url = svc.get("url", "")
    host = urlsplit(url).hostname or ""
    runtime = svc.get("runtime", "")
    fails: list[str] = []

    def add(code):
        fails.append(code)

    # 1 DNS
    a = resolve_a(host)
    if not a:
        add("dns_nxdomain")
        return _result(slug, host, runtime, fails, {"a": []})

    # 2 gateway drift: an A record in the accepted union passes (built-in default,
    # edges file, per-vantage extras); one outside it is drift. args without an
    # `edge_table` (older callers, tests) get the built-in layer only.
    edges = getattr(args, "edge_table", None) or default_edge_table()
    want = edges.accepted(svc)
    if want and not (a & want):
        add("gateway_drift")

    # 3-6 cert
    verified, san, na_epoch, cert_err = inspect_cert(host, args.timeout)
    if not verified:
        add("dns_nxdomain" if cert_err.startswith("DNS:") else "tls_untrusted")
    if san and not san_covers(san, host):
        add("cert_host_mismatch")
    exp_san = expected_san_from_cert_domain(svc.get("cert_domain", ""))
    if exp_san and san and exp_san.lower() not in [s.lower() for s in san]:
        add("cert_registry_mismatch")
    if na_epoch is not None:
        if na_epoch < time.time() + args.expiry_days * 86400:
            add("cert_expiring")

    # 7-10 health  (use the registry's health_url when it declares one)
    health_url = svc.get("health_url") or url.rstrip("/") + "/health"
    hcode, hfinal, hbody = curl(health_url, args.timeout, want_body=True)
    hhost = urlsplit(hfinal).hostname or ""
    if hhost and hhost != (urlsplit(health_url).hostname or host):
        add("offsite_redirect")
    if hcode in (401, 403):
        add("health_forbidden")
    elif hcode == 404:
        add("health_notfound")
    elif hcode != 200:
        add("health_status")          # 000 / 5xx / unexpected → real outage
    else:
        if not health_body_ok(hbody):
            add("health_body")
    if any(fp in hbody.lower() for fp in FALLBACK_FINGERPRINTS):
        add("fallback_vhost")

    # 11 example path — the point is "a request reaches the app", not "the
    # registered example is still accurate". Any <500 same-host response proves
    # the edge is wired; only 5xx / 000 / an off-host redirect is an edge fault.
    ex = svc.get("example_path") or svc.get("example_url")
    if ex:
        ex_url = ex if ex.startswith("http") else url.rstrip("/") + "/" + ex.lstrip("/")
        ecode, efinal, _ = curl(ex_url, args.timeout, want_body=False)
        ehost = urlsplit(efinal).hostname or ""
        if ecode == 0 or ecode >= 500 or (ehost and ehost != host):
            add("example_unreachable")

    meta = {"a": sorted(a), "san": san, "health_code": hcode, "health_final": hfinal,
            "cert_err": cert_err, "want": sorted(want)}
    return _result(slug, host, runtime, fails, meta)


def _result(slug, host, runtime, fails, meta):
    hard = [f for f in fails if SEVERITY.get(f, "fail") == "fail"]
    return {
        "slug": slug, "host": host, "runtime": runtime,
        "fails": fails, "hard_fail": bool(hard),
        "ok": not fails, "meta": meta,
    }


def notify_ntfy(url: str, newly_broken, recovered, notes=()):
    lines = list(notes)
    if newly_broken:
        lines.append("🔴 NEW edge failures:")
        for r in newly_broken:
            lines.append(f"  {r['slug']} ({r['host']}): {', '.join(r['fails'])}")
    if recovered:
        lines.append("🟢 recovered: " + ", ".join(r["slug"] for r in recovered))
    if not lines:
        return
    try:
        # allow https://user:pass@host/topic
        parts = urlsplit(url)
        headers = {"Title": "fleet-edge-probe", "Priority": "high", "Tags": "warning"}
        if parts.username:
            import base64
            tok = base64.b64encode(f"{parts.username}:{parts.password or ''}".encode()).decode()
            headers["Authorization"] = f"Basic {tok}"
            url = parts._replace(netloc=parts.hostname + (f":{parts.port}" if parts.port else "")).geturl()
        req = urllib.request.Request(url, data="\n".join(lines).encode(), method="POST", headers=headers)
        urllib.request.urlopen(req, timeout=10)
    except OSError as e:
        log(f"ntfy post failed: {e}")
    except Exception as e:  # a malformed URL must not crash the run, nor be echoed (it may carry user:pass)
        log(f"ntfy post failed: {e.__class__.__name__}")


PROM_HELP = {
    "fleet_edge_probe_up": "1 if the probe ran to completion",
    "fleet_edge_probe_service_ok": "1 if the service passed all edge assertions",
    "fleet_edge_probe_service_hard_fail": "1 if the service has a page-worthy edge failure",
    "fleet_edge_probe_fail_code": "1 per (service, assertion code) that failed",
    "fleet_edge_probe_hard_fail_total": "count of services with a hard edge failure",
    "fleet_edge_probe_service_skipped": "1 per service not probed because its registry lifecycle.status is sunset (label lifecycle)",
    "fleet_edge_probe_skipped_total": "count of services skipped because of registry lifecycle",
    "fleet_edge_probe_eligible_services": "count of services this vantage actually probes (0 = this vantage is blind)",
    "fleet_edge_probe_last_run": "unix ts of the last completed run",
}


def write_prom(path: str, results: list[dict], vantage: str, eligible: int | None = None, up: int = 1):
    def esc(s):
        return str(s).replace("\\", "\\\\").replace('"', '\\"')
    hard = sum(1 for r in results if r["hard_fail"])
    lines = []
    for m, h in PROM_HELP.items():
        lines.append(f"# HELP {m} {h}")
        lines.append(f"# TYPE {m} gauge")
    v = f'vantage="{esc(vantage)}"'
    if eligible is None:
        eligible = sum(1 for r in results if not r.get("skipped"))
    lines.append(f"fleet_edge_probe_up{{{v}}} {up}")
    lines.append(f"fleet_edge_probe_eligible_services{{{v}}} {eligible}")
    lines.append(f"fleet_edge_probe_last_run{{{v}}} {int(time.time())}")
    lines.append(f"fleet_edge_probe_hard_fail_total{{{v}}} {hard}")
    lines.append(f"fleet_edge_probe_skipped_total{{{v}}} {sum(1 for r in results if r.get('skipped'))}")
    for r in results:
        lbl = f'{v},slug="{esc(r["slug"])}",host="{esc(r["host"])}",runtime="{esc(r["runtime"])}"'
        if r.get("skipped"):
            # not probed: no service_ok claim; keep it visible and explicit
            lines.append(f'fleet_edge_probe_service_skipped{{{lbl},lifecycle="{esc(r.get("lifecycle", ""))}"}} 1')
            lines.append(f"fleet_edge_probe_service_hard_fail{{{lbl}}} 0")
            continue
        lines.append(f"fleet_edge_probe_service_ok{{{lbl}}} {1 if r['ok'] else 0}")
        lines.append(f"fleet_edge_probe_service_hard_fail{{{lbl}}} {1 if r['hard_fail'] else 0}")
        for code in r["fails"]:
            sev = SEVERITY.get(code, "fail")
            lines.append(f'fleet_edge_probe_fail_code{{{lbl},code="{esc(code)}",severity="{sev}"}} 1')
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Strict external edge prober for the fleet.")
    ap.add_argument("--services", default=None,
                    help="path or URL to services.json (default: local checkout, else raw GitHub)")
    ap.add_argument("--only", default="", help="comma-separated slugs to probe")
    ap.add_argument("--sample", type=int, default=0, help="probe only the first N eligible services")
    ap.add_argument("--runtime", default="",
                    help="filter: one runtime or a comma list (compose,coolify; `external` entries "
                         "are never probed), or `all`/empty for every probeable runtime")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--expiry-days", type=int, default=14)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--json", action="store_true", help="emit JSON report to stdout")
    ap.add_argument("--all", action="store_true", help="human mode: also list passing services")
    ap.add_argument("--state", default=None, help="state file for alert-on-transition")
    ap.add_argument("--ntfy", default=os.environ.get("EDGE_PROBE_NTFY", ""),
                    help="ntfy topic URL (https://[user:pass@]host/topic) to POST "
                         "newly-broken/recovered and nothing-eligible transitions (only with --state)")
    ap.add_argument("--prom", default=os.environ.get("EDGE_PROBE_PROM", ""),
                    help="write Prometheus textfile-collector metrics to this path "
                         "(e.g. /var/lib/prometheus/node-exporter/fleet_edge_probe.prom)")
    ap.add_argument("--vantage", default=os.environ.get("EDGE_PROBE_VANTAGE", socket.gethostname()),
                    help="label for where this probe ran (default: hostname)")
    ap.add_argument("--edges", default=os.environ.get(EDGES_ENV, ""),
                    help="edges data file (JSON; schema in the README) adding accepted public edge "
                         f"IPs (default: ${EDGES_ENV}, else fleet-edge-probe.edges.json next to the "
                         "script if present)")
    ap.add_argument("--gateway-extra", default=os.environ.get(GATEWAY_EXTRA_ENV, ""),
                    help="comma-separated extra accepted A-record addresses for THIS vantage, e.g. "
                         f"the internal split-horizon gateway address (default: ${GATEWAY_EXTRA_ENV})")
    args = ap.parse_args()

    try:
        args.edge_table = build_edge_table(args.edges, args.gateway_extra)
    except EdgeConfigError as e:
        log(f"edge configuration error: {e}")
        return EXIT_USAGE

    src = args.services
    if not src:
        for p in ("services.json",
                  os.path.join(os.path.dirname(__file__), "..", "services.json")):
            if os.path.exists(p):
                src = p
                break
        src = src or REGISTRY_URL
    try:
        reg = load_registry(src)
    except Exception as e:
        log(f"cannot load registry from {src}: {e}")
        return EXIT_USAGE
    if not isinstance(reg, list):
        log(f"registry {src} is not a JSON list of services")
        return EXIT_USAGE

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    runtimes = parse_runtime_filter(args.runtime)
    elig, detail = select_services(reg, runtimes, only, args.sample)
    for rt, n in detail["requested_runtime_counts"].items():
        if n == 0:
            log(f"warning: --runtime {rt} matches 0 services in this registry")

    log(f"probing {len(elig)} services (timeout={args.timeout}s, concurrency={args.concurrency}) ...")
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(probe_or_skip, s, args): s for s in elig}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: (r["ok"], r["slug"]))

    broken = [r for r in results if not r["ok"]]
    hard = [r for r in results if r["hard_fail"]]
    skipped = [r for r in results if r.get("skipped")]
    eligible = len(results) - len(skipped)  # services actually probed
    nothing = eligible == 0
    if nothing:
        # the filters left nothing to probe: a failure, never a quiet success
        log("NOTHING ELIGIBLE: " + describe_selection(detail))

    ts = datetime.now(timezone.utc).isoformat()
    if args.json:
        report = {"ts": ts, "probed": eligible, "eligible": eligible,
                  "skipped": len(skipped),
                  "broken": len(broken), "hard_fail": len(hard)}
        if nothing:
            report["nothing_eligible"] = True
            report["detail"] = detail
        report["results"] = results
        print(json.dumps(report, indent=2))
    else:
        for r in results:
            if r.get("skipped"):
                print(f"SKIP  {r['slug']:<34} {r['host']:<40} {r['runtime']:<8}  [lifecycle={r['lifecycle']}]")
                continue
            if r["ok"] and not args.all:
                continue
            tag = "PASS" if r["ok"] else ("FAIL" if r["hard_fail"] else "WARN")
            extra = ""
            if not r["ok"]:
                m = r["meta"]
                extra = f"  [{', '.join(r['fails'])}]"
                if m.get("cert_err"):
                    extra += f"  cert={m['cert_err']}"
                if "offsite_redirect" in r["fails"]:
                    extra += f"  ->{m.get('health_final')}"
                if "gateway_drift" in r["fails"]:
                    extra += f"  A={m.get('a')} want={m.get('want')}"
                if m.get("san"):
                    extra += f"  SAN={m['san'][:3]}"
            print(f"{tag:4}  {r['slug']:<34} {r['host']:<40} {r['runtime']:<8}{extra}")
        print(f"\n{ts}  probed={len(results)-len(skipped)}  "
              f"ok={len(results)-len(broken)-len(skipped)}  "
              f"warn={len(broken)-len(hard)}  fail={len(hard)}  skipped={len(skipped)}")

    # alert-on-transition (state first, then the textfile, then the network: a hung or
    # failing ntfy POST must never keep the metric from being written)
    notes: list[str] = []
    newly_broken, recovered = [], []
    if args.state:
        prev, prev_nothing = {}, False
        if os.path.exists(args.state):
            try:
                with open(args.state) as f:
                    doc = json.load(f)
                prev = {r["slug"]: r for r in doc.get("results", [])}
                prev_nothing = bool(doc.get("nothing_eligible"))
            except Exception:
                prev, prev_nothing = {}, False
        newly_broken = [r for r in broken if prev.get(r["slug"], {}).get("ok", True)]
        recovered = [r for r in results if r["ok"] and not r.get("skipped")
                     and prev.get(r["slug"]) and not prev[r["slug"]]["ok"]]
        state_doc = {"ts": ts, "eligible": eligible, "nothing_eligible": nothing, "results": results}
        if nothing:
            state_doc["detail"] = detail
        try:  # atomic: a torn state file would repeat every alert on the next run
            with open(args.state + ".tmp", "w") as f:
                json.dump(state_doc, f, indent=2)
            os.replace(args.state + ".tmp", args.state)
        except OSError as e:
            log(f"cannot write state {args.state}: {e}")
        if newly_broken:
            log(f"NEW failures: {[r['slug'] for r in newly_broken]}")
        if recovered:
            log(f"recovered: {[r['slug'] for r in recovered]}")
        if nothing and not prev_nothing:
            notes.append(f"🟠 edge probe on {args.vantage} has NOTHING to probe and is blind "
                         f"(one alert per transition): {describe_selection(detail)}")
        elif prev_nothing and not nothing:
            notes.append(f"🟢 edge probe on {args.vantage} probes {eligible} services again")

    if args.prom:
        try:
            write_prom(args.prom, results, args.vantage, eligible=eligible, up=0 if nothing else 1)
        except OSError as e:
            log(f"cannot write prom textfile {args.prom}: {e}")

    if args.state and args.ntfy and (newly_broken or recovered or notes):
        notify_ntfy(args.ntfy, newly_broken, recovered, notes)

    if nothing:
        return EXIT_NOTHING_ELIGIBLE
    return EXIT_FAIL if hard else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
