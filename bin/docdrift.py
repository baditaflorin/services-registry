#!/usr/bin/env python3
"""docdrift - detect and reduce drift between agent-facing docs and the code/data they describe.

PROTOTYPE of what should become `fleet-runner docs check|stamp|gen` (Go, next to the
claude-md-canonical audit check). It is READ-ONLY against the repos: it only runs
`git show`, `git log`, `git rev-parse`, `git grep` against a ref (default origin/main), never
against a working tree, so a stale or dirty shared checkout can not fool it (RULE ZERO).

ANCHORS live in HTML comments next to the claim they protect (Claude Code strips block-level HTML
comments from auto-loaded CLAUDE.md, so they cost no context):

  <!-- anchor id=verb-deploy-pool repo=go_fleet_runner symbol=verb:deploy-pool
       verified=2f178c713 digest=sha256:ab12... on=2026-10-12 [level=warn|error] [expires=2026-11-30]
       [check="fleet-runner deploy-pool --help"] -->

  symbol=  go:<file>#<Func|Type.Method|Const|Var>   declaration, comments/whitespace ignored
           verb:<name>                              the `case "<name>"` block in main.go + its handler func
           json:<file>#/<pointer>                   canonical-JSON digest of a subtree
           regex:<file>#<pattern>                   digest of every match (multiline)
           file:<path>                              digest of the whole file (existence claims)
  verified= short sha of the CODE commit (tip of the ref in that repo) the claim was verified against.
            Never the doc's own commit. Used for tracing (git log <sha>..origin/main -- <file>).
  digest=   sha256 of the normalised symbol text. THE change signal: unrelated commits to the same
            file do not trip it.
  expires=  live claims (host counts, who runs what) must be re-verified by this date.

COPIES carry a stamp instead of bytes:   <!-- canonical-ref repo=services-registry path=CLAUDE.md sha=4b2982a73 -->
GENERATED blocks are never typed by hand:  <!-- BEGIN GENERATED:verbs --> ... <!-- END GENERATED:verbs -->

usage:
  docdrift.py check   --repos DIR [--ref origin/main] [--as-of YYYY-MM-DD] [--json] [--explain] DOC...
  docdrift.py resolve --repos DIR REPO SYMBOL           print the digest + first lines of the resolved symbol
  docdrift.py stamp   --repos DIR --yes [--id ID] DOC   re-verify: rewrite verified/digest/on of fresh+stale anchors
  docdrift.py add     --repos DIR --doc DOC --line N --id ID --repo REPO --symbol SYM [--expires D] [--level L]
  docdrift.py gen     --repos DIR [--check|--write] DOC...    regenerate GENERATED blocks (exit 1 on diff with --check)
  docdrift.py lint    --repos DIR DOC...                      flag a lone `&` used as a command separator in shell recipes
  docdrift.py sync    --repos DIR CANONICAL TARGET [--write]   refresh a CLAUDE.md copy from the canonical, keeping its repo-local tail
  docdrift.py scan    --repos DIR [--json OUT]                 anchors + canonical-copy staleness for every repo clone in DIR
  docdrift.py census  --repos DIR [--json OUT]                 copy staleness of ALL registry repos via the GitHub API (blob shas, no clones)
Exit status of check: 0 clean, 1 STALE/BROKEN/EXPIRED at level=error (default), 2 usage/IO error.
"""
from __future__ import print_function
import argparse, datetime, hashlib, json, os, re, subprocess, sys

ANCHOR_RE = re.compile(r'<!--\s*anchor\s+(.*?)\s*-->', re.S)
CANON_RE = re.compile(r'<!--\s*canonical-ref\s+(.*?)\s*-->', re.S)
KV_RE = re.compile(r'(\w+)=("([^"]*)"|\S+)')
GEN_RE = re.compile(r'(<!--\s*BEGIN GENERATED:\s*(?P<name>[\w-]+)(?P<attrs>[^>]*?)-->)(?P<body>.*?)(<!--\s*END GENERATED:\s*(?P=name)\s*-->)', re.S)


