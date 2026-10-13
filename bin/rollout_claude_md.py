#!/usr/bin/env python3
"""Roll the canonical CLAUDE.md (services-registry) out to the fleet's repositories. Deterministic, no LLM.

Per repository it compares the blob of CLAUDE.md with the canonical's own history:
  current    identical to the canonical now                         -> nothing to do
  absent     no CLAUDE.md                                           -> add it
  behind     identical to an OLDER canonical revision (N behind)    -> replace it (nothing repo-specific can be lost)
  diverged   differs from every canonical revision                  -> skipped (needs a human or `docdrift sync` with a repo-local tail)
Commits carry "[skip ci]" so a docs-only commit does not start a pipeline in ~500 repositories. Git runs over SSH, so GitHub's API quota is not used.

usage:
  rollout_claude_md.py plan   --registry <clone> --out <dir> [--repos a,b,c | --repos-file f] [--workers 6]
  rollout_claude_md.py apply  --out <dir> [--limit N] [--only a,b] [--no-push]
plan clones every repository shallowly into <out>/clones and writes <out>/plan.json; apply commits and pushes from those clones.
A push to a protected default branch is retried as the branch docs/claude-md-sync-<sha> and recorded as needing a PR."""
import argparse, concurrent.futures as cf, json, os, subprocess, sys, threading

OWNER = "baditaflorin"
ID = ["-c", "user.name=Claude Code", "-c", "user.email=noreply@anthropic.com"]


