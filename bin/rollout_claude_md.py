#!/usr/bin/env python3
"""Roll the canonical CLAUDE.md (services-registry) out to the fleet's repositories. Deterministic, no LLM.

Per repository it compares the blob of CLAUDE.md with the canonical's own history:
  current    identical to the canonical now                         -> nothing to do
  absent     no CLAUDE.md                                           -> add it
  behind     identical to an OLDER canonical revision (N behind)    -> replace it (nothing repo-specific can be lost)
  diverged   differs from every canonical revision                  -> skipped (needs a human or `docdrift sync` with a repo-local tail)
Commits carry "[skip ci]" so a docs-only commit does not start a pipeline in ~500 repositories (not the commit that goes to the sync branch of a protected repository: its PR needs the required checks). Git runs over SSH, so GitHub's API quota is not used.

usage:
  rollout_claude_md.py plan   --registry <clone> --out <dir> [--repos a,b,c | --repos-file f] [--workers 6]
  rollout_claude_md.py apply  --out <dir> [--limit N] [--only a,b] [--no-push]
plan clones every repository shallowly into <out>/clones and writes <out>/plan.json; apply commits and pushes from those clones.
A push to a protected default branch is retried as the branch docs/claude-md-sync-<sha> and recorded as needing a PR."""
import argparse, concurrent.futures as cf, json, os, subprocess, sys, threading

OWNER = "baditaflorin"
SYNC_BRANCH = "docs/claude-md-sync"
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


def remote_default_branch(d):
    """the branch the remote calls HEAD (git ls-remote --symref), or None when the remote cannot be asked"""
    p = subprocess.run(["git", "-C", d, "ls-remote", "--symref", "origin", "HEAD"], capture_output=True, text=True)
    if p.returncode:
        return None
    for line in p.stdout.splitlines():
        if line.startswith("ref:"):
            return line.split()[1].replace("refs/heads/", "", 1)
    return None


def refresh_clone(repo, out):
    """a shallow clone at the remote's newest default-branch tip; None when it cannot be had. Clones are a disposable cache: a damaged one
    (a stale index.lock from an interrupted run, a failed reset) is deleted and cloned again once."""
    import shutil
    d = os.path.join(out, "clones", repo)
    for attempt in (1, 2):
        try:
            if os.path.isdir(d):
                lock = os.path.join(d, ".git", "index.lock")
                if os.path.exists(lock):
                    os.remove(lock)  # nothing else runs in these clones: the lock is left over from an interrupted run
                # the remote's own default branch decides, never what this cache clone happens to have checked out: a leftover local branch made the
                # rollout push its commit to a stray remote branch of the same name instead of the default branch (go-fleet-metrics-hub, 2026-10-16)
                br = remote_default_branch(d) or git(d, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).replace("origin/", "") or "HEAD"
                f = subprocess.run(["git", "-C", d, "fetch", "-q", "--depth", "1", "origin", br], capture_output=True, text=True)
                if f.returncode:
                    raise RuntimeError("fetch: " + f.stderr.strip()[:120])
                if br == "HEAD":
                    git(d, "reset", "-q", "--hard", "FETCH_HEAD")
                else:
                    git(d, "checkout", "-q", "-f", "-B", br, "FETCH_HEAD")
                return d, None
            r = subprocess.run(["git", "clone", "--depth", "1", "--quiet", "git@github.com:%s/%s.git" % (OWNER, repo), d], capture_output=True, text=True)
            if r.returncode:
                return None, "clone: " + r.stderr.strip()[:120]
            return d, None
        except Exception as e:
            if attempt == 2:
                return None, str(e)[:120]
            shutil.rmtree(d, ignore_errors=True)
    return None, "unreachable"


def classify_one(repo, out, cblob, blobs):
    d, err = refresh_clone(repo, out)
    if d is None:
        return dict(repo=repo, status="clone-failed" if err.startswith("clone") else "fetch-failed", note=err)
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


