#!/usr/bin/env python3
"""
fleet-container-audit — dockerhost-side health/port audit, driven by services.json.

Complements fleet-edge-probe (which sees the PUBLIC edge). This runs ON a
dockerhost and catches what the edge probe is slow or blind to:

  no_container    no container found for a registered service
  not_running     container exists but State != running
  restarting      container is in a restart loop (crash-loop)  ← python-proxy 2026-09
  restart_storm   RestartCount very high (crash-looped a lot)
  unhealthy       docker HEALTHCHECK == unhealthy
  port_unbound    the registered host_port is bound by nothing
  port_foreign    the registered host_port is bound by a DIFFERENT container
                  / process than this service, and not a registered $external
                  ← fleet-cert-watch vs hermes-agent tunnel 2026-09

Emits one summary row + one row per finding to an OpenObserve stream
(default `fleet_container_audit`), and optionally a node_exporter textfile.
Exit 0 = clean, 1 = >=1 finding, 2 = usage error.

stdlib only. Needs: python3, docker (CLI, no sudo), curl-free (urllib).
"""
from __future__ import annotations
import argparse, base64, json, os, re, socket, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

REGISTRY_URL = "https://raw.githubusercontent.com/baditaflorin/services-registry/main/services.json"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load_registry(src):
    if src.startswith("http"):
        with urllib.request.urlopen(src, timeout=30) as r:
            return json.load(r)
    with open(src) as f:
        return json.load(f)


