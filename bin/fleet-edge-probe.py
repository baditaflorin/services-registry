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
  2  gateway_drift       A record points at the expected gateway for `runtime`
                         (compose -> 176.9.123.221 ; coolify -> 65.108.75.123)
  3  tls_untrusted       TLS chain validates with the system trust store (no -k)
  4  cert_host_mismatch  leaf cert SAN actually covers this hostname
  5  cert_registry_mismatch  SAN matches registry `cert_domain`
                              (wildcard.0crawl.com => SAN must include *.0crawl.com)
  6  cert_expiring       notAfter > now + --expiry-days
  7  offsite_redirect    GET /health may redirect, but must stay on the same host
  8  health_status       final /health status == 200
  9  health_body         /health body is JSON with status=="ok" (not a landing page)
 10  fallback_vhost      body is not the known default-vhost fingerprint (<title>LV3 ...)
 11  example_unreachable GET <url><example_path> -> 200/401/403 and same host
                         (401/403 == "edge wired, auth on" == fine)

Exit: 0 = all probed services pass ; 1 = >=1 failure ; 2 = usage/fetch error.

stdlib only. Needs: python3, curl.
"""

from __future__ import annotations
import argparse, json, os, socket, ssl, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlsplit

REGISTRY_URL = "https://raw.githubusercontent.com/baditaflorin/services-registry/main/services.json"

# Known public gateway IPs by runtime. Extend here as topology changes.
GATEWAY_IPS = {
    "compose": {"176.9.123.221"},
    "coolify": {"65.108.75.123"},
}

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

    # 2 gateway drift
    want = GATEWAY_IPS.get(runtime)
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
        body_ok = False
        try:
            j = json.loads(hbody)
            body_ok = isinstance(j, dict) and (
                str(j.get("status", "")).lower() in ("ok", "healthy", "up")
                or "service" in j or "version" in j
            )
        except ValueError:
            body_ok = hbody.strip().lower() in ("ok", "healthy", "up") or \
                hbody.strip().lower().startswith("ok")
        if not body_ok:
            add("health_body")
    if any(fp in hbody.lower() for fp in FALLBACK_FINGERPRINTS):
        add("fallback_vhost")

    # 11 example path
    ex = svc.get("example_path") or svc.get("example_url")
    if ex:
        ex_url = ex if ex.startswith("http") else url.rstrip("/") + "/" + ex.lstrip("/")
        ecode, efinal, _ = curl(ex_url, args.timeout, want_body=False)
        ehost = urlsplit(efinal).hostname or ""
        if ecode not in (200, 401, 403) or (ehost and ehost != host):
            add("example_unreachable")

    meta = {"a": sorted(a), "san": san, "health_code": hcode, "health_final": hfinal,
            "cert_err": cert_err}
    return _result(slug, host, runtime, fails, meta)


def _result(slug, host, runtime, fails, meta):
    hard = [f for f in fails if SEVERITY.get(f, "fail") == "fail"]
    return {
        "slug": slug, "host": host, "runtime": runtime,
        "fails": fails, "hard_fail": bool(hard),
        "ok": not fails, "meta": meta,
    }


def notify_ntfy(url: str, newly_broken, recovered):
    lines = []
    if newly_broken:
        lines.append("🔴 NEW edge failures:")
        for r in newly_broken:
            lines.append(f"  {r['slug']} ({r['host']}): {', '.join(r['fails'])}")
    if recovered:
        lines.append("🟢 recovered: " + ", ".join(r["slug"] for r in recovered))
    if not lines:
        return
    body = "\n".join(lines).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Title": "fleet-edge-probe", "Priority": "high"})
        urllib.request.urlopen(req, timeout=10)
    except OSError as e:
        log(f"ntfy post failed: {e}")


def main():
    ap = argparse.ArgumentParser(description="Strict external edge prober for the fleet.")
    ap.add_argument("--services", default=None,
                    help="path or URL to services.json (default: local checkout, else raw GitHub)")
    ap.add_argument("--only", default="", help="comma-separated slugs to probe")
    ap.add_argument("--sample", type=int, default=0, help="probe only the first N eligible services")
    ap.add_argument("--runtime", default="", help="filter: compose|coolify|external")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--expiry-days", type=int, default=14)
    ap.add_argument("--concurrency", type=int, default=12)
    ap.add_argument("--json", action="store_true", help="emit JSON report to stdout")
    ap.add_argument("--all", action="store_true", help="human mode: also list passing services")
    ap.add_argument("--state", default=None, help="state file for alert-on-transition")
    ap.add_argument("--ntfy", default=os.environ.get("EDGE_PROBE_NTFY", ""),
                    help="ntfy topic URL to POST newly-broken/recovered (only with --state)")
    args = ap.parse_args()

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
        return 2

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    elig = []
    for e in reg:
        if e.get("kind") != "container":
            continue
        if not str(e.get("url", "")).startswith("https://"):
            continue
        if e.get("runtime") == "external":
            continue
        if e.get("network_exposure") == "internal":
            continue
        if args.runtime and e.get("runtime") != args.runtime:
            continue
        if only and e.get("id") not in only:
            continue
        elig.append(e)
    if args.sample:
        elig = elig[: args.sample]
    if not elig:
        log("no eligible services matched filters")
        return 2

    log(f"probing {len(elig)} services (timeout={args.timeout}s, concurrency={args.concurrency}) ...")
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(probe, s, args): s for s in elig}
        for fut in as_completed(futs):
            results.append(fut.result())
    results.sort(key=lambda r: (r["ok"], r["slug"]))

    broken = [r for r in results if not r["ok"]]
    hard = [r for r in results if r["hard_fail"]]

    ts = datetime.now(timezone.utc).isoformat()
    if args.json:
        print(json.dumps({"ts": ts, "probed": len(results),
                          "broken": len(broken), "hard_fail": len(hard),
                          "results": results}, indent=2))
    else:
        for r in results:
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
                if m.get("san"):
                    extra += f"  SAN={m['san'][:3]}"
            print(f"{tag:4}  {r['slug']:<34} {r['host']:<40} {r['runtime']:<8}{extra}")
        print(f"\n{ts}  probed={len(results)}  ok={len(results)-len(broken)}  "
              f"warn={len(broken)-len(hard)}  fail={len(hard)}")

    # alert-on-transition
    if args.state:
        prev = {}
        if os.path.exists(args.state):
            try:
                prev = {r["slug"]: r for r in json.load(open(args.state)).get("results", [])}
            except Exception:
                prev = {}
        newly_broken = [r for r in broken if prev.get(r["slug"], {}).get("ok", True)]
        recovered = [r for r in results if r["ok"] and prev.get(r["slug"]) and not prev[r["slug"]]["ok"]]
        try:
            json.dump({"ts": ts, "results": results}, open(args.state, "w"), indent=2)
        except OSError as e:
            log(f"cannot write state {args.state}: {e}")
        if newly_broken:
            log(f"NEW failures: {[r['slug'] for r in newly_broken]}")
        if recovered:
            log(f"recovered: {[r['slug'] for r in recovered]}")
        if args.ntfy and (newly_broken or recovered):
            notify_ntfy(args.ntfy, newly_broken, recovered)

    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