def drop_skip_ci(d):
    """The sync branch backs a pull request, and a repository that protects its default branch may require status checks on it (a ruleset
    requiring ci/woodpecker/push/woodpecker): a commit marked [skip ci] never gets them, so the PR stays BLOCKED however often it is rebuilt
    (found 2026-10-16: mesh-card-sorter#9 and mesh-budget-pot#9). Only the commit that goes to the branch loses the marker; direct pushes keep it."""
    m = subprocess.run(["git", "-C", d, "log", "-1", "--format=%B"], capture_output=True, text=True, check=True).stdout
    if " [skip ci]" in m:
        subprocess.run(["git", "-C", d] + ID + ["commit", "-q", "--amend", "-m", m.replace(" [skip ci]", "").rstrip("\n")], capture_output=True, text=True, check=True)


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
            br = SYNC_BRANCH  # one branch per repository, owned by this tool: a later run updates it (and so the open PR) instead of adding another
            drop_skip_ci(d)
            p2 = subprocess.run(["git", "-C", d, "push", "-q", "--force", "origin", "HEAD:refs/heads/" + br], capture_output=True, text=True)
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


def history(registry):
    """[(revisions behind, commit, counter of stripped lines)] for every revision of the canonical, newest first"""
    out = []
    for c in git(registry, "log", "--format=%H", "origin/main", "--", "CLAUDE.md").split("\n"):
        t = subprocess.run(["git", "-C", registry, "show", c + ":CLAUDE.md"], capture_output=True, text=True)
        if t.returncode == 0:
            import collections
            out.append((len(out), c, collections.Counter(l.strip() for l in t.stdout.split("\n") if l.strip())))
    return out


def closest_rev(texts, mine):
    import collections
    m = collections.Counter(l.strip() for l in mine.split("\n") if l.strip())
    best = min(((sum((m - cnt).values()), sum((cnt - m).values()), i, c) for i, c, cnt in texts))
    return best[3], best[2], best[0]  # commit, revisions behind, own lines


def canonical_lines_missing(mine, theirs):
    """the non-blank canonical lines this copy lacks (order kept, duplicates counted)"""
    import collections
    have = collections.Counter(l.strip() for l in mine.split("\n") if l.strip())
    out = []
    for l in theirs.split("\n"):
        k = l.strip()
        if k and have[k] > 0:
            have[k] -= 1
        elif k:
            out.append(l)
    return out


def replace_safe(mine, theirs, own):
    """A copy with NO lines of its own (every non-blank line also exists in the canonical revision it is closest to) holds nothing worth merging; it only
    lacks canonical lines, and a three-way merge cannot bring those back because it treats the missing line as a local deletion. -> the canonical text and the
    canonical lines the copy lacked, or None when the copy has lines of its own (that is a real merge) or already equals the canonical."""
    if own != 0 or mine == theirs:
        return None
    return theirs, canonical_lines_missing(mine, theirs)


def merge_prefer_canonical(registry, rev, mine, theirs):
    """three-way merge that always yields a text. Where both sides INSERTED at the same place (the base side of the conflict is empty) both are kept;
    where both CHANGED the same lines the canonical wins. -> (text, conflicts, overridden): the local wording the canonical replaced"""
    import tempfile
    base = subprocess.run(["git", "-C", registry, "show", rev + ":CLAUDE.md"], capture_output=True, text=True, check=True).stdout
    with tempfile.TemporaryDirectory() as t:
        ps = []
        for n, b in (("ours", mine), ("base", base), ("theirs", theirs)):
            pth = os.path.join(t, n)
            with open(pth, "w") as f:
                f.write(b)
            ps.append(pth)
        r = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", "local", "-L", "base", "-L", "canonical", *ps], capture_output=True, text=True)
    out, over, lines, i = [], [], r.stdout.split("\n"), 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            j = i
            while not lines[j].startswith(">>>>>>>"):
                j += 1
            blk = lines[i + 1:j]
            k1 = next(n for n, l in enumerate(blk) if l.startswith("|||||||"))
            k2 = next(n for n, l in enumerate(blk) if l == "=======")
            loc, bas, can = blk[:k1], blk[k1 + 1:k2], blk[k2 + 1:]
            if not any(l.strip() for l in bas):
                out += loc + can
            else:
                out += can
                over.append(" ".join(l.strip() for l in loc if l.strip())[:160])
            i = j + 1
            continue
        out.append(lines[i]); i += 1
    return "\n".join(out), r.returncode, over