def dsh(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


# NB: `docker ps --format "{{json .}}"` is O(n) API calls and times out on a
# host with hundreds of containers — use an explicit tab format instead.
_PS_FMT = "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}"


def docker_ps():
    """All containers with the fields we need (fast, explicit format)."""
    p = dsh("ps", "-a", "--format", _PS_FMT)
    if p.returncode != 0:
        log(f"docker ps failed: {p.stderr.strip()}")
        sys.exit(2)
    rows = []
    for line in p.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        rows.append({"Names": f[0], "State": f[1], "Status": f[2],
                     "Ports": f[3], "Image": f[4]})
    return rows


_HEALTH_RE = re.compile(r"\((health: starting|healthy|unhealthy)\)")
_RESTART_RE = re.compile(r"Restarting \((\d+)\)")


def container_state(c):
    """Derive status/health/restarts from `docker ps` State+Status (no inspect)."""
    status_txt = c.get("Status", "")
    hm = _HEALTH_RE.search(status_txt)
    rm = _RESTART_RE.search(status_txt)
    return {
        "status": (c.get("State") or "").lower(),      # running/exited/created/restarting/...
        "health": hm.group(1) if hm else "",
        "restarts": int(rm.group(1)) if rm else 0,
        "status_txt": status_txt,
    }


PORT_RE = re.compile(r"(?:0\.0\.0\.0|\[::\]|127\.0\.0\.1|[\d.]+):(\d+)->")


def published_ports(ports_str):
    return {int(m) for m in PORT_RE.findall(ports_str or "")}


_LISTEN_RE = re.compile(r'(?:0\.0\.0\.0|\[::\]|\*|127\.0\.0\.1|[\d.]+):(\d+)\s')
_PROC_RE = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')


def host_listeners():
    """host_port -> 'comm(pid)' for every listening socket (ss), docker included."""
    out = {}
    try:
        p = subprocess.run(["ss", "-tlnpH"], capture_output=True, text=True, timeout=15)
        for line in p.stdout.splitlines():
            pm = _LISTEN_RE.search(line)
            if not pm:
                continue
            proc = _PROC_RE.search(line)
            label = f"{proc.group(1)}({proc.group(2)})" if proc else "?"
            out.setdefault(int(pm.group(1)), label)
    except Exception:
        pass
    return out


def candidate_names(slug):
    return {slug, f"go-{slug}", f"go_{slug}", slug.replace("-", "_"),
            f"{slug}-app-1", f"{slug}_app_1", f"{slug}-app", f"{slug}-1"}


def audit(reg, args):
    hostname = socket.gethostname()
    containers = docker_ps()
    by_name = {c["Names"].lstrip("/"): c for c in containers}
    insp = {n: container_state(c) for n, c in by_name.items()}
    # host_port -> container name (from published ports)
    port_owner = {}
    for c in containers:
        for hp in published_ports(c.get("Ports", "")):
            port_owner.setdefault(hp, c["Names"].lstrip("/"))
    listeners = host_listeners()

    external_ports = set()
    for e in reg:
        if e.get("runtime") == "external" or str(e.get("id", "")).startswith("hermes-agent-tunnel"):
            for k in ("host_port", "container_port"):
                if e.get(k):
                    external_ports.add(int(e[k]))

    findings = []
    checked = 0
    for e in reg:
        if e.get("kind") != "container":
            continue
        if e.get("runtime") not in (None, "compose"):
            continue  # coolify / external audited elsewhere / not here
        if e.get("network_exposure") == "internal":
            pass  # still worth auditing the container; keep
        slug = e["id"]
        hp = e.get("host_port")
        checked += 1

        # locate the container
        cname = None
        if hp and hp in port_owner:
            cname = port_owner[hp]
        if not cname:
            for n in candidate_names(slug):
                if n in by_name:
                    cname = n
                    break

        def add(check, detail="", severity="fail"):
            findings.append({"slug": slug, "check": check, "detail": detail,
                             "severity": severity,
                             "container": cname or "", "host_port": hp})

        if not cname:
            lis = listeners.get(hp) if hp else None
            if hp and hp in external_ports:
                pass  # registered $external squatter, expected
            elif lis and not lis.startswith("docker-proxy"):
                add("port_foreign", f"host_port {hp} held by {lis}, no container for {slug}")
            else:
                add("no_container", "nothing on the port" if hp and not lis
                    else "no container matched")
            continue

        st = insp.get(cname, {})
        status = st.get("status", "")
        if status == "restarting":
            add("restarting", f"{cname} restarting (RestartCount={st.get('restarts')})")
        elif status not in ("running",):
            add("not_running", f"{cname} state={status}")
        elif st.get("health") == "unhealthy":
            add("unhealthy", f"{cname} HEALTHCHECK unhealthy")
        elif st.get("restarts", 0) >= args.restart_storm:
            add("restart_storm", f"{cname} RestartCount={st['restarts']}")

        # image pin — ADR-0028: prod pins :<short-sha>. A running :latest or a
        # stale :<semver> is a hand `docker compose up` / drift that no deploy
        # gate saw (the "I pushed so it's deployed" anti-pattern that let
        # python-proxy crash-loop for 3 weeks).
        img = by_name.get(cname, {}).get("Image", "")
        tag = img.rsplit(":", 1)[1] if ":" in img.rsplit("/", 1)[-1] else ""
        if tag == "latest":
            add("image_tag_latest", f"{cname} runs {img} — ADR-0028 wants :<short-sha>",
                severity="warn")
        elif re.fullmatch(r"v?\d+\.\d+\.\d+", tag) and e.get("version") and tag.lstrip("v") != str(e["version"]):
            add("image_version_mismatch", f"{cname} runs :{tag}, registry version={e['version']}",
                severity="warn")

        # port ownership — only trust the docker-proxy mapping here. A running
        # matched container with no mapping is normally host-networked and binds
        # the port itself; ss can't tell that apart without sudo, so don't guess.
        if hp:
            owner = port_owner.get(hp)
            if owner and owner != cname:
                add("port_foreign", f"host_port {hp} bound by {owner}, expected {cname}")

    bad = sum(1 for f in findings if f.get("severity", "fail") == "fail")
    warn = len(findings) - bad
    return {
        "ts": datetime.now(timezone.utc).isoformat(), "host": hostname,
        "checked": checked, "findings": findings, "bad": bad, "warn": warn,
    }


def push_oo(url, auth, result):
    rows = [{"kind": "summary", "host": result["host"], "ts": result["ts"],
             "checked": result["checked"], "bad": result["bad"],
             "warn": result.get("warn", 0)}]
    for f in result["findings"]:
        rows.append({"kind": "finding", "host": result["host"], "ts": result["ts"], **f})
    req = urllib.request.Request(url, data=json.dumps(rows).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log(f"oo: {r.status} rows={len(rows)}")
    except Exception as ex:
        log(f"oo push failed: {ex}")


def write_prom(path, result):
    h = result["host"]
    lines = [
        "# HELP fleet_container_audit_bad_total findings this run",
        "# TYPE fleet_container_audit_bad_total gauge",
        f'fleet_container_audit_bad_total{{host="{h}"}} {result["bad"]}',
        "# HELP fleet_container_audit_last_run unix ts of last run",
        "# TYPE fleet_container_audit_last_run gauge",
        f'fleet_container_audit_last_run{{host="{h}"}} {int(time.time())}',
        "# HELP fleet_container_bad 1 per (slug,check) finding",
        "# TYPE fleet_container_bad gauge",
    ]
    for f in result["findings"]:
        lines.append(
            f'fleet_container_bad{{host="{h}",slug="{f["slug"]}",'
            f'check="{f["check"]}",severity="{f.get("severity", "fail")}"}} 1')
    tmp = path + ".tmp"
    open(tmp, "w").write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--services", default=os.environ.get("CA_SERVICES", REGISTRY_URL))
    ap.add_argument("--oo-url", default=os.environ.get("CA_OO_URL", ""))
    ap.add_argument("--oo-auth", default=os.environ.get("CA_OO_AUTH", ""))
    ap.add_argument("--prom", default=os.environ.get("CA_PROM", ""))
    ap.add_argument("--restart-storm", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        reg = load_registry(args.services)
    except Exception as e:
        log(f"cannot load registry: {e}")
        return 2

    result = audit(reg, args)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for f in result["findings"]:
            tag = "BAD " if f.get("severity", "fail") == "fail" else "warn"
            print(f"{tag} {f['slug']:<32} {f['check']:<20} {f['detail']}")
        print(f"\n{result['ts']}  host={result['host']}  checked={result['checked']}  "
              f"bad={result['bad']}  warn={result.get('warn', 0)}")

    if args.oo_url:
        push_oo(args.oo_url, args.oo_auth, result)
    if args.prom:
        try:
            write_prom(args.prom, result)
        except OSError as e:
            log(f"prom write failed: {e}")

    return 1 if result["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())
