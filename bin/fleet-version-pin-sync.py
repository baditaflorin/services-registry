#!/usr/bin/env python3
"""fleet-version-pin-sync: keep the registry's hand-pinned `version` equal to what is running.

Why. The registry `version` of a service lives in overrides.json and bin/generate.py copies it from there and
nowhere else. `fleet-runner bump-version` never touches it, so every release leaves the pin behind (on 2026-10-16:
157 of 387 container services were stale and 96 had no version at all). go-fleet-preflight then reports version skew
and checks GHCR for the wrong image tag, and `fleet-runner deploy-pool` refuses a service without a version.

What a run does (one at a time, flock in the unit; PAUSE file in the state directory = do nothing):
  1. a fresh checkout of services-registry main in its own work directory (never the shared /root/workspace one);
  2. for every container service (not external, not sunset, repository under github.com/baditaflorin): the version
     of the repository's main service.yaml (batched GraphQL through `gh`) and the version the running service
     reports on GET /version (the LAN docker hosts first for compose services, else the public URL);
  3. a pin is CHANGED only when the running version equals main (a release that is both merged and deployed; a
     composite-runner service reporting the runner's version, an undeployed release or a deploy from a branch never
     qualifies) and is higher than the pin (a pin is never lowered: CI rejects that too);
     a pin that EXISTS is raised only when ghcr.io/baditaflorin/<id or repo>:<version> exists (the preflight image
     check, which deploy-pool placement treats as blocking, must not turn red); a MISSING pin is added either way
     (no version is already the worse failure);
  4. --apply: set those pins in overrides.json (in place, every other byte kept), `generate.py --only <ids>`,
     assert services.json changed in exactly those rows and only in `version`, run the version-regression check and
     the unit tests, then branch, commit, push, open a pull request, wait for its Woodpecker statuses and merge it
     pinned to the tested head. A red CI leaves the pull request open and the run fails.
Without --apply it only reports. A node_exporter textfile (--prom) says how many pins are ok/stale/missing and when
the last good run was.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/baditaflorin/services-registry.git"
GH_REPO = "baditaflorin/services-registry"
OWNER = "baditaflorin"
CI_CONTEXTS = ("ci/woodpecker/pr/woodpecker", "ci/woodpecker/push/woodpecker")
SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")
VERSION_LINE = re.compile(r'^version:\s*["\']?([^"\'\s#]+)', re.M)
PIN_LINE = re.compile(r'\n    "version": "[^"]*",?')


# ── pure helpers (unit-tested) ────────────────────────────────────────────────

def semver(value):
    """Comparable (major, minor, patch) or None for anything that is not plain SemVer."""
    m = SEMVER.fullmatch((value or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def parse_version_body(body):
    """The version a /version endpoint reports: {"version"} or the go-common {"data":{"version"}} envelope."""
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    v = doc.get("version")
    if v is None and isinstance(doc.get("data"), dict):
        v = doc["data"].get("version")
    if not isinstance(v, str) or not v.strip():
        return None
    return v.strip().lstrip("v")


def repo_name(repo_url):
    m = re.fullmatch(rf"https://github\.com/{OWNER}/([^/]+?)(?:\.git)?/?", (repo_url or "").strip())
    return m.group(1) if m else None


def candidates(services):
    """Registry rows the sync looks at."""
    out = []
    for e in services:
        if e.get("kind") != "container" or e.get("runtime") == "external":
            continue
        if (e.get("lifecycle") or {}).get("status") == "sunset":
            continue
        repo = repo_name(e.get("repo_url"))
        if repo:
            out.append({"id": e["id"], "repo": repo, "runtime": e.get("runtime"), "pin": e.get("version"),
                        "host_port": e.get("host_port"), "url": (e.get("url") or "").rstrip("/")})
    return out


def decide(row):
    """(action, reason) for one row carrying pin, main, live and image (True/False/None = not checked)."""
    pin, main, live = row.get("pin"), row.get("main"), row.get("live")
    if not main:
        return "skip", "repository main has no service.yaml version"
    if not live:
        return "skip", "running service did not report a version"
    if pin == live:
        return "ok", "pin equals the running version"
    if live != main:
        return "skip", f"running {live} is not repository main {main}"
    if semver(live) is None:
        return "skip", f"version {live!r} is not SemVer"
    if pin is not None:
        if semver(pin) is None:
            return "skip", f"pin {pin!r} is not SemVer"
        if semver(live) < semver(pin):
            return "skip", f"pin {pin} is ahead of the running {live} (never lowered)"
        if row.get("image") is not True:
            return "skip", f"ghcr.io has no image for {live}: raising the pin would turn the preflight image check red"
        return "raise", f"pin {pin} -> running {live}"
    return "add", f"no pin -> running {live}"


def set_pins(text, targets):
    """overrides.json text with `version` set for each id in targets, every other byte kept.

    The block is located with the JSON decoder (some blocks close on the same line as their last key), an existing
    `version` line is rewritten, a missing one is inserted first in the block. The parsed result must differ from the
    input only in those versions."""
    before = json.loads(text)
    for sid, ver in targets.items():
        head = f'\n  "{sid}": {{'
        if text.count(head) != 1:
            raise ValueError(f"{sid}: expected one top-level block, found {text.count(head)}")
        brace = text.index(head) + len(head) - 1
        _, end = json.JSONDecoder().raw_decode(text, brace)
        block = text[brace:end]
        lines = list(PIN_LINE.finditer(block))
        if len(lines) > 1:
            raise ValueError(f"{sid}: more than one version line")
        if lines:
            m = lines[0]
            new_line = re.sub(r'"version": "[^"]*"', f'"version": "{ver}"', m.group(0))
            block = block[:m.start()] + new_line + block[m.end():]
        else:
            if not block.startswith("{\n"):
                raise ValueError(f"{sid}: unexpected block layout")
            block = "{\n" + f'    "version": "{ver}",' + block[1:]
        text = text[:brace] + block + text[end:]
    after = json.loads(text)
    for sid in set(before) | set(after):
        a, b = before.get(sid), after.get(sid)
        if sid in targets:
            if b.get("version") != targets[sid] or {k: v for k, v in a.items() if k != "version"} != \
                    {k: v for k, v in b.items() if k != "version"}:
                raise ValueError(f"{sid}: edit changed more than its version")
        elif a != b:
            raise ValueError(f"{sid}: untouched block changed")
    return text


def version_only_change(old_services, new_services, targets):
    """Problems if services.json changed outside `version` of the target rows."""
    a = {e["id"]: e for e in old_services}
    b = {e["id"]: e for e in new_services}
    problems = []
    if set(a) != set(b):
        problems.append("the set of services changed")
    for sid in set(a) & set(b):
        if a[sid] == b[sid]:
            if sid in targets:
                problems.append(f"{sid}: row did not change")
            continue
        changed = {k for k in set(a[sid]) | set(b[sid]) if a[sid].get(k) != b[sid].get(k)}
        if sid not in targets or changed != {"version"} or b[sid].get("version") != targets[sid]:
            problems.append(f"{sid}: changed {sorted(changed)}")
    return problems


def stale_targets(problems, targets):
    """The target ids whose ONLY problem is that their generated row on main differs in more than `version` (`<id>: changed [...]`), or None when any other
    problem is present (a changed set of services, a row that did not change, a row outside the targets): those still stop the run. A target whose entry in
    services.json is stale against overrides.json (2026-10-20: six `trl_evidence` texts edited in overrides.json without regenerating) cannot be pinned by a
    pins-only change; skipping it must not hold back the other pins (one stale row stopped every sync for 12 hours)."""
    ids = set()
    for problem in problems:
        sid, sep, rest = problem.partition(": changed ")
        if not sep or sid not in targets or "'version'" in rest and rest.strip() == "['version']":
            return None
        ids.add(sid)
    return ids or None


def changelog_with(text, line, today):
    """CHANGELOG.md with `line` added under today's `## YYYY-MM-DD` heading (created when missing)."""
    head = f"## {today}\n\n"
    if text.startswith(head):
        return head + line + text[len(head):]
    return head + line + "\n" + text


