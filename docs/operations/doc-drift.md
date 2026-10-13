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

## Rolling the canonical out to the fleet

`bin/rollout_claude_md.py` copies this repository's `CLAUDE.md` into the fleet's repositories. It is deterministic (no LLM) and uses git over SSH, so GitHub's API quota is not spent.

- `plan --registry <clone> --out <dir> --repos-file <names>` clones every repository shallowly, then classifies its `CLAUDE.md` by blob against this file's history: `current`, `absent`, `behind` (equal to an older revision: replacing it loses nothing) or `diverged` (differs from every revision).
- `apply --out <dir> [--workers N] [--only a,b]` writes the canonical to the `absent` and `behind` repositories and pushes a docs-only commit with `[skip ci]`, so one commit does not start a pipeline in hundreds of repositories. A push that is rejected because the branch moved is re-fetched, re-classified and retried once; a protected default branch gets the branch `docs/claude-md-sync-<head>` and needs a PR. The log (`apply_log.jsonl`) is keyed by canonical head, so a later canonical rolls out again.
- `diverged --registry <clone> --out <dir>` lists, for each diverged copy, the canonical revision it is closest to and the lines that are the repository's own; `merge ... [--push]` does a three-way merge (base = that revision) and pushes only conflict-free results. Conflicts need a person: typical ones are a locally reworded Auth section or a `<!-- ...:managed -->` block that another tool maintains.

Run it from a machine whose SSH key can push to the repositories. Check the Woodpecker agents' logs after a run to see that no pipeline started.

## Evidence ledger: what was true, at which commit and version

An audit of a document produces many pieces of evidence of the form `repo/path:lines@sha`. `bin/evidence_ledger.py` keeps each one with what it said and where it was read, so the document can later say "this was true at go_fleet_runner v0.7.277; the code is now 33 releases further".

- `build --claims <claims.jsonl> --repos <dir with clones> --out ledger.jsonl` stores one row per evidence range: the claim, the repository, the path and lines, the commit it was read at (`stamp_sha`), the nearest release tag at that commit (`stamp_tag`, from `git describe`), a digest of the whitespace-normalised snippet and the snippet itself (at most 15 lines).
- `check --ledger ledger.jsonl --repos <dir> [--json out.json] [--md out.md]` compares each row with the repository's default branch: `FRESH` (same text, same lines), `MOVED` (same text, other lines), `CHANGED` (the text is gone: the report gives the diff and how many commits and release tags have passed since the stamp), `GONE` (file deleted) or `NO-REPO` (not cloned here). The Markdown report has a per-document table and a per-repository table "read at vX (sha), now vY: N commits, M tags later".

A ledger holds code snippets, so keep it in a private repository (fleet-state), not here. A snippet with a credential-like line (password, token, secret, api key followed by a value) is never stored: the row keeps only the digest (`redacted: true`) and its diff is withheld. `check` reads git objects only and changes nothing.