# ------------------------------------------------------------------------------------------ git
class Git(object):
    def __init__(self, root, ref):
        self.root, self.ref = root, ref

    def _run(self, repo, *args):
        p = subprocess.run(["git", "-C", os.path.join(self.root, repo)] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode("utf-8", "replace").strip() or "git failed")
        return p.stdout.decode("utf-8", "replace")

    def has_repo(self, repo):
        return os.path.exists(os.path.join(self.root, repo, ".git"))

    def resolve_ref(self, repo):
        for cand in (self.ref, "HEAD"):
            try:
                return self._run(repo, "rev-parse", "--verify", "--quiet", cand + "^{commit}").strip()
            except RuntimeError:
                continue
        raise RuntimeError("no ref %s in %s" % (self.ref, repo))

    def show(self, repo, path, rev=None):
        return self._run(repo, "show", "%s:%s" % (rev or self.resolve_ref(repo), path))

    def has_commit(self, repo, sha):
        try:
            self._run(repo, "cat-file", "-e", sha + "^{commit}")
            return True
        except RuntimeError:
            return False

    def log_since(self, repo, since, path, limit=5):
        try:
            out = self._run(repo, "log", "--format=%h %cs %s", "%s..%s" % (since, self.resolve_ref(repo)), "--", path)
        except RuntimeError:
            return None
        lines = [l for l in out.splitlines() if l]
        return lines

    def grep_files(self, repo, pattern):
        try:
            out = self._run(repo, "grep", "-l", "-E", pattern, self.resolve_ref(repo), "--", "*.go")
        except RuntimeError:
            return []
        return [l.split(":", 1)[1] for l in out.splitlines() if ":" in l]

    def last_change(self, repo, path):
        try:
            return self._run(repo, "log", "-1", "--format=%h", self.resolve_ref(repo), "--", path).strip()
        except RuntimeError:
            return ""


# ------------------------------------------------------------------------------------------ go
def go_scan(src):
    """-> (masked, nocomment). masked: same length, comments and string interiors blanked (structure scanning).
    nocomment: comments removed, strings kept (hashing)."""
    m, s = [], []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j == -1 else j
            m.append(" " * (j - i))
            i = j
            continue
        if two == "/*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            seg = src[i:j]
            m.append("".join("\n" if ch == "\n" else " " for ch in seg))
            s.append("\n" * seg.count("\n"))
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            j = min(n, j + 1)
            seg = src[i:j]
            m.append(c + "".join("\n" if ch == "\n" else " " for ch in seg[1:-1]) + (c if len(seg) > 1 else ""))
            s.append(seg)
            i = j
            continue
        if c == "`":
            j = src.find("`", i + 1)
            j = n if j == -1 else j + 1
            seg = src[i:j]
            m.append("`" + "".join("\n" if ch == "\n" else " " for ch in seg[1:-1]) + ("`" if len(seg) > 1 else ""))
            s.append(seg)
            i = j
            continue
        m.append(c)
        s.append(c)
        i += 1
    return "".join(m), "".join(s)


def go_norm(text):
    _, nc = go_scan(text)
    return "\n".join(" ".join(l.split()) for l in nc.splitlines() if l.strip())


def _match_end(masked, start, body_open=None):
    """index just past the declaration that begins at `start`."""
    n = len(masked)
    i = body_open if body_open is not None else start
    depth, seen = 0, False
    while i < n:
        c = masked[i]
        if c in "({[":
            depth += 1
            seen = True
        elif c in ")}]":
            depth -= 1
            if depth <= 0 and seen and c == "}":
                j = masked.find("\n", i)
                return n if j == -1 else j
        elif c == "\n" and depth <= 0:
            prev = masked[start:i].rstrip()
            if not (prev.endswith((",", "+", "-", "*", "/", "&&", "||", "=", "(")) and not seen):
                return i
        i += 1
    return n


def go_find_decl(src, name):
    masked, _ = go_scan(src)
    pats = []
    if "." in name:
        recv, meth = name.split(".", 1)
        pats.append(r"^func\s*\(\s*\w*\s*\*?\s*%s(?:\[[^\]]*\])?\s*\)\s*%s\s*[\(\[]" % (re.escape(recv), re.escape(meth)))
    else:
        pats += [r"^func\s+%s\s*[\(\[]" % re.escape(name), r"^type\s+%s\b" % re.escape(name), r"^(?:const|var)\s+%s\b" % re.escape(name),
                 r"^\t%s\b(?=\s*(?:=|[A-Za-z\[\*]|$))" % re.escape(name)]
    for p in pats:
        m = re.search(p, masked, re.M)
        if not m:
            continue
        start = m.start()
        body = None
        if masked[start:].startswith("func"):
            # body brace = first '{' at paren depth 0 that ends its line
            depth, k = 0, start
            while k < len(masked):
                ch = masked[k]
                if ch in "([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
                elif ch == "{" and depth == 0:
                    rest = masked[k + 1:masked.find("\n", k) if masked.find("\n", k) != -1 else len(masked)]
                    if rest.strip() == "" or rest.strip() == "}":
                        body = k
                        break
                k += 1
        end = _match_end(masked, start, body)
        return src[start:end]
    return None


def go_verb_block(src, verb):
    lines = src.split("\n")
    sw = next((i for i, l in enumerate(lines) if "switch os.Args[1] {" in l), None)
    if sw is None:
        return None
    end = next((i for i in range(sw + 1, len(lines)) if lines[i] == "\t}"), len(lines))
    idx = [i for i in range(sw + 1, end) if re.match(r"^\t(case |default:)", lines[i])]
    for k, i in enumerate(idx):
        if lines[i].startswith("\tcase") and verb in re.findall(r'"([^"]+)"', lines[i].split(":")[0]):
            j = idx[k + 1] if k + 1 < len(idx) else end
            return "\n".join(lines[i:j])
    return None