def cmd_merge(a):
    """For 'diverged' repositories: merge the new canonical into the local copy. Without --prefer-canonical only conflict-free merges are pushed (and only with --push);
    with it every copy is merged (both-inserted: keep both; both-changed: the canonical wins) and the replaced local wording is recorded in the commit message."""
    with open(os.path.join(a.out, "plan.json")) as f:
        plan = json.load(f)
    with open(os.path.join(a.out, "CLAUDE.md.canonical")) as f:
        theirs = f.read()
    texts = history(a.registry)
    todo = [r for r in plan["rows"] if r["status"] == "diverged"]
    if a.only:
        todo = [r for r in todo if r["repo"] in a.only.split(",")]
    logp = os.path.join(a.out, "merge_log.jsonl")
    lock = threading.Lock()

    def one(r):
        d = os.path.join(a.out, "clones", r["repo"])
        with open(os.path.join(d, "CLAUDE.md"), errors="replace") as f:
            mine = f.read()
        rev, behind, own = closest_rev(texts, mine)
        safe = replace_safe(mine, theirs, own) if getattr(a, "replace_safe", False) else None
        if safe is not None:
            merged, restored = safe
            nconf, over = 0, []
        elif a.prefer_canonical:
            merged, nconf, over = merge_prefer_canonical(a.registry, rev, mine, theirs); restored = None
        else:
            merged, nconf = merge_text(a.registry, rev, mine, theirs); over = []; restored = None
        x = dict(repo=r["repo"], base=rev[:9], conflicts=nconf, overridden=len(over), canonical=plan["canonical_head"])
        if restored is not None:
            x["replaced_safe"] = len(restored)
        if nconf and not a.prefer_canonical:
            x["result"] = "conflict"
        elif merged == mine:
            x["result"] = "unchanged"
        elif not a.push:
            x["result"] = "clean-not-pushed" if not nconf else "resolved-not-pushed"
        else:
            with open(os.path.join(d, "CLAUDE.md"), "w") as f:
                f.write(merged)
            git(d, "add", "CLAUDE.md")
            if restored is not None:
                msg = "docs(claude): restore the %d line(s) this CLAUDE.md lacked from the services-registry canonical %s [skip ci]" % (len(restored), plan["canonical_head"])
                body = ("This copy had no lines of its own: every line also appears in canonical revision %s, the one it is closest to. It only lacked canonical lines, which a three-way "
                        "merge cannot restore (it keeps the missing line as a local deletion), so the file now equals the canonical.") % x["base"]
                body += "\n\nLines restored:\n" + "\n".join("- " + o[:160] for o in restored[:12])
            else:
                msg = "docs(claude): merge the services-registry canonical %s into the local CLAUDE.md [skip ci]" % plan["canonical_head"]
                body = ("Three-way merge (git merge-file): base = canonical revision %s (the one this copy is closest to), ours = this repository's CLAUDE.md, theirs = the canonical now. "
                        "Where both sides inserted at the same place both were kept; where both changed the same lines the canonical wording was used (%d such hunks).") % (x["base"], len(over))
                if over:
                    body += "\n\nLocal wording replaced by the canonical (still in this repository's history):\n" + "\n".join("- " + o for o in over[:12])
            body += "\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
            subprocess.run(["git", "-C", d] + ID + ["commit", "-q", "-m", msg, "-m", body], capture_output=True, text=True, check=True)
            p = subprocess.run(["git", "-C", d, "push", "-q", "origin", "HEAD"], capture_output=True, text=True)
            if p.returncode and ("fetch first" in p.stderr or "non-fast-forward" in p.stderr):
                # the branch moved: redo the merge on its newest tip once
                br = r.get("branch") or "HEAD"
                if subprocess.run(["git", "-C", d, "fetch", "-q", "--depth", "1", "origin", br], capture_output=True, text=True).returncode == 0:
                    git(d, "reset", "-q", "--hard", "FETCH_HEAD")
                    with open(os.path.join(d, "CLAUDE.md"), errors="replace") as f:
                        mine2 = f.read()
                    rev2, _, own2 = closest_rev(texts, mine2)
                    safe2 = replace_safe(mine2, theirs, own2) if getattr(a, "replace_safe", False) else None
                    merged2, over2 = (safe2[0], []) if safe2 is not None else merge_prefer_canonical(a.registry, rev, mine2, theirs)[::2]
                    if merged2 != mine2:
                        with open(os.path.join(d, "CLAUDE.md"), "w") as f:
                            f.write(merged2)
                        git(d, "add", "CLAUDE.md")
                        subprocess.run(["git", "-C", d] + ID + ["commit", "-q", "-m", msg, "-m", body], capture_output=True, text=True, check=True)
                        p = subprocess.run(["git", "-C", d, "push", "-q", "origin", "HEAD"], capture_output=True, text=True)
            if p.returncode == 0:
                x["result"] = "pushed"
            else:
                drop_skip_ci(d)
                p2 = subprocess.run(["git", "-C", d, "push", "-q", "--force", "origin", "HEAD:refs/heads/" + SYNC_BRANCH], capture_output=True, text=True)
                x["result"] = "branch-needs-pr" if p2.returncode == 0 else "push-failed"
                x["branch"] = SYNC_BRANCH
                x["note"] = (p.stderr or "").strip()[:100]
        with lock:
            print(x["repo"], x["result"], "conflicts=%s overridden=%s" % (x["conflicts"], x["overridden"]), flush=True)
            with open(logp, "a") as f:
                f.write(json.dumps(x) + "\n")
        return x

    with cf.ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(one, todo))
    t = {}
    for x in res:
        t[x["result"]] = t.get(x["result"], 0) + 1
    print("merge:", t)
    return res


