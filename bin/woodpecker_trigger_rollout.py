#!/usr/bin/env python3
"""Move per-repo Woodpecker triggers to: pull_request + push to the default branch + manual.

Fleet repos carry a root .woodpecker.yml whose single step is filtered with
`event: [push, pull_request]`. A push to a PR branch therefore starts a second
pipeline identical to the PR one. This tool rewrites ONLY repos whose file is
exactly that template shape into a workflow-level `when:` list (see
templates/woodpecker-go-ci.yml). Anything else is reported as SPECIAL and left
alone. It has no third-party dependencies (CI runs on python:alpine).

  classify  FILE [--default-branch main]     print STANDARD/SPECIAL and why
  apply     REPO... --mode pr|direct         fresh shallow clone, rewrite, push (direct) or PR
  rollback  REPO... --state FILE             git revert the commit this tool pushed

Every apply/rollback records repo, before/after SHAs in the --state JSON.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVENTS_OK = {"push", "pull_request", "manual"}
SAFE_IMAGE = re.compile(r"^(golang|node):[A-Za-z0-9._-]+$")
# Commands that suggest release/deploy/side-effect logic: never auto-migrate those.
DENY_CMD = re.compile(
    r"docker|publish|deploy|cosign|\bssh\b|\bscp\b|\bcurl\b|\bwget\b|fleet-runner|from_secret|"
    r"CI_COMMIT_TAG|CI_PIPELINE_EVENT|CI_COMMIT_BRANCH|git\s+push|\bgh\s|release|\btag\b",
    re.I,
)
PATH_BLOCK = ["path:", "exclude:", "- CLAUDE.md", "- AGENTS.md"]


class Special(Exception):
    """The file is not the standard template shape; the message is the reason."""


def _lines(text: str):
    if "\t" in text:
        raise Special("tab characters")
    out = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append((len(raw) - len(raw.lstrip(" ")), s))
    return out


def parse_standard(text: str) -> dict:
    """Return {'events': [...], 'docs_only_skip': bool, 'has_when': bool} or raise Special."""
    ls = _lines(text)
    if len(ls) < 6 or ls[0] != (0, "steps:"):
        raise Special("top level is not exactly `steps:` (extra top-level keys, services, clone or a workflow-level when)")
    if any(i == 0 for i, _ in ls[1:]):
        raise Special("extra top-level keys")
    if ls[1][0] != 2 or not ls[1][1].endswith(":") or ls[1][1].startswith("-"):
        raise Special("steps is not a mapping with a single named step")
    if any(i == 2 for i, _ in ls[2:]):
        raise Special("more than one step")
    body = ls[2:]
    image = None
    commands: list[str] = []
    events = None
    docs_only = False
    has_when = False
    i = 0
    while i < len(body):
        ind, s = body[i]
        if ind != 4:
            raise Special(f"unexpected nesting: {s[:50]!r}")
        if s.startswith("image:"):
            image = s.split(":", 1)[1].strip()
            i += 1
        elif s == "pull: true":
            i += 1
        elif s == "commands:":
            i += 1
            while i < len(body) and body[i][0] == 6 and body[i][1].startswith("- "):
                cmd = body[i][1][2:]
                if re.search(r"[|>][+-]?$", cmd) or cmd.endswith("\\"):
                    raise Special("multi-line command")
                commands.append(cmd)
                i += 1
        elif s == "when:":
            has_when = True
            i += 1
            blk = []
            while i < len(body) and body[i][0] > 4:
                blk.append(body[i])
                i += 1
            if not blk or blk[0][0] != 6 or not blk[0][1].startswith("event: ["):
                raise Special("step when is not a one-line `event: [..]` mapping")
            m = re.fullmatch(r"event: \[([a-z_, ]+)\]", blk[0][1])
            if not m:
                raise Special("unparsable event list")
            events = [e.strip() for e in m.group(1).split(",")]
            rest = blk[1:]
            if rest:
                if [s2 for _, s2 in rest] != PATH_BLOCK or [d for d, _ in rest] != [6, 8, 10, 10]:
                    raise Special("step when has conditions other than the CLAUDE.md/AGENTS.md path exclude")
                docs_only = True
        else:
            raise Special(f"step key {s.split(':')[0]!r} is not part of the template")
    if image is None or not SAFE_IMAGE.match(image):
        raise Special(f"image {image!r} is not golang:/node:")
    if not commands:
        raise Special("no commands")
    for c in commands:
        if DENY_CMD.search(c):
            raise Special(f"command looks like release/deploy logic: {c[:60]!r}")
    if not has_when:
        raise Special("no trigger filter: runs on every event (tag, deployment, ...); not the injected template")
    if not set(events) <= EVENTS_OK:
        raise Special(f"events {events} include more than push/pull_request/manual (e.g. tag)")
    if not {"push", "pull_request"} <= set(events):
        raise Special(f"events {events} are not the push+pull_request duplicate shape")
    return {"events": events, "docs_only_skip": docs_only, "has_when": has_when}


def classify(text: str, *, has_dir: bool = False, has_yaml: bool = False, archived: bool = False) -> tuple[str, str]:
    if archived:
        return "SPECIAL", "archived repository"
    if has_dir:
        return "SPECIAL", "has a .woodpecker/ directory (multi-workflow)"
    if has_yaml:
        return "SPECIAL", "has a .woodpecker.yaml as well"
    try:
        parse_standard(text)
    except Special as exc:
        return "SPECIAL", str(exc)
    return "STANDARD", "template shape"


def _when_block(default_branch: str, docs_only: bool) -> list[str]:
    path = ["    path:", "      exclude:", "        - CLAUDE.md", "        - AGENTS.md"] if docs_only else []
    return (
        [
            "# Triggers: one pipeline per PR update and one per push to " + default_branch + ";",
            "# a push to a PR branch is covered by its pull_request run, so it no longer",
            "# starts a second identical pipeline (services-registry templates/woodpecker-go-ci.yml).",
            "when:",
            "  - event: pull_request",
        ]
        + path
        + ["  - event: push", "    branch: " + default_branch]
        + path
        + ["  - event: manual", ""]
    )


def transform(text: str, default_branch: str = "main") -> str:
    """Rewrite a STANDARD file; raises Special if it is not one."""
    info = parse_standard(text)
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", default_branch):
        raise Special("odd default branch name")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    removed = 0
    while i < len(lines):
        if lines[i] == "    when:":
            i += 1
            removed += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip(" ")) > 4):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if removed != 1:
        raise Special("could not locate the step-level when block")
    # insert before `steps:`
    idx = next(k for k, l in enumerate(out) if l == "steps:")
    new = out[:idx] + _when_block(default_branch, info["docs_only_skip"]) + out[idx:]
    result = "\n".join(new).rstrip("\n") + "\n"
    verify_transformed(text, result)
    return result


def verify_transformed(old: str, new: str) -> None:
    """The step is unchanged apart from its when block, and the file now has a top-level when."""
    old_steps = [s for ind, s in _lines(old)]
    new_lines = _lines(new)
    if new_lines[0] != (0, "when:") and not any(l == (0, "when:") for l in new_lines):
        raise Special("verification: no top-level when")
    top = [l for l in new_lines if l[0] == 0]
    if [s for _, s in top] != ["when:", "steps:"]:
        raise Special("verification: top-level keys are not exactly when, steps")
    new_steps = [s for ind, s in new_lines[[k for k, l in enumerate(new_lines) if l == (0, "steps:")][0]:]]
    # remove the old step-level when block from old_steps for comparison
    cmp_old = []
    skip = False
    for ind, s in _lines(old):
        if ind == 4 and s == "when:":
            skip = True
            continue
        if skip and ind > 4:
            continue
        skip = False
        cmp_old.append(s)
    if cmp_old != new_steps:
        raise Special("verification: step content changed")
    if len(old_steps) < 3:
        raise Special("verification: too short")


def default_branch_of(repo: str) -> str:
    # git protocol, not the REST API: the 5000/h REST budget is shared with every other agent
    out = run(["git", "ls-remote", "--symref", f"https://github.com/{repo}.git", "HEAD"])
    m = re.match(r"ref: refs/heads/(\S+)\tHEAD", out)
    if not m:
        raise RuntimeError("cannot resolve default branch for " + repo)
    return m.group(1)


def run(args, cwd=None, check=True) -> str:
    p = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(args[:3])}: {p.stderr.strip()[:300]}")
    return p.stdout


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"repos": {}}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


MSG = "ci: run Woodpecker on PRs and the default branch only (no duplicate push+PR pipelines)"
TRAILER = "\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


def apply_repo(repo: str, mode: str, state: dict, dry_run: bool) -> dict:
    res = {"repo": repo, "mode": mode}
    with tempfile.TemporaryDirectory(prefix="wp-trigger-") as td:
        wt = Path(td) / "r"
        db = default_branch_of(repo)
        run(["git", "clone", "-q", "--depth", "20", "--branch", db, f"https://github.com/{repo}.git", str(wt)])
        f = wt / ".woodpecker.yml"
        listing = {p.name for p in wt.iterdir()}
        if not f.exists():
            return {**res, "status": "special", "reason": "no root .woodpecker.yml"}
        text = f.read_text()
        kind, why = classify(text, has_dir=".woodpecker" in listing, has_yaml=".woodpecker.yaml" in listing)
        if kind != "STANDARD":
            return {**res, "status": "special", "reason": why}
        new = transform(text, db)
        before = run(["git", "rev-parse", "HEAD"], cwd=wt).strip()
        if dry_run:
            return {**res, "status": "would-change", "before": before}
        f.write_text(new)
        run(["git", "add", ".woodpecker.yml"], cwd=wt)
        if mode == "pr":
            br = "ci/woodpecker-pr-main-triggers"
            run(["git", "checkout", "-q", "-b", br], cwd=wt)
            run(["git", "commit", "-q", "-m", MSG + TRAILER], cwd=wt)
            run(["git", "push", "-q", "origin", br], cwd=wt)
            body = "One pipeline per PR update and one per push to the default branch; pushes to PR branches no longer duplicate the PR run.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
            url = run(["gh", "pr", "create", "--repo", repo, "--base", db, "--head", br, "--title", MSG, "--body", body], cwd=wt).strip()
            res.update(status="pr", pr=url, before=before)
        else:
            run(["git", "commit", "-q", "-m", MSG + TRAILER], cwd=wt)
            for attempt in (1, 2):
                p = subprocess.run(["git", "push", "-q", "origin", f"HEAD:{db}"], cwd=wt, text=True, capture_output=True)
                if p.returncode == 0:
                    break
                if attempt == 2:
                    return {**res, "status": "push-failed", "reason": p.stderr.strip()[:200]}
                run(["git", "pull", "-q", "--rebase", "origin", db], cwd=wt)
            after = run(["git", "rev-parse", "HEAD"], cwd=wt).strip()
            res.update(status="pushed", before=before, after=after)
    state["repos"][repo] = res
    return res


def rollback_repo(repo: str, state: dict) -> dict:
    rec = state["repos"].get(repo)
    if not rec or not rec.get("after"):
        return {"repo": repo, "status": "no-direct-commit-recorded"}
    with tempfile.TemporaryDirectory(prefix="wp-trigger-rb-") as td:
        wt = Path(td) / "r"
        db = default_branch_of(repo)
        run(["git", "clone", "-q", "--depth", "50", "--branch", db, f"https://github.com/{repo}.git", str(wt)])
        p = subprocess.run(["git", "revert", "--no-edit", rec["after"]], cwd=wt, text=True, capture_output=True)
        if p.returncode:
            run(["git", "revert", "--abort"], cwd=wt, check=False)
            return {"repo": repo, "status": "revert-conflict", "reason": p.stderr.strip()[:200]}
        run(["git", "commit", "-q", "--amend", "-m", "Revert: " + MSG + TRAILER], cwd=wt)
        run(["git", "push", "-q", "origin", f"HEAD:{db}"], cwd=wt)
        rec["reverted"] = run(["git", "rev-parse", "HEAD"], cwd=wt).strip()
        return {"repo": repo, "status": "reverted", "sha": rec["reverted"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify")
    c.add_argument("file")
    c.add_argument("--default-branch", default="main")
    c.add_argument("--print", action="store_true", dest="show")
    a = sub.add_parser("apply")
    a.add_argument("repos", nargs="+")
    a.add_argument("--mode", choices=["pr", "direct"], required=True)
    a.add_argument("--state", type=Path, required=True)
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--pause", type=float, default=3.0, help="seconds between repos (keeps the CI queue calm)")
    r = sub.add_parser("rollback")
    r.add_argument("repos", nargs="+")
    r.add_argument("--state", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "classify":
        text = Path(args.file).read_text()
        kind, why = classify(text)
        print(kind, why)
        if kind == "STANDARD" and args.show:
            print(transform(text, args.default_branch), end="")
        return 0
    state = load_state(args.state)
    for n, repo in enumerate(args.repos):
        try:
            res = apply_repo(repo, args.mode, state, args.dry_run) if args.cmd == "apply" else rollback_repo(repo, state)
        except Exception as exc:  # keep going: one repo must not stop the wave
            res = {"repo": repo, "status": "error", "reason": str(exc)[:300]}
        print(json.dumps(res), flush=True)
        save_state(args.state, state)
        if args.cmd == "apply" and not args.dry_run and n + 1 < len(args.repos):
            time.sleep(args.pause)
    return 0


if __name__ == "__main__":
    sys.exit(main())
