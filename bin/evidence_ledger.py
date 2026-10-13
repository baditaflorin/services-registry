#!/usr/bin/env python3
"""Evidence ledger: for every machine-checkable piece of evidence behind a documentation claim, remember WHAT it said and AT WHICH COMMIT
and VERSION it was true, so that later (when the code has moved on) a CPU-only check can say: still there / moved / changed / gone, how many
commits and release tags have passed since, and show the actual diff. No LLM involved.

Evidence refs of the form  <repo>/<path>:<l1>[-<l2>][,<l3>[-<l4>]...]@<sha>  are ledgered, one row per line range.

usage:
  evidence_ledger.py build  --claims claims_final.jsonl --repos <dir with <repo>/ clones> --out ledger.jsonl [--ref HEAD]
  evidence_ledger.py check  --ledger ledger.jsonl --repos <dir> [--ref HEAD] [--json out.json] [--md out.md] [--show-diff N]
A row stores: the claim (doc, line, text, verdict, severity), the evidence (repo, path, lines), the stamp (the commit the snippet was read at, its
nearest release tag), a digest of the whitespace-normalised snippet and the snippet itself (<=15 lines).
check statuses: FRESH (same text at the same lines), MOVED (same text, other lines), CHANGED (text no longer present; diff + versions since),
GONE (file missing at the ref), NO-REPO (repository not available here)."""
import argparse, collections, difflib, hashlib, json, os, re, subprocess, sys

# A snippet that looks like it holds a credential is never stored (only its digest is): the ledger is committed, and the "now" side of a diff is shown.
SECRET = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|credential|private[_-]?key)\w*[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9+/_.\-]{12,}")
REDACTED = ["<redacted: credential-like line; compare by digest only>"]
PAT = re.compile(r"^([\w.-]+)/(.+?):((?:\d+(?:-\d+)?)(?:,\d+(?:-\d+)?)*)@([0-9a-f]{7,40})$")
_cache = {}