def verbs_declared(src):
    lines = src.split("\n")
    sw = next((i for i, l in enumerate(lines) if "switch os.Args[1] {" in l), None)
    out = []
    if sw is None:
        return out
    end = next((i for i in range(sw + 1, len(lines)) if lines[i] == "\t}"), len(lines))
    for i in range(sw + 1, end):
        m = re.match(r"^\tcase ((?:\"[^\"]+\"\s*,?\s*)+):", lines[i])
        if m:
            out += re.findall(r'"([^"]+)"', m.group(1))
    return out


# ------------------------------------------------------------------------------------------ symbols
def sha(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_pointer(doc, ptr):
    cur = doc
    for tok in [t for t in ptr.split("/")[1:]] if ptr not in ("", "/") else []:
        tok = tok.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(tok)]
        else:
            cur = cur[tok]
    return cur


def resolve(git, repo, symbol):
    """-> (normalised_text, file_for_history). Raises LookupError with the reason when not found."""
    kind, _, rest = symbol.partition(":")
    if kind == "verb":
        path = "main.go"
        src = git.show(repo, path)
        blk = go_verb_block(src, rest)
        if blk is None:
            raise LookupError('case "%s" not found in %s dispatch (verb removed, renamed or never existed)' % (rest, path))
        parts = [go_norm(blk)]
        h = re.search(r"\b(\w+)\(os\.Args\[2:\]\)", blk)
        if h:
            for f in git.grep_files(repo, r"^func %s\(" % re.escape(h.group(1))):
                d = go_find_decl(git.show(repo, f), h.group(1))
                if d:
                    parts.append(go_norm(d))
                    path = f
                    break
        return "\n--\n".join(parts), path
    fil, _, sel = rest.partition("#")
    if kind == "go":
        d = go_find_decl(git.show(repo, fil), sel)
        if d is None:
            raise LookupError("go symbol %s not found in %s" % (sel, fil))
        return go_norm(d), fil
    if kind == "json":
        try:
            sub = json_pointer(json.loads(git.show(repo, fil)), sel)
        except (KeyError, IndexError, ValueError):
            raise LookupError("json pointer %s not found in %s" % (sel, fil))
        return json.dumps(sub, sort_keys=True, separators=(",", ":")), fil
    if kind == "regex":
        ms = [m.group(0) for m in re.finditer(sel, git.show(repo, fil), re.M)]
        if not ms:
            raise LookupError("pattern /%s/ matches nothing in %s" % (sel, fil))
        return "\n".join(" ".join(m.split()) for m in ms), fil
    if kind == "file":
        try:
            return git.show(repo, rest), rest
        except RuntimeError:
            raise LookupError("file %s does not exist at %s" % (rest, git.ref))
    raise LookupError("unknown symbol kind %r" % kind)


# ------------------------------------------------------------------------------------------ parsing
class Anchor(object):
    def __init__(self, doc, line, attrs, span):
        self.doc, self.line, self.attrs, self.span = doc, line, attrs, span
        self.id = attrs.get("id", "?")

    def get(self, k, d=""):
        return self.attrs.get(k, d)


def parse_attrs(s):
    out = {}
    for m in KV_RE.finditer(s):
        out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return out


def parse_doc(path):
    text = open(path, encoding="utf-8").read()
    anchors = []
    for m in ANCHOR_RE.finditer(text):
        anchors.append(Anchor(path, text.count("\n", 0, m.start()) + 1, parse_attrs(m.group(1)), m.span()))
    canon = []
    for m in CANON_RE.finditer(text):
        canon.append(Anchor(path, text.count("\n", 0, m.start()) + 1, parse_attrs(m.group(1)), m.span()))
    return text, anchors, canon