def repo_names(registry, extra_file):
    """every repository the canonical is rolled out to: the registry's services.json (repo_url) plus the infrastructure repositories in extra_file"""
    import re
    reg = json.loads(git(registry, "show", "origin/main:services.json"))
    ents = reg if isinstance(reg, list) else reg.get("services", reg)
    names = {m.group(1).replace(".git", "") for e in ents for m in [re.search(r"github\.com/%s/([^/\s#]+)" % re.escape(OWNER), e.get("repo_url") or "")] if m}
    if extra_file and os.path.exists(extra_file):
        with open(extra_file) as f:
            names |= {l.strip() for l in f.read().splitlines() if l.strip() and not l.startswith("#")}
    return sorted(names)


def ensure_prs(results, head):
    """a repository whose default branch is protected got the branch SYNC_BRANCH: make sure a PR from it is open (best effort; needs gh)"""
    import shutil
    made = []
    todo = [x for x in results if x.get("result") == "branch-needs-pr"]
    if todo and not shutil.which("gh"):
        return [(x["repo"], "gh not found on PATH: branch %s is pushed, open the PR by hand" % SYNC_BRANCH) for x in todo]
    for x in todo:
        repo = "%s/%s" % (OWNER, x["repo"])
        p = subprocess.run(["gh", "pr", "list", "--repo", repo, "--head", SYNC_BRANCH, "--state", "open", "--json", "number", "--jq", "length"], capture_output=True, text=True)
        if p.returncode == 0 and p.stdout.strip() == "0":
            body = ("Refreshes CLAUDE.md from the services-registry canonical (%s). The default branch of this repository is protected, so the weekly fleet rollout (`bin/rollout_claude_md.py weekly`) "
                    "puts the change on the branch `%s` instead of pushing it; the next run updates this same branch.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)") % (head, SYNC_BRANCH)
            q = subprocess.run(["gh", "pr", "create", "--repo", repo, "--head", SYNC_BRANCH, "--title", "docs(claude): refresh CLAUDE.md from the services-registry canonical", "--body", body], capture_output=True, text=True)
            made.append((x["repo"], (q.stdout or q.stderr).strip().splitlines()[-1][:120] if (q.stdout or q.stderr).strip() else "?"))
    return made