def git(repo_dir, *a):
    r = subprocess.run(["git", "-C", repo_dir] + list(a), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def rref(repos, repo, ref):
    """ref "auto" = the repository's own default branch: origin/main, else origin/master, else HEAD"""
    if ref != "auto":
        return ref
    d = os.path.join(repos, repo)
    for c in ("origin/main", "origin/master", "HEAD"):
        if git(d, "rev-parse", "--verify", "-q", c):
            return c
    return "HEAD"


def show(repos, repo, ref, path):
    ref = rref(repos, repo, ref)
    k = (repo, ref, path)
    if k not in _cache:
        t = git(os.path.join(repos, repo), "show", "%s:%s" % (ref, path))
        _cache[k] = None if t is None else t.split("\n")
    return _cache[k]


norm = lambda lines: [re.sub(r"\s+", " ", l).strip() for l in lines if l.strip()]
digest = lambda lines: hashlib.sha256("\n".join(norm(lines)).encode()).hexdigest()[:16]


def ranges(spec):
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out.append((int(a), int(b or a)))
    return out


def tag_at(repos, repo, sha):
    d = os.path.join(repos, repo)
    t = git(d, "describe", "--tags", "--abbrev=0", sha)
    return (t or "").strip() or None


def full_sha(repos, repo, ref):
    return (git(os.path.join(repos, repo), "rev-parse", ref) or "").strip()


def cmd_build(a):
    rows, skipped = [], collections.Counter()
    heads = {}
    for line in open(a.claims).read().splitlines():
        c = json.loads(line)
        for k, e in enumerate(c.get("evidence") or []):
            m = PAT.match(e.get("ref", ""))
            if not m:
                skipped["not a file:line@sha ref"] += 1
                continue
            repo, path, spec, label = m.groups()
            if not os.path.isdir(os.path.join(a.repos, repo, ".git")) and not os.path.isfile(os.path.join(a.repos, repo, ".git")):
                skipped["repository not available"] += 1
                continue
            head = heads.setdefault(repo, full_sha(a.repos, repo, a.ref))
            lines = show(a.repos, repo, head, path)
            if lines is None:
                skipped["file absent at the stamp commit"] += 1
                continue
            old = show(a.repos, repo, label, path)
            for (l1, l2) in ranges(spec):
                snip = lines[l1 - 1:l2]
                if not norm(snip):
                    skipped["empty snippet"] += 1
                    continue
                red = any(SECRET.search(x) for x in snip)
                rows.append(dict(
                    id="%s:%s:%d:%d-%d" % (c["chunk"], c["n"], k, l1, l2),
                    doc="%s/%s" % (c["repo"], c["path"]), doc_line=c["line"], claim=c["text"][:200], verdict=c["final_verdict"], severity=c.get("severity"),
                    repo=repo, path=path, lines=[l1, l2], stamp_sha=head[:12], stamp_tag=tag_at(a.repos, repo, head),
                    label_sha=label, moved_since_label=(old is not None and digest(old[l1 - 1:l2]) != digest(snip)),
                    digest=digest(snip), snippet=REDACTED if red else (snip if len(snip) <= 15 else snip[:3] + ["..."]), redacted=red, as_of=a.as_of))
    with open(a.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("ledger rows: %d | skipped: %s | rows whose snippet differs between the label sha and the stamp: %d" % (len(rows), dict(skipped), sum(1 for r in rows if r["moved_since_label"])))


def find_snippet(file_lines, snip_norm):
    n = len(snip_norm)
    flat = [(i, re.sub(r"\s+", " ", l).strip()) for i, l in enumerate(file_lines) if l.strip()]
    texts = [t for _, t in flat]
    for j in range(len(texts) - n + 1):
        if texts[j:j + n] == snip_norm:
            return flat[j][0] + 1
    return None


def counts_since(repos, repo, sha, ref):
    d = os.path.join(repos, repo)
    n = (git(d, "rev-list", "--count", "%s..%s" % (sha, ref)) or "0").strip()
    t = (git(d, "tag", "--merged", ref, "--no-merged", sha) or "").split()
    return int(n or 0), len(t)


def cmd_check(a):
    out = []
    for line in open(a.ledger).read().splitlines():
        r = json.loads(line)
        l1, l2 = r["lines"]
        if not os.path.isdir(os.path.join(a.repos, r["repo"])):
            out.append(dict(r, status="NO-REPO"))
            continue
        cur = show(a.repos, r["repo"], a.ref, r["path"])
        rr = rref(a.repos, r["repo"], a.ref)
        snip_norm = norm(r["snippet"]) if r["snippet"][-1:] != ["..."] and not r.get("redacted") else None
        if cur is None:
            st, info = "GONE", {}
        elif digest(cur[l1 - 1:l2]) == r["digest"]:
            st, info = "FRESH", {}
        else:
            where = find_snippet(cur, snip_norm) if snip_norm else None
            if where:
                st, info = "MOVED", {"now_at_line": where}
            else:
                st = "CHANGED"
                info = {}
        if st in ("CHANGED", "GONE"):
            n, t = counts_since(a.repos, r["repo"], r["stamp_sha"], rr)
            info.update(commits_since=n, tags_since=t, now_tag=tag_at(a.repos, r["repo"], rr))
            if st == "CHANGED" and cur is not None:
                d = "\n".join(difflib.unified_diff(r["snippet"], cur[l1 - 1:l2 + 6], "at %s (%s)" % (r["stamp_sha"], r["stamp_tag"] or "no tag"), "now", lineterm="", n=1))[:1500]
                info["diff"] = "<diff withheld: credential-like line>" if r.get("redacted") or SECRET.search(d) else d
        out.append(dict(r, status=st, **info))
    c = collections.Counter(x["status"] for x in out)
    print("evidence rows %d: %s" % (len(out), dict(c)))
    # how far each repository has moved since its evidence was stamped: "read at v0.7.277 (02bf8378f), now v0.7.310: 64 commits, 8 release tags later"
    repo_move = {}
    for x in out:
        if x["status"] == "NO-REPO":
            continue
        k = (x["repo"], x["stamp_sha"])
        if k not in repo_move:
            rr = rref(a.repos, x["repo"], a.ref)
            n, t = counts_since(a.repos, x["repo"], x["stamp_sha"], rr)
            repo_move[k] = dict(repo=x["repo"], stamp_sha=x["stamp_sha"], stamp_tag=x["stamp_tag"], now_tag=tag_at(a.repos, x["repo"], rr), commits_since=n, tags_since=t)
    by_doc = collections.defaultdict(collections.Counter)
    for x in out:
        by_doc[x["doc"]][x["status"]] += 1
    summary = dict(statuses=dict(c), by_doc={d: dict(v) for d, v in by_doc.items()}, repos=sorted(repo_move.values(), key=lambda m: -m["commits_since"]))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(dict(summary=summary, rows=out), f, indent=1, ensure_ascii=False)
    if a.md:
        bad = [x for x in out if x["status"] in ("CHANGED", "GONE")]
        sev = {"S1": 0, "S2": 1, "S3": 2}
        bad.sort(key=lambda x: (sev.get(x["severity"], 3), x["doc"], x["doc_line"]))
        L = ["# Evidence that has changed since it was stamped", "", "%d of %d evidence rows: %s" % (len(bad), len(out), dict(c)), "",
             "## Per document", "", "| document | evidence rows | fresh | moved | changed | gone | repo not here |", "|---|---|---|---|---|---|---|"]
        for d, v in sorted(by_doc.items(), key=lambda kv: -(kv[1]["CHANGED"] + kv[1]["GONE"])):
            L.append("| %s | %d | %d | %d | %d | %d | %d |" % (d, sum(v.values()), v["FRESH"], v["MOVED"], v["CHANGED"], v["GONE"], v["NO-REPO"]))
        L += ["", "## How far each repository has moved since the evidence was read", "", "| repository | read at | now | commits since | release tags since |", "|---|---|---|---|---|"]
        for m in summary["repos"][:60]:
            L.append("| %s | `%s` (%s) | %s | %d | %d |" % (m["repo"], m["stamp_sha"], m["stamp_tag"] or "no tag", m["now_tag"] or "no tag", m["commits_since"], m["tags_since"]))
        L += ["", "## Changed or gone evidence", ""]
        for x in bad[:a.show_diff or 200]:
            L += ["- **%s:%s** (%s %s) %s" % (x["doc"], x["doc_line"], x["verdict"], x["severity"] or "-", x["claim"][:140]),
                  "  - evidence `%s/%s:%d-%d` was read at `%s` (%s); %s now: %d commits and %d release tags later%s" % (
                      x["repo"], x["path"], x["lines"][0], x["lines"][1], x["stamp_sha"], x["stamp_tag"] or "no tag", x["status"], x.get("commits_since", 0), x.get("tags_since", 0),
                      (" (now %s)" % x["now_tag"]) if x.get("now_tag") else "")]
        with open(a.md, "w") as f:
            f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build"); b.add_argument("--claims", required=True); b.add_argument("--repos", required=True); b.add_argument("--out", required=True)
    b.add_argument("--ref", default="HEAD"); b.add_argument("--as-of", default="2026-10-12")
    c = sp.add_parser("check"); c.add_argument("--ledger", required=True); c.add_argument("--repos", required=True); c.add_argument("--ref", default="auto")
    c.add_argument("--json"); c.add_argument("--md"); c.add_argument("--show-diff", type=int, default=0)
    a = ap.parse_args()
    {"build": cmd_build, "check": cmd_check}[a.cmd](a)
