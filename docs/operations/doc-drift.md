# Keeping agent docs true: drift detection

`bin/docdrift.py` compares what CLAUDE.md and AGENTS.md say with the code and data they describe. It only reads git objects (never a working tree), so a stale checkout cannot fool it.

| check | where it runs | what it catches |
|---|---|---|
| `lint` | CI (step `docs-drift`) | a lone `&` used as a command separator in a shell recipe (it backgrounds the left command) |
| `gen --check` | CI | a `GENERATED` block that no longer matches its source |
| `check` | CI for anchors in this repository; nightly on the builder for all others | an anchor whose symbol digest changed (STALE), vanished (BROKEN) or passed its `expires` (EXPIRED) |
| `scan` | nightly on the builder (fleet-state `scripts/docdrift-nightly.sh`, writes `state/doc-drift.json`) | which repo clones carry which canonical revision and how many revisions behind |

## Anchors

```
<!-- anchor id=<slug> repo=<repo> symbol=<go:file#Func | verb:<name> | json:file#/ptr | regex:file#pat | file:<path>>
     verified=<short sha of that repo when verified> digest=sha256:<hash of the normalised symbol> on=<YYYY-MM-DD> [level=warn|error] [expires=<YYYY-MM-DD>] -->
```

The digest is the change signal; the sha only tells you where to look (`git log <sha>..origin/main -- <file>`). Anchors are block-level HTML comments, so Claude Code does not load them into context.
A pattern inside a `regex:` symbol cannot contain a double quote; write `.` for it.
Anchor narrow symbols: one that exceeds a few thousand characters (a whole JSON file, a large verb handler) changes on unrelated edits and flaps.

When an anchor goes STALE: read `git diff <verified>..origin/main -- <symbol file>`, correct the doc text if the claim changed, then `python3 bin/docdrift.py --repos <dir> stamp --yes --id <id> CLAUDE.md`. A stamp asserts that someone just re-verified the claim; it is never an automatic refresh.

## Copies

A repository's CLAUDE.md is a byte-for-byte copy of this repository's `CLAUDE.md`, optionally followed by a repo-local tail that starts at the line
`<!-- repo-local: ... -->`. Refresh with `python3 bin/docdrift.py --repos <dir> sync <canonical CLAUDE.md> <repo>/CLAUDE.md --write` (the tail is preserved).
Staleness is read off git: a copy that equals an old canonical revision is "N revisions behind"; `docdrift census` does this for every registry repo from GitHub blob shas.