# ------------------------------------------------------------------------------------------ check
def check_anchor(git, a, as_of, explain):
    repo, sym = a.get("repo"), a.get("symbol")
    r = dict(doc=a.doc, line=a.line, id=a.id, repo=repo, symbol=sym, level=a.get("level", "error"), status="FRESH", detail="")
    if not repo or not sym:
        r.update(status="BROKEN", detail="anchor needs repo= and symbol=")
        return r
    if not git.has_repo(repo):
        r.update(status="SKIPPED", detail="repository %s is not available here (checked where it is cloned)" % repo)
        return r
    try:
        head = git.resolve_ref(repo)
        text, fil = resolve(git, repo, sym)
    except (LookupError, RuntimeError) as e:
        r.update(status="BROKEN", detail=str(e))
        return r
    now = sha(text)
    r["digest_now"], r["head"] = now, head[:9]
    exp = a.get("expires")
    if now != a.get("digest"):
        r["status"] = "STALE"
        ver = a.get("verified", "")
        since = git.log_since(repo, ver, fil) if ver and git.has_commit(repo, ver) else None
        if since is None:
            r["detail"] = "symbol changed; verified sha %s is not in %s (rewritten history?)" % (ver or "(missing)", repo)
        else:
            r["detail"] = "%s -> %s (symbol changed; %d commit(s) touched %s since)" % (ver[:9], head[:9], len(since), fil)
            r["commits"] = since[: (50 if explain else 3)]
    elif exp and exp < as_of:
        r["status"] = "EXPIRED"
        r["detail"] = "expires %s, re-verify%s" % (exp, (": " + a.get("check")) if a.get("check") else "")
    elif exp:
        r["detail"] = "expires %s" % exp
    return r


def check_canon(git, c, as_of):
    repo, path, ver = c.get("repo"), c.get("path", "CLAUDE.md"), c.get("sha", "")
    r = dict(doc=c.doc, line=c.line, id="canonical-ref", repo=repo, symbol="file:" + path, level=c.get("level", "warn"), status="FRESH", detail="")
    if not git.has_repo(repo):
        r.update(status="SKIPPED", detail="canonical repository %s is not available here" % repo)
        return r
    try:
        head = git.resolve_ref(repo)
        if not ver or not git.has_commit(repo, ver):
            r.update(status="BROKEN", detail="canonical-ref sha %r unknown in %s" % (ver, repo))
            return r
        since = git.log_since(repo, ver, path)
    except RuntimeError as e:
        r.update(status="BROKEN", detail=str(e))
        return r
    if since:
        r.update(status="STALE", detail="copy stamped at %s; canonical %s moved %d commit(s) since (latest: %s)" % (ver[:9], path, len(since), since[0]), commits=since[:3])
    return r


def cmd_check(a):
    git = Git(a.repos, a.ref)
    results = []
    for d in a.docs:
        _, anchors, canon = parse_doc(d)
        results += [check_anchor(git, x, a.as_of, a.explain) for x in anchors]
        results += [check_canon(git, x, a.as_of) for x in canon]
    bad = 0
    if a.json:
        print(json.dumps(dict(as_of=a.as_of, ref=a.ref, results=results), indent=1))
    else:
        for r in results:
            if r["status"] in ("FRESH", "SKIPPED") and not a.all:
                continue
            print("%-7s %-26s %s:%s  %s" % (r["status"], r["id"], os.path.basename(r["doc"]), r["line"], r["detail"] or r["symbol"]))
            for c in r.get("commits", []):
                print("          . " + c)
        tally = {}
        for r in results:
            tally[r["status"]] = tally.get(r["status"], 0) + 1
        print("-- %d anchors: %s" % (len(results), ", ".join("%d %s" % (v, k) for k, v in sorted(tally.items())) or "none"))
    for r in results:
        if r["status"] not in ("FRESH", "SKIPPED") and r["level"] == "error":
            bad = 1
    return bad


# ------------------------------------------------------------------------------------------ stamp / add
def fmt_anchor(attrs, order=("id", "repo", "symbol", "verified", "digest", "on", "level", "expires", "check")):
    parts = []
    for k in order:
        if attrs.get(k):
            v = attrs[k]
            parts.append('%s="%s"' % (k, v) if (" " in v) else "%s=%s" % (k, v))
    for k, v in attrs.items():
        if k not in order:
            parts.append("%s=%s" % (k, v))
    return "<!-- anchor " + " ".join(parts) + " -->"


def cmd_stamp(a):
    if not a.yes:
        print("stamping asserts that a human or agent just RE-VERIFIED the doc claim against the code; pass --yes to confirm.", file=sys.stderr)
        return 2
    git = Git(a.repos, a.ref)
    text, anchors, _ = parse_doc(a.doc)
    out, last, changed = [], 0, 0
    for x in anchors:
        if a.id and x.id != a.id:
            continue
        try:
            t, _f = resolve(git, x.get("repo"), x.get("symbol"))
        except (LookupError, RuntimeError) as e:
            print("skip %s: %s" % (x.id, e), file=sys.stderr)
            continue
        attrs = dict(x.attrs)
        attrs.update(verified=git.resolve_ref(x.get("repo"))[:9], digest=sha(t), on=a.as_of)
        out.append(text[last:x.span[0]])
        out.append(fmt_anchor(attrs))
        last = x.span[1]
        changed += 1
    out.append(text[last:])
    new = "".join(out)
    if a.dry_run:
        sys.stdout.write("".join(__import__("difflib").unified_diff(text.splitlines(True), new.splitlines(True), a.doc, a.doc + " (stamped)")))
    else:
        open(a.doc, "w", encoding="utf-8").write(new)
    print("stamped %d anchor(s)" % changed)
    return 0