# ── I/O ──────────────────────────────────────────────────────────────────────

def run(cmd, cwd=None, check=True, **kw):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, **kw)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}…: exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    return p


def git(workdir, *args, check=True):
    return run(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", *args],
               cwd=workdir, check=check)


def fresh_checkout(workdir):
    workdir = Path(workdir)
    if not (workdir / ".git").exists():
        workdir.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", REPO_URL, str(workdir)])
    git(workdir, "fetch", "--quiet", "origin", "main")
    # The work dir is this job's own, and a run that stops half-way (2026-10-20 18:21Z: the "services.json changed more than the pins" guard) leaves its
    # edits in it. `checkout -B main origin/main` refuses to run over them, so every later run died here for 12 hours: drop whatever is left FIRST.
    git(workdir, "reset", "--quiet", "--hard")
    git(workdir, "clean", "-fdq")
    git(workdir, "checkout", "--quiet", "-B", "main", "origin/main")
    git(workdir, "reset", "--quiet", "--hard", "origin/main")
    git(workdir, "clean", "-fdq")


def main_versions(repos):
    """{repo: version or None} from each repository's HEAD:service.yaml, 50 repositories per GraphQL call."""
    out = {}
    repos = sorted(set(repos))
    for i in range(0, len(repos), 50):
        batch = repos[i:i + 50]
        parts = [f'r{j}: repository(owner: "{OWNER}", name: {json.dumps(n)}) '
                 '{ object(expression: "HEAD:service.yaml") { ... on Blob { text } } }' for j, n in enumerate(batch)]
        p = run(["gh", "api", "graphql", "-f", "query=query {" + " ".join(parts) + "}"], check=False)
        data = (json.loads(p.stdout or "{}").get("data") or {}) if p.stdout else {}
        for j, name in enumerate(batch):
            blob = ((data.get(f"r{j}") or {}).get("object") or {}).get("text") or ""
            m = VERSION_LINE.search(blob)
            out[name] = m.group(1) if m else None
    return out


