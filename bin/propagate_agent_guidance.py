#!/usr/bin/env python3
"""Idempotently roll out fleet agent/release-receipt guidance in small batches."""
from __future__ import annotations
import argparse, json, os, re, subprocess, tempfile
from datetime import datetime, timezone
from pathlib import Path
MARKER = "<!-- fleet-release-receipts:managed -->"
CI_PATH = """
      path:
        exclude:
          - CLAUDE.md
          - AGENTS.md"""
def run(args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp"); tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600); tmp.replace(path)
def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default
def supports_docs_only_ci(text):
    return "path:" in text and "CLAUDE.md" in text and "AGENTS.md" in text
def add_docs_only_ci(path):
    text = path.read_text()
    if supports_docs_only_ci(text): return False
    pattern = r"^(\s*)when:\n\1  event:\s*\[push,\s*pull_request\]\s*$"
    match = re.search(pattern, text, flags=re.M)
    if not match or len(re.findall(r"^\s*when:\s*$", text, flags=re.M)) != 1:
        raise ValueError("unsupported Woodpecker shape; preserve for manual CI migration")
    indent = match.group(1)
    insertion = match.group(0) + "\n" + indent + CI_PATH.lstrip("\n")
    path.write_text(text[:match.start()] + insertion + text[match.end():]); return True
def append_managed(path, content):
    old = path.read_text() if path.exists() else ""
    if MARKER in old: return False
    suffix = "" if not old or old.endswith("\n") else "\n"
    path.write_text(old + suffix + "\n" + MARKER + "\n\n" + content.strip() + "\n"); return True
def targets(registry, workspace):
    rows = json.loads(registry.read_text())
    if isinstance(rows, dict): rows = rows.get("services", [])
    out = []
    for item in rows:
        if item.get("kind") != "container": continue
        repo = item.get("repo") or item.get("repo_name") or item.get("id")
        if repo and (workspace / repo).is_dir(): out.append((str(item.get("id", repo)), repo))
    return sorted(set(out))
def update_one(repo, workspace, agents_text, clause_text, apply):
    source = workspace / repo; run(["git","-C",str(source),"fetch","origin","main"])
    with tempfile.TemporaryDirectory(prefix="agent-guidance-", dir="/root/wt") as td:
        wt = Path(td) / repo; run(["git","-C",str(source),"worktree","add","--detach",str(wt),"origin/main"])
        try:
            files=[]; ci=wt/".woodpecker.yml"
            if ci.exists():
                try:
                    if add_docs_only_ci(ci): files.append(".woodpecker.yml")
                except ValueError as exc: return "blocked_ci_shape",str(exc)
            elif (wt/".woodpecker.yaml").exists() or (wt/".woodpecker").exists(): return "blocked_ci_shape","nonstandard Woodpecker configuration"
            else: return "blocked_ci_missing","no root Woodpecker configuration"
            if append_managed(wt/"AGENTS.md",agents_text): files.append("AGENTS.md")
            if append_managed(wt/"CLAUDE.md",clause_text): files.append("CLAUDE.md")
            if not files: return "already_current",""
            if not apply: return "would_update",",".join(files)
            run(["git","add","--",*files],cwd=wt)
            run(["git","-c","user.name=Florin","-c","user.email=baditaflorin@gmail.com","commit","-m","docs: codify fleet release receipts"],cwd=wt)
            run(["git","push","origin","HEAD:main"],cwd=wt); return "updated",",".join(files)
        finally: run(["git","-C",str(source),"worktree","remove","--force",str(wt)],check=False)
def main():
    p=argparse.ArgumentParser(); p.add_argument("--registry",type=Path,default=Path("services.json")); p.add_argument("--workspace",type=Path,default=Path("/root/workspace")); p.add_argument("--state",type=Path,default=Path("/var/lib/fleet-runner/agent-guidance-rollout.json")); p.add_argument("--batch-size",type=int,default=3,choices=range(1,4)); p.add_argument("--apply",action="store_true"); args=p.parse_args()
    root=Path(__file__).resolve().parents[1]; agents=(root/"AGENTS.md").read_text(); clause=(root/"CLAUDE.md").read_text().split("## Canonical release receipts",1)[1]
    state=load_json(args.state,{"version":1,"completed":{},"runs":[]}); pending=[(sid,repo) for sid,repo in targets(args.registry,args.workspace) if repo not in state["completed"]]; batch=pending[:args.batch_size]; results=[]
    for sid,repo in batch:
        status,detail=update_one(repo,args.workspace,agents,clause,args.apply); results.append({"service":sid,"repo":repo,"status":status,"detail":detail})
        if status in {"updated","already_current","blocked_ci_shape","blocked_ci_missing"}: state["completed"][repo]=status
    state["runs"].append({"at":datetime.now(timezone.utc).isoformat(),"results":results}); state["runs"]=state["runs"][-200:]; atomic_json(args.state,state); print(json.dumps({"batch":results,"remaining":len(pending)-len(batch)},indent=2))
if __name__ == "__main__": main()