def cmd_add(a):
    git = Git(a.repos, a.ref)
    try:
        t, _f = resolve(git, a.repo, a.symbol)
    except (LookupError, RuntimeError) as e:
        if not a.allow_broken:
            print("refusing to anchor a symbol that does not resolve: %s" % e, file=sys.stderr)
            return 1
        t = ""
    attrs = dict(id=a.id, repo=a.repo, symbol=a.symbol, verified=git.resolve_ref(a.repo)[:9], digest=sha(t), on=a.as_of)
    if a.level:
        attrs["level"] = a.level
    if a.expires:
        attrs["expires"] = a.expires
    lines = open(a.doc, encoding="utf-8").read().split("\n")
    lines.insert(a.line, fmt_anchor(attrs))
    open(a.doc, "w", encoding="utf-8").write("\n".join(lines))
    print(fmt_anchor(attrs))
    return 0


# ------------------------------------------------------------------------------------------ generators
def gen_verbs(git, attrs):
    repo = attrs.get("repo", "go_fleet_runner")
    vs = sorted(set(verbs_declared(git.show(repo, "main.go"))))
    rows = ["`%s`" % v for v in vs]
    # no commit sha in the header: it would make `gen --check` fail on every unrelated commit to the source repository
    return "\n_%d verbs, generated from the dispatch switch in `%s/main.go`; do not edit by hand._\n\n%s\n" % (len(vs), repo, ", ".join(rows))


def gen_registry_counts(git, attrs):
    repo = attrs.get("repo", "services-registry")
    txt = git.show(repo, "services.summary.txt")
    total = re.search(r"^total:\s*(\d+)", txt, re.M)
    kinds = re.findall(r"^\s+(\d+)\s+(container|static)\s*$", txt, re.M)
    meshes = re.findall(r"^\s+(\d+)\s+([\w-]+)\s*$", txt.split("## by mesh")[1].split("##")[0], re.M) if "## by mesh" in txt else []
    s = "\n_Generated from `%s/services.summary.txt`._\n\n" % repo
    s += "- registry entries: **%s**" % (total.group(1) if total else "?")
    s += " (" + ", ".join("%s %s" % (n, k) for n, k in kinds) + ")\n"
    if meshes:
        s += "- by mesh: " + ", ".join("%s %s" % (n, k) for n, k in meshes) + "\n"
    return s


def gen_pools(git, attrs):
    repo = attrs.get("repo", "fleet-state")
    d = json.loads(git.show(repo, "service-pools.json"))
    s = "\n_Generated from `%s/service-pools.json`. A row means the entry is MERGED; deployed/serving state needs a live probe._\n\n" % repo
    s += "| service | mode | replicas | strategy | targets | weights |\n|---|---|---|---|---|---|\n"
    for name, p in sorted(d.get("services", {}).items()):
        s += "| %s | %s | %s | %s | %s | %s |\n" % (name, p.get("mode", ""), p.get("replicas", ""), p.get("strategy", ""), ", ".join(p.get("targets", [])), json.dumps(p.get("weights", {})) if p.get("weights") else "-")
    return s


def gen_proxmox_inventory(git, attrs):
    """the guests of ONE Proxmox host from the weekly read-only snapshot (bin/proxmox_inventory.py), as a table.
    Status is left out on purpose: it changes faster than a document, and the snapshot date is not printed (it would break `gen --check`)."""
    repo = attrs.get("repo", "fleet-state")
    path = attrs.get("path", "inventory/proxmox-snapshot.json")
    host = attrs["host"]
    h = json.loads(git.show(repo, path))["hosts"].get(host)
    if h is None or "error" in h:
        raise SystemExit("host %s is not in %s/%s (or its entry is an error)" % (host, repo, path))
    s = "\n_Generated from `%s/%s` (a weekly read-only snapshot, `bin/proxmox_inventory.py`); do not edit by hand._\n\n" % (repo, path)
    s += "| ID | Name | Type | vCPUs | RAM (GiB) |\n|---|---|---|---|---|\n"
    for g in sorted(h["guests"], key=lambda g: (g["type"], g["vmid"])):
        s += "| %d | `%s`%s | %s | %d | %s |\n" % (g["vmid"], g["name"], " (template)" if g.get("template") else "", "VM" if g["type"] == "qemu" else "CT", g["cpus"], ("%g" % round(g["maxmem_mb"] / 1024.0, 1)))
    return s


GENERATORS = {"verbs": gen_verbs, "registry-counts": gen_registry_counts, "pools": gen_pools, "proxmox-inventory": gen_proxmox_inventory}