def git(cwd, *a, check=True):
    r = subprocess.run(["git", "-C", cwd] + list(a), capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError("git %s: %s" % (" ".join(a[:3]), (r.stderr or r.stdout).strip()[:200]))
    return r.stdout.strip()


def canonical(reg):
    text = subprocess.run(["git", "-C", reg, "show", "origin/main:CLAUDE.md"], capture_output=True, text=True, check=True).stdout
    revs = git(reg, "log", "--format=%H", "origin/main", "--", "CLAUDE.md").split("\n")
    blobs = {}
    for i, c in enumerate(revs):  # newest first: i = how many canonical revisions behind
        b = git(reg, "rev-parse", c + ":CLAUDE.md", check=False)
        if b:
            blobs.setdefault(b, (i, c[:9]))
    head = git(reg, "rev-parse", "--short=9", "origin/main")
    return text, git(reg, "rev-parse", "origin/main:CLAUDE.md"), blobs, head


def classify_one(repo, out, cblob, blobs):
    d = os.path.join(out, "clones", repo)
    if not os.path.isdir(d):
        r = subprocess.run(["git", "clone", "--depth", "1", "--quiet", "git@github.com:%s/%s.git" % (OWNER, repo), d], capture_output=True, text=True)
        if r.returncode:
            return dict(repo=repo, status="clone-failed", note=r.stderr.strip()[:120])
    else:  # a clone from an earlier plan: bring it to the remote's newest tip and drop any local, unpushed commit
        br = git(d, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).replace("origin/", "") or "HEAD"
        f = subprocess.run(["git", "-C", d, "fetch", "-q", "--depth", "1", "origin", br], capture_output=True, text=True)
        if f.returncode:
            return dict(repo=repo, status="fetch-failed", note=f.stderr.strip()[:120])
        git(d, "reset", "-q", "--hard", "FETCH_HEAD")
    p = os.path.join(d, "CLAUDE.md")
    branch = git(d, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if not os.path.exists(p):
        return dict(repo=repo, status="absent", branch=branch)
    b = git(d, "hash-object", p)
    if b == cblob:
        return dict(repo=repo, status="current", branch=branch)
    if b in blobs:
        return dict(repo=repo, status="behind", behind=blobs[b][0], at=blobs[b][1], branch=branch)
    return dict(repo=repo, status="diverged", branch=branch)


def cmd_plan(a):
    text, cblob, blobs, head = canonical(a.registry)
    repos = a.repos.split(",") if a.repos else [l.strip() for l in open(a.repos_file).read().splitlines() if l.strip()]
    os.makedirs(os.path.join(a.out, "clones"), exist_ok=True)
    with cf.ThreadPoolExecutor(a.workers) as ex:
        rows = list(ex.map(lambda r: classify_one(r, a.out, cblob, blobs), repos))
    with open(os.path.join(a.out, "plan.json"), "w") as f:
        json.dump(dict(canonical_blob=cblob, canonical_head=head, blobs={k: v[0] for k, v in blobs.items()}, rows=rows), f, indent=1)
    with open(os.path.join(a.out, "CLAUDE.md.canonical"), "w") as f:
        f.write(text)
    t = {}
    for r in rows:
        t[r["status"]] = t.get(r["status"], 0) + 1
    print("canonical %s (blob %s): %s" % (head, cblob[:10], t))


def apply_one(r, out, text, head):
    d = os.path.join(out, "clones", r["repo"])
    with open(os.path.join(d, "CLAUDE.md"), "w") as f:
        f.write(text)
    git(d, "add", "CLAUDE.md")
    verb = "add" if r["status"] == "absent" else "refresh"
    msg = "docs(claude): %s CLAUDE.md from the services-registry canonical %s [skip ci]" % (verb, head)
    body = "Fleet agent brief, corrected against the code and data on 2026-10-12. Staleness of any copy is read from git: a copy that equals an older canonical revision is N revisions behind (docdrift scan).\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
    subprocess.run(["git", "-C", d] + ID + ["commit", "-q", "-m", msg, "-m", body], capture_output=True, text=True, check=True)
    return d


def resync(r, out, text, plan):
    """The remote branch moved on. Reset the clone to its newest tip and redo the classification; returns pushed | current | diverged | failed"""
    d = os.path.join(out, "clones", r["repo"])
    br = r.get("branch") or "HEAD"
    if subprocess.run(["git", "-C", d, "fetch", "-q", "--depth", "1", "origin", br], capture_output=True, text=True).returncode:
        return "failed"
    git(d, "reset", "-q", "--hard", "FETCH_HEAD")
    p = os.path.join(d, "CLAUDE.md")
    if os.path.exists(p):
        b = git(d, "hash-object", p)
        if b == plan["canonical_blob"]:
            return "current"
        if b not in plan.get("blobs", {}):
            return "diverged"
    apply_one(r, out, text, plan["canonical_head"])
    return "pushed" if subprocess.run(["git", "-C", d, "push", "-q", "origin", "HEAD"], capture_output=True, text=True).returncode == 0 else "failed"


def cmd_apply(a):
    with open(os.path.join(a.out, "plan.json")) as f:
        plan = json.load(f)
    with open(os.path.join(a.out, "CLAUDE.md.canonical")) as f:
        text = f.read()
    logp = os.path.join(a.out, "apply_log.jsonl")
    # a repository counts as done for THIS canonical only (log rows carry it), so a later canonical is rolled out again
    logged = [json.loads(l) for l in open(logp).read().splitlines() if l.strip()] if os.path.exists(logp) else []
    done = {x["repo"] for x in logged if x.get("result") in ("pushed", "branch-needs-pr", "skipped-current") and x.get("canonical") == plan["canonical_head"]}
    todo = [r for r in plan["rows"] if r["status"] in ("absent", "behind") and r["repo"] not in done]
    if a.only:
        todo = [r for r in todo if r["repo"] in a.only.split(",")]
    if a.exclude:
        todo = [r for r in todo if r["repo"] not in a.exclude.split(",")]
    if a.limit:
        todo = todo[:a.limit]

    def one(r):
        try:
            d = apply_one(r, a.out, text, plan["canonical_head"])
            if a.no_push:
                return dict(repo=r["repo"], result="committed-local")
            p = subprocess.run(["git", "-C", d, "push", "-q", "origin", "HEAD"], capture_output=True, text=True)
            if p.returncode and ("fetch first" in p.stderr or "non-fast-forward" in p.stderr):
                # the branch moved between plan and push (the repository is being developed): re-read its tip, re-classify, apply again, push once more
                again = resync(r, a.out, text, plan)
                if again == "pushed":
                    return dict(repo=r["repo"], result="pushed", was=r["status"], note="after fetching a newer tip")
                if again in ("current", "diverged"):
                    return dict(repo=r["repo"], result="skipped-" + again, note="after fetching a newer tip")
            if p.returncode == 0:
                return dict(repo=r["repo"], result="pushed", was=r["status"])
            br = "docs/claude-md-sync-" + plan["canonical_head"]
            p2 = subprocess.run(["git", "-C", d, "push", "-q", "origin", "HEAD:refs/heads/" + br], capture_output=True, text=True)
            return dict(repo=r["repo"], result="branch-needs-pr" if p2.returncode == 0 else "push-failed", branch=br, note=(p.stderr or "").strip()[:100])
        except Exception as e:
            return dict(repo=r["repo"], result="error", note=str(e)[:150])

    res, lock = [], threading.Lock()

    def run(r):
        x = one(r)
        x["canonical"] = plan["canonical_head"]
        with lock:
            res.append(x)
            print(x["repo"], x["result"], flush=True)
            with open(logp, "a") as f:
                f.write(json.dumps(x) + "\n")

    with cf.ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(run, todo))
    t = {}
    for x in res:
        t[x["result"]] = t.get(x["result"], 0) + 1
    print("applied:", t)


def cmd_diverged(a):
    """For every 'diverged' repository: which canonical revision it is closest to, and which lines are the repository's own (not in that revision).
    own == 0: the copy only lost or kept old canonical lines, replacing it loses nothing. own > 0: those lines are local content to keep (repo-local tail) or to review."""
    import collections
    with open(os.path.join(a.out, "plan.json")) as f:
        plan = json.load(f)
    revs = git(a.registry, "log", "--format=%H", "origin/main", "--", "CLAUDE.md").split("\n")
    texts = []
    for c in revs:
        t = subprocess.run(["git", "-C", a.registry, "show", c + ":CLAUDE.md"], capture_output=True, text=True)
        if t.returncode == 0:
            texts.append((len(texts), c[:9], collections.Counter(l.strip() for l in t.stdout.split("\n") if l.strip())))
    out = []
    for r in plan["rows"]:
        if r["status"] != "diverged":
            continue
        with open(os.path.join(a.out, "clones", r["repo"], "CLAUDE.md"), errors="replace") as f:
            txt = f.read()
        lines = [l.strip() for l in txt.split("\n") if l.strip()]
        mine = collections.Counter(lines)
        best = None
        for i, c, cnt in texts:
            own, lost = sum((mine - cnt).values()), sum((cnt - mine).values())
            if best is None or own + lost < best[1] + best[2]:
                best = (i, own, lost, c)
        i, own, lost, c = best
        extra = list((mine - texts[i][2]).elements())
        sample = []
        seen = collections.Counter(extra)
        for l in lines:
            if seen[l] > 0:
                sample.append(l[:140]); seen[l] -= 1
        out.append(dict(repo=r["repo"], closest_rev=c, revisions_behind=i, own_lines=own, lost_lines=lost, total_lines=len(lines), own_sample=sample[:6]))
    with open(os.path.join(a.out, "diverged.json"), "w") as f:
        json.dump(out, f, indent=1)
    zero = [x for x in out if x["own_lines"] == 0]
    small = [x for x in out if 0 < x["own_lines"] <= 25]
    big = [x for x in out if x["own_lines"] > 25]
    print("diverged %d: own_lines==0 (safe to replace) %d | 1-25 own lines %d | more than 25 own lines %d" % (len(out), len(zero), len(small), len(big)))


def merge_text(registry, rev, mine, theirs):
    """three-way merge: base = the canonical revision the copy is closest to, ours = the repository's copy, theirs = the canonical now.
    -> (merged text, number of conflicts)  (git merge-file: 0 = every local edit and every canonical change applied cleanly)"""
    import tempfile
    base = subprocess.run(["git", "-C", registry, "show", rev + ":CLAUDE.md"], capture_output=True, text=True, check=True).stdout
    with tempfile.TemporaryDirectory() as t:
        paths = []
        for name, body in (("ours", mine), ("base", base), ("theirs", theirs)):
            pth = os.path.join(t, name)
            with open(pth, "w") as f:
                f.write(body)
            paths.append(pth)
        r = subprocess.run(["git", "merge-file", "-p", "-L", "local", "-L", "canonical-base", "-L", "canonical", *paths], capture_output=True, text=True)
        return r.stdout, (r.returncode if r.returncode >= 0 else 255)


def cmd_merge(a):
    """For 'diverged' repositories: merge the new canonical into the local copy. Clean merges are committed (and pushed with --push); conflicts are left alone and listed."""
    with open(os.path.join(a.out, "diverged.json")) as f:
        dv = {x["repo"]: x for x in json.load(f)}
    with open(os.path.join(a.out, "plan.json")) as f:
        plan = json.load(f)
    with open(os.path.join(a.out, "CLAUDE.md.canonical")) as f:
        theirs = f.read()
    revs = {c[:9]: c for c in git(a.registry, "log", "--format=%H", "origin/main", "--", "CLAUDE.md").split("\n")}
    todo = [r for r in plan["rows"] if r["status"] == "diverged"]
    if a.only:
        todo = [r for r in todo if r["repo"] in a.only.split(",")]
    logp = os.path.join(a.out, "merge_log.jsonl")
    lock = threading.Lock()

    def one(r):
        d = os.path.join(a.out, "clones", r["repo"])
        with open(os.path.join(d, "CLAUDE.md"), errors="replace") as f:
            mine = f.read()
        merged, nconf = merge_text(a.registry, revs[dv[r["repo"]]["closest_rev"]], mine, theirs)
        x = dict(repo=r["repo"], base=dv[r["repo"]]["closest_rev"], conflicts=nconf, canonical=plan["canonical_head"])
        if nconf:
            x["result"] = "conflict"
        elif merged == mine:
            x["result"] = "unchanged"
        elif not a.push:
            x["result"] = "clean-not-pushed"
        else:
            with open(os.path.join(d, "CLAUDE.md"), "w") as f:
                f.write(merged)
            git(d, "add", "CLAUDE.md")
            msg = "docs(claude): merge the services-registry canonical %s into the local CLAUDE.md [skip ci]" % plan["canonical_head"]
            body = ("Three-way merge (git merge-file): base = canonical revision %s, which this copy was closest to; ours = this repository's CLAUDE.md, whose own edits are kept; "
                    "theirs = the canonical now. No conflicts.\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>") % x["base"]
            subprocess.run(["git", "-C", d] + ID + ["commit", "-q", "-m", msg, "-m", body], capture_output=True, text=True, check=True)
            p = subprocess.run(["git", "-C", d, "push", "-q", "origin", "HEAD"], capture_output=True, text=True)
            x["result"] = "pushed" if p.returncode == 0 else "push-failed"
            x["note"] = (p.stderr or "").strip()[:100] if p.returncode else ""
        with lock:
            print(x["repo"], x["result"], x["conflicts"], flush=True)
            with open(logp, "a") as f:
                f.write(json.dumps(x) + "\n")
        return x

    with cf.ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(one, todo))
    t = {}
    for x in res:
        t[x["result"]] = t.get(x["result"], 0) + 1
    print("merge:", t)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("plan"); p.add_argument("--registry", required=True); p.add_argument("--out", required=True)
    p.add_argument("--repos"); p.add_argument("--repos-file"); p.add_argument("--workers", type=int, default=6)
    dv = sp.add_parser("diverged"); dv.add_argument("--registry", required=True); dv.add_argument("--out", required=True)
    mg = sp.add_parser("merge"); mg.add_argument("--registry", required=True); mg.add_argument("--out", required=True); mg.add_argument("--only"); mg.add_argument("--push", action="store_true"); mg.add_argument("--workers", type=int, default=4)
    q = sp.add_parser("apply"); q.add_argument("--out", required=True); q.add_argument("--limit", type=int); q.add_argument("--only"); q.add_argument("--exclude"); q.add_argument("--no-push", action="store_true"); q.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    {"plan": cmd_plan, "apply": cmd_apply, "diverged": cmd_diverged, "merge": cmd_merge}[a.cmd](a)