def cmd_weekly(a):
    """plan -> apply -> merge the diverged copies (the canonical wins where both changed the same lines) -> PRs for protected repositories -> report.
    Everything lives under --work (registry clone not needed to exist: it must already be a clone; this command fetches it)."""
    import datetime
    os.makedirs(a.work, exist_ok=True)
    git(a.registry, "fetch", "-q", "origin")
    repos = repo_names(a.registry, a.extra)
    rf = os.path.join(a.work, "repos.txt")
    with open(rf, "w") as f:
        f.write("\n".join(repos) + "\n")
    ns = argparse.Namespace(registry=a.registry, out=a.work, repos=None, repos_file=rf, workers=a.workers)
    cmd_plan(ns)
    with open(os.path.join(a.work, "plan.json")) as f:
        plan = json.load(f)
    before = {}
    for r in plan["rows"]:
        before[r["status"]] = before.get(r["status"], 0) + 1
    apply_ns = argparse.Namespace(out=a.work, limit=None, only=None, exclude=None, no_push=False, workers=a.apply_workers)
    cmd_apply(apply_ns)
    merge_ns = argparse.Namespace(registry=a.registry, out=a.work, only=None, push=True, prefer_canonical=True, replace_safe=True, workers=a.apply_workers)
    merged = cmd_merge(merge_ns)
    with open(os.path.join(a.work, "apply_log.jsonl")) as f:
        applied = [json.loads(l) for l in f if l.strip()]
    mine = [x for x in applied if x.get("canonical") == plan["canonical_head"]] + merged
    prs = ensure_prs(mine, plan["canonical_head"])
    tally = {}
    for x in mine:
        tally[x["result"]] = tally.get(x["result"], 0) + 1
    bad = [x for x in mine if x["result"] in ("push-failed", "error", "conflict")]
    clone_failed = [r["repo"] for r in plan["rows"] if r["status"] in ("clone-failed", "fetch-failed")]
    rep = dict(date=datetime.date.today().isoformat(), canonical=plan["canonical_head"], blob=plan["canonical_blob"][:10], repositories=len(repos), classified_before=before, results=tally,
               problems=[dict(repo=x["repo"], result=x["result"], note=x.get("note", "")) for x in bad], not_reachable=clone_failed, prs_opened=prs)
    os.makedirs(os.path.join(a.work, "reports"), exist_ok=True)
    with open(os.path.join(a.work, "reports", rep["date"] + ".json"), "w") as f:
        json.dump(rep, f, indent=1)
    with open(os.path.join(a.work, "reports", "latest.json"), "w") as f:
        json.dump(rep, f, indent=1)
    print("WEEKLY REPORT", json.dumps(rep))
    return 1 if (bad or clone_failed) else 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("plan"); p.add_argument("--registry", required=True); p.add_argument("--out", required=True)
    p.add_argument("--repos"); p.add_argument("--repos-file"); p.add_argument("--workers", type=int, default=6)
    dv = sp.add_parser("diverged"); dv.add_argument("--registry", required=True); dv.add_argument("--out", required=True)
    mg = sp.add_parser("merge"); mg.add_argument("--registry", required=True); mg.add_argument("--out", required=True); mg.add_argument("--only"); mg.add_argument("--push", action="store_true"); mg.add_argument("--prefer-canonical", action="store_true"); mg.add_argument("--replace-safe", action="store_true", help="a copy with no lines of its own is replaced by the canonical (a three-way merge keeps the lines it lacks missing)"); mg.add_argument("--workers", type=int, default=4)
    wk = sp.add_parser("weekly"); wk.add_argument("--registry", required=True); wk.add_argument("--work", required=True); wk.add_argument("--extra", help="file with extra repository names (infrastructure repos that are not in services.json)")
    wk.add_argument("--workers", type=int, default=8); wk.add_argument("--apply-workers", type=int, default=5)
    q = sp.add_parser("apply"); q.add_argument("--out", required=True); q.add_argument("--limit", type=int); q.add_argument("--only"); q.add_argument("--exclude"); q.add_argument("--no-push", action="store_true"); q.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    fn = {"plan": cmd_plan, "apply": cmd_apply, "diverged": cmd_diverged, "merge": cmd_merge, "weekly": cmd_weekly}[a.cmd]
    rc = fn(a)
    sys.exit(rc if a.cmd == "weekly" and isinstance(rc, int) else 0)