def cmd_gen(a):
    git = Git(a.repos, a.ref)
    rc = 0
    for d in a.docs:
        text = open(d, encoding="utf-8").read()

        def repl(m):
            g = GENERATORS.get(m.group("name"))
            if not g:
                raise SystemExit("no generator named %s" % m.group("name"))
            at = parse_attrs(m.group("attrs"))
            need = at.get("repo") or {"verbs": "go_fleet_runner", "registry-counts": "services-registry", "pools": "fleet-state", "proxmox-inventory": "fleet-state"}.get(m.group("name"))
            if need and not git.has_repo(need):
                print("%s: block %s skipped (repository %s is not available here)" % (d, m.group("name"), need), file=sys.stderr)
                return m.group(0)
            return m.group(1) + g(git, at) + m.group(5)

        new = GEN_RE.sub(repl, text)
        if new != text:
            rc = 1
            if a.write:
                open(d, "w", encoding="utf-8").write(new)
                print("%s: regenerated" % d)
            else:
                sys.stdout.write("".join(__import__("difflib").unified_diff(text.splitlines(True), new.splitlines(True), d, d + " (generated)")))
        else:
            print("%s: generated blocks up to date" % d)
    return 0 if (a.write or rc == 0) else 1


LONE_AMP = re.compile(r"(?<![&|>&\\])&(?![&>\d])(?=\s+[\w$\"'`(\\\-/.])")


def _strip_quoted(s):
    s = re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)
    return re.sub(r"'[^']*'", "''", s)


def lint_shell(path):
    """Flag a lone `&` used as a command separator in fenced bash blocks and inline code. A lone `&` backgrounds the left
    side, so `git add -A & git commit` races and `docker compose pull & up -d` starts before the pull ends. Found for real:
    a bulk edit (services-registry 1cdfe74, 2026-10-07) turned 13 `&&` into `&`. This is a doc-only corruption that no
    code-symbol anchor can see, so it needs its own check. Heuristic: warn, do not fail the build on its own."""
    out, fenced, lang = [], False, ""
    for i, ln in enumerate(open(path, encoding="utf-8").read().split("\n"), 1):
        st = ln.lstrip()
        if st.startswith("```"):
            fenced = not fenced
            lang = st[3:].strip().lower() if fenced else ""
            continue
        segs = []
        if fenced and lang in ("", "bash", "sh", "shell", "zsh", "console"):
            segs = [ln]
        elif not fenced:
            segs = re.findall(r"`([^`\n]+)`", ln)
        for seg in segs:
            code = re.sub(r"\s#.*$", "", _strip_quoted(seg))  # a trailing "# Security & Compliance" is prose
            if code.lstrip().startswith("#"):
                continue
            if LONE_AMP.search(code):
                out.append((i, ln.strip()))
                break
    return out


def cmd_lint(a):
    rc = 0
    for d in a.docs:
        hits = lint_shell(d)
        for i, t in hits:
            print("LINT    lone-&           %s:%d  %s" % (os.path.basename(d), i, t[:140]))
        print("-- %s: %d line(s) with a lone & used as a command separator" % (d, len(hits)))
        if hits:
            rc = 1
    return rc


LOCAL_MARK_RE = re.compile(r"^<!--\s*repo-local\b.*?-->\s*$", re.M)
LOCAL_MARK_TEXT = "<!-- repo-local: everything below this line belongs to this repository, is not part of the canonical, and survives `docdrift sync` -->"


def split_local_tail(text):
    """-> (canonical_body, local_tail or ''). The tail starts at the repo-local marker line."""
    m = LOCAL_MARK_RE.search(text)
    if not m:
        return text, ""
    return text[:m.start()], text[m.start():]


def blob_sha(text):
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def canonical_revisions(git, repo, path):
    """newest-first list of (commit, blob) for the canonical file at the ref."""
    ref = git.resolve_ref(repo)
    shas = [l for l in git._run(repo, "log", "--format=%H", ref, "--", path).split("\n") if l]
    out = []
    for c in shas:
        try:
            out.append((c, git._run(repo, "rev-parse", "%s:%s" % (c, path)).strip()))
        except RuntimeError:
            pass
    return out


def copy_status(revs, blob):
    """exact match of a copy's blob against the canonical history -> ('current'|'behind', n_behind, commit) or ('diverged', None, None)."""
    for i, (c, b) in enumerate(revs):
        if b == blob:
            return ("current", 0, c) if i == 0 else ("behind", i, c)
    return ("diverged", None, None)


