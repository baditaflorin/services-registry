#!/usr/bin/env python3
"""Compare the live Proxmox inventory (bin/proxmox_inventory.py) with the documentation. CPU only, deterministic, reads files.

Findings (exit status 1 when there is any):
  host_undocumented       a Proxmox host in the snapshot that no document mentions by its id
  host_count_mismatch     a document says "<N> Proxmox hosts/servers" and the snapshot has a different number
  guest_new_undocumented  a guest that is NEW since --previous (not a template) and that no document mentions by name or as "VM/CT <id>"
  host_unreadable         the snapshot could not read a host, so it could not be checked
Guests that existed in --previous are a baseline: the documents do not have to list every guest, only every guest that appears from now on.
An allowlist ({"guests": ["name or vmid", ...], "hosts": ["id", ...]}) silences findings that are deliberate.

usage: inventory_check.py --snapshot snap.json --doc OPS.md [--doc ...] [--previous prev.json] [--allow allow.json] [--json out.json]"""
import argparse, json, re, sys

NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
COUNT_RE = re.compile(r"\b(\w+)\s+(?:physical\s+)?Proxmox\s+(?:hosts|servers|hypervisors)\b", re.I)


def mentions(text, token):
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text, re.I) is not None


def guest_mentioned(text, g):
    return bool(g["name"]) and mentions(text, g["name"]) or re.search(r"\b(?:VM|CT|LXC|VMID)\s*`?%d\b" % g["vmid"], text, re.I) is not None


def check(snap, docs, previous=None, allow=None):
    text = "\n".join(docs)
    allow = allow or {}
    out = []
    hosts = snap.get("hosts", {})
    ok_hosts = {h: v for h, v in hosts.items() if "error" not in v}
    for h, v in hosts.items():
        if "error" in v:
            out.append(dict(kind="host_unreadable", host=h, detail=v["error"]))
    for h in ok_hosts:
        if h not in allow.get("hosts", []) and not mentions(text, h):
            out.append(dict(kind="host_undocumented", host=h, detail="no document mentions %r" % h))
    for m in COUNT_RE.finditer(text):
        w = m.group(1).lower()
        n = NUMBERS.get(w) or (int(w) if w.isdigit() else None)
        if n is not None and not any(f["kind"] == "host_unreadable" for f in out) and n != len(hosts):
            out.append(dict(kind="host_count_mismatch", detail="a document says %r, the inventory has %d" % (m.group(0), len(hosts))))
    if previous:
        for h, v in ok_hosts.items():
            old = {(g["type"], g["vmid"]) for g in (previous.get("hosts", {}).get(h, {}).get("guests", []))}
            had_host = h in previous.get("hosts", {}) and "error" not in previous["hosts"][h]
            for g in v["guests"]:
                if g["template"] or (g["type"], g["vmid"]) in old and had_host:
                    continue
                if str(g["vmid"]) in allow.get("guests", []) or g["name"] in allow.get("guests", []):
                    continue
                if not guest_mentioned(text, g):
                    out.append(dict(kind="guest_new_undocumented", host=h, guest="%s %d %s" % (g["type"], g["vmid"], g["name"]), detail="new since the previous snapshot and not in any document"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True); ap.add_argument("--doc", action="append", required=True); ap.add_argument("--previous"); ap.add_argument("--allow"); ap.add_argument("--json")
    a = ap.parse_args(argv)
    load = lambda p: json.load(open(p)) if p else None
    docs = [open(d, errors="replace").read() for d in a.doc]
    res = check(load(a.snapshot), docs, load(a.previous), load(a.allow))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(dict(findings=res), f, indent=1)
    for r in res:
        print("%-22s %s %s" % (r["kind"], r.get("host") or r.get("guest", ""), r["detail"]))
    print("inventory check: %d finding(s)" % len(res))
    return 1 if res else 0


if __name__ == "__main__":
    sys.exit(main())
