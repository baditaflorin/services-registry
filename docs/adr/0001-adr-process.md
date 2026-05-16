# ADR-0001 — Adopt Architecture Decision Records for the fleet

* **Status**: Accepted
* **Date**: 2026-05-16
* **Authors**: baditaflorin + claude-opus-4-7
* **Tags**: process, governance

## Context

The fleet now spans ~280 services. We've made many irreversible architectural calls (BEGIN IMMEDIATE on pinned `*sql.Conn`, `:metrics` middleware short-circuit, compose-tag-pinning, fail-closed scope-guard semantics, audit-log-as-prefix-only, etc.). These decisions are buried in commit messages, fleet-runner output, and per-repo CLAUDE.md propagation. New agents (and humans) keep rediscovering them.

We need a single durable surface for **decisions that we wouldn't want a future agent to silently reverse**.

## Decision

Adopt [Architecture Decision Records](https://adr.github.io/) under `services-registry/docs/adr/`. The pattern:

- One ADR per cross-cutting decision.
- File name: `NNNN-kebab-title.md` where `NNNN` is monotonic 4-digit, zero-padded.
- Required sections: `Status`, `Date`, `Context`, `Decision`, `Consequences`, optionally `Alternatives considered`.
- `Status` ∈ {Proposed, Accepted, Superseded by ADR-NNNN, Deprecated}.
- Never delete an ADR — supersede with a new one and link both ways.
- Each new fleet service that introduces a new primitive class gets its own ADR (per-service infra decisions are too narrow).

ADR-0001 (this file) seeds the directory and is itself the meta-decision.

## Consequences

**Positive:**
- Future agents read `docs/adr/` cold and immediately know what's load-bearing.
- Reversals become explicit ("ADR-0017 supersedes ADR-0009") — no silent drift.
- Forces us to write down the *why*, not just the *what* (commit messages have the what).

**Negative:**
- One more thing to maintain. Mitigated by: ADRs are short, written at decision-time (not retroactively), and exist for big calls only — most PRs need no ADR.

**Workflow:**
- When proposing: PR contains the new ADR with `Status: Proposed`.
- When accepted: change to `Accepted` in the same merge.
- When superseding: write the new ADR with `Supersedes ADR-XXXX`, then edit the old one's `Status` to `Superseded by ADR-YYYY`.

## Alternatives considered

1. **Status quo (decisions in commit messages + CLAUDE.md inline)** — proven insufficient at our service count. Lost decisions: 6+ rediscovered during the 2026-05-16 batch sessions (job-queue BEGIN IMMEDIATE, /metrics middleware, compose-tag pinning, gateway loopback false-smoke, version-backwards anti-pattern, INTEGRATIONS.md per-repo convention).
2. **RFC process (one big design doc per change)** — too heavy for the typical fleet change. Reserve for genuinely-new architectural patterns.
3. **Wiki / Notion** — splits the source of truth from the code; agents would need a separate fetch surface. Keeping decisions in-repo means they're rendered alongside code in every fleet-runner-injected `CLAUDE.md` chain.

## References

- [ADR overview by Michael Nygard](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [adr-tools](https://github.com/npryce/adr-tools) — optional CLI; not required.

## Index

| #     | Title | Status |
|-------|-------|--------|
| 0001  | Adopt Architecture Decision Records for the fleet | Accepted |
| 0002  | Build 20 fleet-primitive services (Tier S+A+B) | Accepted |
| 0003+ | One per primitive — see directory listing |  |
