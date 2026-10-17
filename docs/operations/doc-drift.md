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
- `apply --out <dir> [--workers N] [--only a,b]` writes the canonical to the `absent` and `behind` repositories and pushes a docs-only commit with `[skip ci]`, so one commit does not start a pipeline in hundreds of repositories. A push that is rejected because the branch moved is re-fetched, re-classified and retried once; a protected default branch gets the branch `docs/claude-md-sync-<head>` and needs a PR. The log (`apply_log.jsonl`) is keyed by canonical head, so a later canonical rolls out again. The commit that goes to the sync branch of a repository with a protected default branch is the exception: it has no `[skip ci]`, because a ruleset there may require the push-pipeline status and a skipped commit would leave its PR `BLOCKED` for good (mesh-card-sorter#9, 2026-10-16).
- `diverged --registry <clone> --out <dir>` lists, for each diverged copy, the canonical revision it is closest to and the lines that are the repository's own; `merge ... [--push]` does a three-way merge (base = that revision) and pushes only conflict-free results. Conflicts need a person: typical ones are a locally reworded Auth section or a `<!-- ...:managed -->` block that another tool maintains.

- `merge --replace-safe` (on by default in `weekly`): a diverged copy with **no lines of its own** (every non-blank line also exists in the canonical revision it is closest to) only lacks canonical lines. A three-way merge cannot bring those back (it keeps a missing line as a local deletion, so `merge` reported `unchanged` for 53 copies on 2026-10-17 and the "copies behind" alert never cleared); this mode replaces the file with the canonical and lists the restored lines in the commit message. A copy with lines of its own is never replaced.
- `merge --prefer-canonical --push` merges every diverged copy: where both sides inserted at the same place both are kept, where both changed the same lines the **canonical wins**. The commit message lists the local wording that was replaced (it stays in the repository's history).
- `weekly --registry <clone> --work <dir> --extra <file>` does all of it in one go: repository list = `services.json` (repo_url) plus the infrastructure repositories in `<file>`, then plan, apply, merge of the diverged copies, and a PR for each repository whose default branch is protected (the tool owns one branch per repository, `docs/claude-md-sync`, and updates it in place). It writes `<dir>/reports/<date>.json` and `latest.json` and exits non-zero when a push failed or a repository could not be reached.

Run it from a machine whose SSH key can push to the repositories. Check the Woodpecker agents' logs after a run to see that no pipeline started.

### Weekly schedule

`bin/rollout-weekly.sh` is what the schedule runs: it updates its own clone of this repository to origin/main and runs `bin/rollout_claude_md.py weekly`, so the schedule always uses the merged version of the tool. On the maintainer's Mac it is a LaunchAgent (`~/Library/LaunchAgents/org.badita.fleet-claude-rollout.plist`, Mondays 06:30, a missed run happens on wake); the log is `~/Library/Logs/fleet-claude-rollout.log`, the state `~/.local/share/fleet-claude-rollout/`. The builder cannot do it: its GitHub key is read-only.

## Evidence ledger: what was true, at which commit and version

An audit of a document produces many pieces of evidence of the form `repo/path:lines@sha`. `bin/evidence_ledger.py` keeps each one with what it said and where it was read, so the document can later say "this was true at go_fleet_runner v0.7.277; the code is now 33 releases further".

- `build --claims <claims.jsonl> --repos <dir with clones> --out ledger.jsonl` stores one row per evidence range: the claim, the repository, the path and lines, the commit it was read at (`stamp_sha`), the nearest release tag at that commit (`stamp_tag`, from `git describe`), a digest of the whitespace-normalised snippet and the snippet itself (at most 15 lines).
- `check --ledger ledger.jsonl --repos <dir> [--json out.json] [--md out.md]` compares each row with the repository's default branch: `FRESH` (same text, same lines), `MOVED` (same text, other lines), `CHANGED` (the text is gone: the report gives the diff and how many commits and release tags have passed since the stamp), `GONE` (file deleted) or `NO-REPO` (no clone with a `.git` here; `--alias NAME=PATH` points a repository name at another checkout, e.g. `--alias fleet-state=/var/lib/fleet-docdrift/fleet-state`). The Markdown report has a per-document table and a per-repository table "read at vX (sha), now vY: N commits, M tags later".

A ledger holds code snippets, so keep it in a private repository (fleet-state), not here. A snippet with a credential-like line (password, token, secret, api key followed by a value) is never stored: the row keeps only the digest (`redacted: true`) and its diff is withheld. `check` reads git objects only and changes nothing.

## Inventory check: a new Proxmox host or guest must reach the documentation

2026-10-14 showed the failure: the fifth Proxmox host (0infer) was added to the builder pool, the CI capacity notes and the DNS export, but `OPS.md` still said "four Proxmox hosts", and a VM appeared and another one was resized the same day the section about them was merged.
`bin/proxmox_inventory.py snapshot` reads, with one read-only `ssh` per host, every Proxmox host's version and its VMs and containers (id, name, vCPUs, memory) into `fleet-state/inventory/proxmox-snapshot.json`.
`bin/inventory_weekly.py` (run by `bin/inventory-weekly.sh`, a weekly LaunchAgent on the maintainer's Mac) does the rest:

- the guest tables in the docs are **generated** from the snapshot (`<!-- BEGIN GENERATED:proxmox-inventory host=<id> -->`, `docdrift gen`), so sizes are never typed by hand;
- `bin/inventory_check.py` compares the docs with the inventory: a host no document mentions, a wrong "`<N>` Proxmox hosts", and a guest that is **new** since the baseline and that no document mentions by name or as `VM <id>`; guests that existed when the baseline was taken do not have to be listed;
- while there are findings, `inventory/proxmox-baseline.json` is not advanced and one GitHub issue ("Docs drift: Proxmox inventory vs documentation") is kept up to date; it closes itself on the next clean run; a macOS notification is shown;
- deliberate exceptions go in `inventory/inventory-allow.json` (`{"guests": [...], "hosts": [...]}`).

`inventory/proxmox-hosts.json` lists the hosts as `{"id", "alias"}` (an ssh alias, no addresses). A host that cannot be read changes nothing and is reported.

## Placement policy table

`host-placement-policy.json` changes in waves: on 2026-10-14 the allowlist of `infer-runtime` went from one service to about 170 in a day. A document that lists hosts, their state or their allowlist therefore holds a **generated** table (`<!-- BEGIN GENERATED:placement-policy -->`, `docdrift gen`): one row per logical host with cluster, state, roles, runtimes, placement classes and the size of the allowlist (a count, never the list). The nightly job runs `gen --check`, so a policy change that is merged without regenerating the table shows up as drift on the next run.

## Volatile claims added by a change

A count with a unit ("12 VMs", "64 GiB"), or a word like "currently" or "not yet", goes stale without anyone touching the document. `bin/docdrift.py volatile --base origin/main DOC...` looks only at the lines a change adds and lists those whose paragraph has no date (`2026-10-14`, "as of"), version (`v0.7.393`), anchor or pointer to the command that prints the value (``run `...` ``, ``see `...` ``, ``lists `...` ``). Code fences and GENERATED blocks are skipped, and existing text is never flagged. It is advisory (exit 0); `--strict` exits 1 so a repository can gate on it. The fix is one of: date the sentence, name the command, or generate the value.

## CLAUDE.md must stay prettier-clean

The weekly rollout copies `CLAUDE.md` into every repository. 68 `mesh-*` repositories run `prettier --check .` (prettier ^3.7.4, `printWidth` 100, no `.prettierignore`), so a canonical that prettier would change fails their next real CI run, and the rollout's `[skip ci]` commits hide that until then (found 2026-10-15 in mesh-card-sorter#9). The `claude-md-format` step in `.woodpecker.yml` runs the same check on `CLAUDE.md`. To fix a failure: `npx prettier@3.7.4 --config <the same json> --write CLAUDE.md`, then `docdrift gen --write CLAUDE.md` (the GENERATED verbs block is emitted in prettier's shape, with a blank line after the BEGIN and before the END comment). Avoid `_emphasis_` next to a `snake_case` word: prettier misreads it and is not idempotent; use `**bold**` or backticks.

## What the nightly scan counts

The scan reads `origin/<default branch>` of every clone in the builder workspace, so the nightly job first runs a bounded `git fetch` of each clone (fleet-state `scripts/docdrift-nightly.sh`; the metric `fleet_docdrift_workspace_clones{result="fetched|failed"}` says how many worked, and many failures mean the copy counts are not to be trusted). Each clone is counted once by its real path: the workspace also holds about 360 symlinks named after services (`ipv6-checker` -> `go_ipv6_checker`), which used to be scanned as extra repositories (found 2026-10-16: 884 "copies" for 534 clones). The independent check of a rollout is `rollout_claude_md.py plan` over the repository list: it classifies each repository from its remote tip over SSH, with no GitHub API quota.

## Which repositories the rollout maintains

The weekly rollout maintains every repository in `services.json` (`repo_url`) plus the **extra repositories** listed in fleet-state `rollout/extra_repos.txt` (infrastructure repositories that carry a copy, and the personal repositories that were given one). That file is the source of truth; `bin/rollout-weekly.sh` fetches it before every run and keeps the local `extra_repos.txt` in the work directory only as a cache and as the fallback when fleet-state cannot be reached or has no list yet. To bring another repository under the rollout, add its name to that file with a pull request. A repository that holds a copy but is on neither list shows up in the nightly scan as "behind" (`fleet_docdrift_copies`), which is the prompt to decide: add it, or remove its copy.