def cmd_sync(a):
    canon = open(a.canonical, encoding="utf-8").read()
    cur = open(a.target, encoding="utf-8").read() if os.path.exists(a.target) else ""
    _, tail = split_local_tail(cur)
    body, _ = split_local_tail(canon)
    new = body + (("" if body.endswith("\n") else "\n") + tail if tail else "")
    if new == cur:
        print("%s: already identical to the canonical%s" % (a.target, " (+ local tail)" if tail else ""))
        return 0
    if a.write:
        open(a.target, "w", encoding="utf-8").write(new)
        print("%s: refreshed (%d -> %d lines%s)" % (a.target, cur.count("\n"), new.count("\n"), ", kept repo-local tail" if tail else ""))
        return 0
    sys.stdout.write("".join(__import__("difflib").unified_diff(cur.splitlines(True), new.splitlines(True), a.target, a.target + " (synced)", n=1)))
    return 1


def cmd_scan(a):
    git = Git(a.repos, a.ref)
    crepo = a.canonical_repo
    revs = canonical_revisions(git, crepo, a.canonical_path)
    docs, summary = [], collections_counter()
    for name in sorted(os.listdir(a.repos)):
        if not os.path.isdir(os.path.join(a.repos, name, ".git")):
            continue
        for path in ("CLAUDE.md", "AGENTS.md"):
            try:
                text = git.show(name, path)
            except RuntimeError:
                continue
            rec = dict(repo=name, path=path, anchors=[], canonical=None)
            tmp = None
            for m in ANCHOR_RE.finditer(text):
                x = Anchor(name + "/" + path, text.count("\n", 0, m.start()) + 1, parse_attrs(m.group(1)), m.span())
                r = check_anchor(git, x, a.as_of, False)
                rec["anchors"].append(dict(id=r["id"], status=r["status"], detail=r["detail"], symbol=r["symbol"]))
                summary["anchor_" + r["status"]] += 1
            for m in CANON_RE.finditer(text):
                x = Anchor(name + "/" + path, text.count("\n", 0, m.start()) + 1, parse_attrs(m.group(1)), m.span())
                r = check_canon(git, x, a.as_of)
                rec["canonical_ref"] = dict(status=r["status"], detail=r["detail"])
                summary["canonical_ref_" + r["status"]] += 1
            if path == a.canonical_path and name != crepo:
                body, tail = split_local_tail(text)
                st, behind, commit = copy_status(revs, blob_sha(body))
                rec["canonical"] = dict(status=st, revisions_behind=behind, matches_commit=(commit or "")[:9], has_local_tail=bool(tail), bytes=len(text))
                summary["copy_" + st] += 1
            docs.append(rec)
    out = dict(as_of=a.as_of, canonical_head=(revs[0][0][:9] if revs else ""), canonical_revisions=len(revs), summary=dict(summary), docs=docs)
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)
    for d in docs:
        c = d.get("canonical")
        if c and c["status"] != "current":
            print("COPY %-9s %-32s %s" % (c["status"].upper(), d["repo"], ("%d canonical revision(s) behind (matches %s)" % (c["revisions_behind"], c["matches_commit"])) if c["revisions_behind"] is not None else "diverged from every canonical revision"))
        for an in d["anchors"]:
            if an["status"] not in ("FRESH", "SKIPPED"):
                print("%-7s %-26s %s/%s  %s" % (an["status"], an["id"], d["repo"], d["path"], an["detail"]))
    print("-- scanned %d docs in %s: %s" % (len(docs), a.repos, ", ".join("%d %s" % (v, k) for k, v in sorted(summary.items())) or "nothing to report"))
    return 1 if any(k.startswith(("anchor_STALE", "anchor_BROKEN", "anchor_EXPIRED")) for k in summary) else 0


def collections_counter():
    import collections
    return collections.Counter()


