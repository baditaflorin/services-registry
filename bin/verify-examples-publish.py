#!/usr/bin/env python3
"""verify-examples-publish: put the nightly example result into the registry's services.examples.json by pull request.

  verify-examples-publish.py --result FILE [--repo OWNER/NAME] [--workdir DIR] [--dry]

The hub (and anything else that reads the registry) takes example state from the committed services.examples.json. The nightly
check keeps its newest result on the builder; this publishes it, but only when something a reader can see changed:

  - a service's `state` or `replicas` differ, or a service appeared or disappeared (checked_at alone never opens a PR);
  - the run looks healthy: at least HEALTHY_SHARE of the services that were ok in the committed file are still ok or
    gated/no-data (a mass `unreachable` is a network or key problem on the builder, not 100 broken services);
  - no earlier bot/examples-* pull request is still open.

Then: fresh shallow clone, branch bot/examples-<UTC>, commit, push, PR, and `fleet-runner ci wait --merge squash` (it merges only
when CI passed). A red CI leaves the PR open and exits 1. Prints what changed, never a key. stdlib + git + gh + fleet-runner.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

HEALTHY_SHARE = 0.8
KEEP = ("ok", "gated", "no-data")


def states(doc):
    return {k: (v.get("state"), json.dumps(v.get("replicas") or [], sort_keys=True)) for k, v in (doc.get("services") or {}).items()}


def diff(old, new):
    """-> [(id, old state or None, new state or None)] for every service whose state or replicas differ."""
    a, b = states(old), states(new)
    return [(k, (a.get(k) or (None,))[0], (b.get(k) or (None,))[0]) for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def healthy(old, new):
    """False when the new run lost a large share of the services that were ok: do not publish that."""
    was_ok = [k for k, v in (old.get("services") or {}).items() if v.get("state") == "ok" and k in (new.get("services") or {})]
    if not was_ok:
        return True
    still = sum(1 for k in was_ok if new["services"][k].get("state") in KEEP)
    return still >= HEALTHY_SHARE * len(was_ok)


def tally(doc):
    t = {}
    for v in (doc.get("services") or {}).values():
        t[v.get("state")] = t.get(v.get("state"), 0) + 1
    return dict(sorted(t.items(), key=lambda kv: str(kv[0])))


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (" ".join(cmd[:3]), r.returncode, (r.stderr or r.stdout).strip()[-300:]))
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--result", required=True)
    ap.add_argument("--repo", default="baditaflorin/services-registry")
    ap.add_argument("--workdir", default="/var/lib/verify-examples/repo")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    new = json.load(open(a.result))
    prs = json.loads(run(["gh", "pr", "list", "-R", a.repo, "--state", "open", "--limit", "100", "--json", "number,headRefName"]).stdout)
    if any(p["headRefName"].startswith("bot/examples-") for p in prs):
        print("verify-examples-publish: an earlier bot/examples-* PR is still open: not opening another")
        return 0
    shutil.rmtree(a.workdir, ignore_errors=True)
    run(["git", "clone", "-q", "--depth", "1", "https://github.com/%s.git" % a.repo, a.workdir])
    target = os.path.join(a.workdir, "services.examples.json")
    old = json.load(open(target)) if os.path.exists(target) else {"services": {}}
    ch = diff(old, new)
    if not ch:
        print("verify-examples-publish: nothing a reader can see changed (%d services)" % len(new.get("services", {})))
        return 0
    if not healthy(old, new):
        print("verify-examples-publish: NOT publishing: the run lost more than %d%% of the services that were ok (tally %s): "
              "a builder network or key problem, not %d broken services" % (round((1 - HEALTHY_SHARE) * 100), tally(new), len(ch)))
        return 1
    print("verify-examples-publish: %d change(s): %s" % (len(ch), ", ".join("%s %s->%s" % c for c in ch[:12]) + (" ..." if len(ch) > 12 else "")))
    if a.dry:
        return 0
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%MZ")
    branch = "bot/examples-" + stamp
    run(["git", "checkout", "-q", "-b", branch], cwd=a.workdir)
    with open(target, "w") as f:
        f.write(json.dumps(new, indent=2) + "\n")
    day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    line = "- examples: services.examples.json refreshed by the nightly check (%s); %d state change(s)%s.\n" % (
        ", ".join("%s %s" % kv for kv in tally(new).items()), len(ch),
        ": " + ", ".join("`%s` %s->%s" % c for c in ch[:8]) + (", ..." if len(ch) > 8 else ""))
    cl = os.path.join(a.workdir, "CHANGELOG.md")
    text = open(cl).read()
    if text.startswith("## " + day):
        head, rest = text.split("\n", 1)
        text = head + "\n\n" + line + rest.lstrip("\n")
    else:
        text = "## %s\n\n%s\n%s" % (day, line, text)
    open(cl, "w").write(text)
    run(["git", "add", "services.examples.json", "CHANGELOG.md"], cwd=a.workdir)
    run(["git", "commit", "-q", "-m", "examples: nightly refresh (%d change(s))" % len(ch)], cwd=a.workdir)
    run(["git", "push", "-q", "origin", branch], cwd=a.workdir)
    url = run(["gh", "pr", "create", "-R", a.repo, "--base", "main", "--head", branch, "--title",
               "examples: nightly refresh (%d change(s))" % len(ch), "--body",
               "Automatic: the nightly example check (verify-examples-nightly on the builder) changed %d state(s):\n\n%s\n\nA run that lost most ok services is never published."
               % (len(ch), "\n".join("- `%s` %s -> %s" % c for c in ch[:40]))]).stdout.strip().splitlines()[-1]
    print("verify-examples-publish: opened", url)
    number = url.rstrip("/").rsplit("/", 1)[-1]
    r = run(["fleet-runner", "ci", "wait", "--repo", a.repo, "--pr", number, "--timeout", "9m", "--merge", "squash"], check=False)
    print("verify-examples-publish: ci wait exit %d" % r.returncode)
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
