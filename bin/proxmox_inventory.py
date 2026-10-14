#!/usr/bin/env python3
"""Snapshot of what actually runs on the fleet's Proxmox hosts: host, Proxmox version, every VM and container (id, name, status, vCPUs, memory).
Read-only: one `ssh <alias>` per host that runs `pveversion`, `hostname` and `pvesh get` (no write verbs), so it can run unattended from any machine
whose SSH config has an alias for each host. The result is DATA for `bin/inventory_check.py`, which compares it with the documentation.

usage: proxmox_inventory.py snapshot --hosts hosts.json --out snapshot.json [--timeout 40]
hosts.json: [{"id": "0infer", "alias": "0infer_bastion"}, ...]   (aliases only: no addresses or secrets belong in it)
snapshot.json: {"taken_at": ISO8601, "hosts": {"<id>": {"hostname", "pve_version", "guests": [{"type", "vmid", "name", "status", "cpus", "maxmem_mb", "template"}]}  or  {"error": "..."}}}
Exit status 1 when a host could not be read (its entry carries the error; the others are still written)."""
import argparse, datetime, json, subprocess, sys

REMOTE = ('echo ---VERSION---; pveversion 2>&1 | head -1; echo ---HOST---; hostname; '
          'echo ---QEMU---; pvesh get /nodes/$(hostname)/qemu --output-format json 2>/dev/null; '
          'echo ---LXC---; pvesh get /nodes/$(hostname)/lxc --output-format json 2>/dev/null; echo ---END---')


def sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("---") and line.endswith("---") and len(line) > 6:
            cur = line.strip("-")
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def guests_from(kind, blob):
    if not blob:
        return []
    out = []
    for g in json.loads(blob):
        out.append(dict(type=kind, vmid=int(g["vmid"]), name=g.get("name") or "", status=g.get("status", ""), cpus=int(g.get("cpus") or g.get("maxcpu") or 0),
                        maxmem_mb=int((g.get("maxmem") or 0) // (1024 * 1024)), template=bool(g.get("template"))))
    return out


def parse(text):
    s = sections(text)
    if "HOST" not in s or not s["HOST"]:
        raise ValueError("no hostname in the output (not a Proxmox host, or ssh failed): " + text.strip()[:120])
    guests = guests_from("qemu", s.get("QEMU", "")) + guests_from("lxc", s.get("LXC", ""))
    return dict(hostname=s["HOST"].splitlines()[0].strip(), pve_version=s.get("VERSION", "").strip(), guests=sorted(guests, key=lambda g: (g["type"], g["vmid"])))


def ssh_run(alias, timeout):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, REMOTE], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError("ssh %s failed (%d): %s" % (alias, r.returncode, (r.stderr or "").strip().splitlines()[-1:] or ""))
    return r.stdout


def take(hosts, timeout=40, run=ssh_run):
    snap = dict(taken_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), hosts={})
    bad = 0
    for h in hosts:
        try:
            snap["hosts"][h["id"]] = parse(run(h["alias"], timeout))
        except Exception as e:  # one unreachable host must not hide the others
            snap["hosts"][h["id"]] = dict(error=str(e)[:200])
            bad += 1
    return snap, bad


def main(argv=None):
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("snapshot"); s.add_argument("--hosts", required=True); s.add_argument("--out", required=True); s.add_argument("--timeout", type=int, default=40)
    a = ap.parse_args(argv)
    with open(a.hosts) as f:
        hosts = json.load(f)
    snap, bad = take(hosts, a.timeout)
    with open(a.out, "w") as f:
        json.dump(snap, f, indent=1, sort_keys=True)
        f.write("\n")
    print("%d hosts, %d guests, %d unreadable" % (len(hosts), sum(len(h.get("guests", [])) for h in snap["hosts"].values()), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