def cmd_census(a):
    """Copy staleness of every repo in the registry, from GitHub blob shas only (needs an authenticated gh)."""
    import concurrent.futures
    git = Git(a.repos, a.ref)
    crepo = a.canonical_repo
    revs = canonical_revisions(git, crepo, a.canonical_path)
    reg = json.loads(git.show(crepo, "services.json"))
    ents = reg if isinstance(reg, list) else reg.get("services", reg)
    names = sorted({m.group(1).replace(".git", "") for e in ents for m in [re.search(r"github\.com/%s/([^/\s#]+)" % re.escape(a.owner), e.get("repo_url") or "")] if m})

    def blob(repo, path):
        p = subprocess.run(["gh", "api", "repos/%s/%s/contents/%s" % (a.owner, repo, path), "--jq", ".sha"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.stdout.decode(), p.stderr.decode()
        if p.returncode == 0 and out.strip():
            return out.strip()
        if "404" in err or "Not Found" in err or "Not Found" in out:
            return None  # the file really does not exist
        raise RuntimeError("%s/%s: %s" % (repo, path, (err or out).strip()[:160]))

    rl = subprocess.run(["gh", "api", "rate_limit", "--jq", ".resources.core.remaining"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        remaining = int(rl.stdout.decode().strip())
    except ValueError:
        remaining = None
    if remaining is not None and remaining < 2 * len(names) + 50:
        print("census needs ~%d GitHub API calls but only %d remain in this hour's quota; retry after the reset (gh api rate_limit)" % (2 * len(names), remaining), file=sys.stderr)
        return 3

    def one(r):
        try:
            return (r, blob(r, a.canonical_path), blob(r, "AGENTS.md"), None)
        except RuntimeError as e:
            return (r, None, None, str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        res = list(ex.map(one, names))
    limited = [r for r in res if r[3] and "rate limit" in r[3].lower()]
    if limited:
        print("census aborted: GitHub is rate limiting this account (%d calls refused). Nothing was recorded as missing. Wait, then rerun with fewer --workers." % len(limited), file=sys.stderr)
        return 3
    rows, tally = [], collections_counter()
    for repo, cb, ab, err in res:
        if err:
            tally["error"] += 1
            rows.append(dict(repo=repo, status="error", error=err))
            continue
        if cb is None:
            st, behind, commit = "missing", None, None
        else:
            st, behind, commit = copy_status(revs, cb)
        tally[st] += 1
        rows.append(dict(repo=repo, claude_blob=cb, status=st, revisions_behind=behind, matches_commit=(commit or "")[:9], has_agents_md=ab is not None))
    hist = collections_counter()
    for r in rows:
        if r["status"] == "behind":
            hist[r["revisions_behind"]] += 1
    out = dict(as_of=a.as_of, canonical_head=revs[0][0][:9] if revs else "", repos=len(rows), tally=dict(tally), behind_histogram=dict(sorted(hist.items())), agents_md=sum(1 for r in rows if r.get("has_agents_md")), rows=rows)
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)
    if tally.get("error"):
        print("WARNING: %d repos could not be read (see rows[].error); their status is unknown, not missing" % tally["error"], file=sys.stderr)
    print("census of %d repos at canonical %s: %s; AGENTS.md in %d" % (len(rows), out["canonical_head"], ", ".join("%d %s" % (v, k) for k, v in sorted(tally.items())), out["agents_md"]))
    print("revisions behind (histogram): " + ", ".join("%s:%d" % kv for kv in sorted(hist.items())))
    return 0


def cmd_resolve(a):
    git = Git(a.repos, a.ref)
    try:
        t, f = resolve(git, a.repo, a.symbol)
    except (LookupError, RuntimeError) as e:
        print("BROKEN: %s" % e)
        return 1
    print("repo=%s ref=%s file=%s\ndigest=%s\n--- first 12 normalised lines ---" % (a.repo, git.resolve_ref(a.repo)[:9], f, sha(t)))
    print("\n".join(t.split("\n")[:12]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos", required=True, help="directory containing the repo clones (<repos>/<repo>)")
    ap.add_argument("--ref", default="origin/main", help="ref to read (default origin/main; falls back to HEAD for detached clones)")
    ap.add_argument("--as-of", default=datetime.date.today().isoformat())
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("docs", nargs="+"); c.add_argument("--json", action="store_true"); c.add_argument("--explain", action="store_true"); c.add_argument("--all", action="store_true"); c.set_defaults(f=cmd_check)
    r = sub.add_parser("resolve"); r.add_argument("repo"); r.add_argument("symbol"); r.set_defaults(f=cmd_resolve)
    s = sub.add_parser("stamp"); s.add_argument("doc"); s.add_argument("--id"); s.add_argument("--yes", action="store_true"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_stamp)
    ad = sub.add_parser("add"); ad.add_argument("--doc", required=True); ad.add_argument("--line", type=int, required=True); ad.add_argument("--id", required=True); ad.add_argument("--repo", required=True); ad.add_argument("--symbol", required=True)
    ad.add_argument("--expires"); ad.add_argument("--level"); ad.add_argument("--allow-broken", action="store_true"); ad.set_defaults(f=cmd_add)
    g = sub.add_parser("gen"); g.add_argument("docs", nargs="+"); g.add_argument("--check", action="store_true"); g.add_argument("--write", action="store_true"); g.set_defaults(f=cmd_gen)
    li = sub.add_parser("lint"); li.add_argument("docs", nargs="+"); li.set_defaults(f=cmd_lint)
    sy = sub.add_parser("sync"); sy.add_argument("canonical"); sy.add_argument("target"); sy.add_argument("--write", action="store_true"); sy.set_defaults(f=cmd_sync)
    for nm, fn in (("scan", cmd_scan), ("census", cmd_census)):
        sp = sub.add_parser(nm); sp.add_argument("--json"); sp.add_argument("--canonical-repo", default="services-registry"); sp.add_argument("--canonical-path", default="CLAUDE.md")
        if nm == "census":
            sp.add_argument("--owner", default="baditaflorin")
            sp.add_argument("--workers", type=int, default=4)
        sp.set_defaults(f=fn)
    a = ap.parse_args(argv)
    try:
        return a.f(a)
    except (IOError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