def http_version(url, timeout):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fleet-version-pin-sync"}),
                                    timeout=timeout) as r:
            return parse_version_body(r.read(4096).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def live_version(row, direct_hosts):
    if row["runtime"] == "compose" and row.get("host_port"):
        for host in direct_hosts:
            v = http_version(f"http://{host}:{int(row['host_port'])}/version", 3)
            if v:
                return v
    if row.get("url", "").startswith("https://"):
        return http_version(row["url"] + "/version", 6)
    return None


def image_exists(names, version):
    for name in names:
        p = run(["timeout", "25", "docker", "manifest", "inspect", f"ghcr.io/{OWNER}/{name}:{version}"], check=False)
        if p.returncode == 0:
            return True
    return False


def wait_and_merge(workdir, number, head, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            st = json.loads(run(["gh", "api", f"repos/{GH_REPO}/commits/{head}/status"]).stdout)
        except RuntimeError as exc:
            # The GitHub REST budget is shared by every agent and tool of the account and runs dry several times a day (2026-10-21 07:31Z): one refused status call
            # killed the run with its pull request open, and every later run then refused to start. A failed look is not a failed CI: look again after a pause.
            print(f"could not read the CI status of #{number} ({str(exc)[-160:]}); retrying", file=sys.stderr)
            time.sleep(120)
            continue
        states = {s["context"]: s["state"] for s in st.get("statuses", [])}
        if any(states.get(c) in ("failure", "error") for c in CI_CONTEXTS):
            raise RuntimeError(f"CI failed on #{number}: {states}")
        if all(states.get(c) == "success" for c in CI_CONTEXTS):
            run(["gh", "pr", "merge", str(number), "-R", GH_REPO, "--squash", "--match-head-commit", head,
                 "--delete-branch"], cwd=workdir)
            return
        time.sleep(30)
    raise RuntimeError(f"CI on #{number} did not finish within {timeout_s}s; the pull request stays open")


def write_prom(path, counts, applied, ok):
    if not path:
        return
    lines = ["# HELP fleet_version_pins Registry version pins by state (fleet-version-pin-sync).",
             "# TYPE fleet_version_pins gauge"]
    for state, n in sorted(counts.items()):
        lines.append(f'fleet_version_pins{{state="{state}"}} {n}')
    lines += ["# TYPE fleet_version_pin_sync_applied gauge", f"fleet_version_pin_sync_applied {applied}",
              "# TYPE fleet_version_pin_sync_success gauge", f"fleet_version_pin_sync_success {1 if ok else 0}",
              "# TYPE fleet_version_pin_sync_last_run_timestamp_seconds gauge",
              f"fleet_version_pin_sync_last_run_timestamp_seconds {int(time.time())}"]
    target = Path(path)
    tmp = target.with_suffix(".prom.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    tmp.replace(target)


def resume_open_pr(workdir, pr, counts, a):
    """A pull request of this job is still open: a run died before it merged (CI still running, the GitHub budget gone). Do not open another on top of it: wait for CI on
    that exact head and merge it (the same gate as a fresh one), and let the next run compute what is left. Exit 3 when it cannot be merged (CI failed, no verdict in time)."""
    print(f"a version-pin-sync pull request is still open (#{pr['number']}): resuming it, not opening another", file=sys.stderr)
    try:
        wait_and_merge(workdir, pr["number"], pr["headRefOid"], a.ci_timeout)
    except RuntimeError as exc:
        write_prom(a.prom, counts, 0, False)
        print(f"#{pr['number']} stays open: {exc}", file=sys.stderr)
        return 3
    print(f"merged #{pr['number']} (resumed)")
    write_prom(a.prom, counts, 0, True)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workdir", default="/var/lib/fleet-version-pin-sync/repo")
    ap.add_argument("--state-dir", default="/var/lib/fleet-version-pin-sync")
    ap.add_argument("--direct-hosts", default="10.10.10.20,10.10.10.30")
    ap.add_argument("--apply", action="store_true", help="change the registry (default: report only)")
    ap.add_argument("--max-changes", type=int, default=250)
    ap.add_argument("--ci-timeout", type=int, default=3600)
    ap.add_argument("--prom", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if (Path(a.state_dir) / "PAUSE").exists():
        print("PAUSE present: nothing done")
        return 0
    fresh_checkout(a.workdir)
    wd = Path(a.workdir)
    services = json.loads((wd / "services.json").read_text())
    rows = candidates(services)
    mains = main_versions([r["repo"] for r in rows])
    hosts = [h.strip() for h in a.direct_hosts.split(",") if h.strip()]
    for r in rows:
        r["main"] = mains.get(r["repo"])
        r["live"] = live_version(r, hosts) if r["main"] else None
        r["image"] = None
        if r["main"] and r["live"] == r["main"] and r["pin"] not in (None, r["live"]):
            r["image"] = image_exists([r["id"], r["repo"]], r["live"])
        r["action"], r["reason"] = decide(r)
    counts = {}
    for r in rows:
        key = {"ok": "ok", "raise": "stale", "add": "missing"}.get(r["action"], "skipped")
        counts[key] = counts.get(key, 0) + 1
    targets = {r["id"]: r["live"] for r in rows if r["action"] in ("raise", "add")}
    report = {"targets": targets, "counts": counts,
              "skipped": {r["id"]: r["reason"] for r in rows if r["action"] == "skip"}}
    print(json.dumps(report, indent=1) if a.json else
          f"pins: {counts}; to change: {len(targets)} " + ", ".join(f"{k}={v}" for k, v in sorted(targets.items())))
    if not a.apply or not targets:
        write_prom(a.prom, counts, 0, True)
        return 0
    if len(targets) > a.max_changes:
        write_prom(a.prom, counts, 0, False)
        print(f"refusing: {len(targets)} changes is more than --max-changes {a.max_changes}", file=sys.stderr)
        return 2
    open_prs = [pr for pr in json.loads(run(["gh", "pr", "list", "-R", GH_REPO, "--state", "open", "--limit", "100",
                                             "--json", "number,headRefName,headRefOid"]).stdout)
                if pr["headRefName"].startswith("bot/version-pin-sync-")]
    if open_prs:
        return resume_open_pr(wd, open_prs[0], counts, a)
    ok = False
    try:
        old_services = services
        original_overrides = (wd / "overrides.json").read_text()
        skipped_stale = {}
        for _attempt in range(3):
            (wd / "overrides.json").write_text(set_pins(original_overrides, targets))
            run([sys.executable, "bin/generate.py", "--only", ",".join(sorted(targets))], cwd=wd)
            problems = version_only_change(old_services, json.loads((wd / "services.json").read_text()), targets)
            if not problems:
                break
            stale = stale_targets(problems, targets)
            if not stale or len(stale) >= len(targets):
                raise RuntimeError("services.json changed more than the pins: " + "; ".join(problems[:10]))
            for sid in sorted(stale):
                skipped_stale[sid] = targets.pop(sid)
            git(wd, "checkout", "--quiet", "--", ".")  # back to origin/main, then again for the remaining pins
            print("skipping " + ", ".join(sorted(stale)) + ": their generated rows on main are stale against overrides.json "
                  "(regenerate them first); the other pins go on", file=sys.stderr)
        else:
            raise RuntimeError("services.json still changed more than the pins after skipping stale rows: " + "; ".join(problems[:10]))
        run([sys.executable, "bin/check_service_version_regressions.py", "origin/main"], cwd=wd)
        run([sys.executable, "-m", "unittest", "discover", "-s", "bin", "-p", "test_*.py"], cwd=wd)
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        line = (f"- fleet-version-pin-sync: {len(targets)} registry version pin(s) set to the version the service runs, "
                "which is also its repository main: " + ", ".join(f"`{k}` {v}" for k, v in sorted(targets.items())) + ".\n")
        cl = wd / "CHANGELOG.md"
        cl.write_text(changelog_with(cl.read_text(), line, today))
        branch = f"bot/version-pin-sync-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%MZ')}"
        git(wd, "checkout", "--quiet", "-b", branch)
        git(wd, "add", "overrides.json", "services.json", "CHANGELOG.md")
        git(wd, "-c", "user.name=fleet-version-pin-sync", "-c", "user.email=fleet-version-pin-sync@users.noreply.github.com",
            "commit", "--quiet", "-m", f"fix(registry): sync {len(targets)} version pin(s) to the running version\n\n"
            "Set by fleet-version-pin-sync: each service runs this version and its repository main has it.\n\n"
            + "\n".join(f"- {k}: {r}" for k, r in sorted((r['id'], r['reason']) for r in rows if r['id'] in targets)))
        git(wd, "push", "--quiet", "origin", branch)
        head = git(wd, "rev-parse", "HEAD").stdout.strip()
        body = ("Automatic: `bin/fleet-version-pin-sync.py` on the Builder. Each pin below is set to the version the "
                "service reports on `/version`, which equals its repository main `service.yaml`; a pin that existed is "
                "raised only when ghcr.io has that image. services.json changes only `version` in these rows "
                "(asserted); the version-regression check and the unit tests passed before the push.\n\n"
                + "\n".join(f"- `{r['id']}`: {r['reason']}" for r in rows if r["id"] in targets)
                + ("\n\nNot synced (their generated row on main is stale against overrides.json: regenerate it, then the next run sets "
                   "the pin): " + ", ".join(f"`{k}` {v}" for k, v in sorted(skipped_stale.items())) if skipped_stale else "")
                + "\n\nMerged automatically when Woodpecker is green on this exact head.")
        url = run(["gh", "pr", "create", "-R", GH_REPO, "--base", "main", "--head", branch, "--title",
                   f"fix(registry): sync {len(targets)} version pin(s) to the running version", "--body", body],
                  cwd=wd).stdout.strip()
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        print(f"opened {url}")
        wait_and_merge(wd, number, head, a.ci_timeout)
        print(f"merged #{number}")
        ok = True
        return 0
    finally:
        write_prom(a.prom, counts, len(targets) if ok else 0, ok)


if __name__ == "__main__":
    sys.exit(main())
