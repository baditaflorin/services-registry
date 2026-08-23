# `bin/oo` scenario test plan

Twenty realistic operational scenarios an on-call engineer or an AI agent
would actually hit while running this fleet, used to stress-test `bin/oo`
against real needs rather than synthetic bugs. Each scenario states the
situation, the exact `oo` invocation(s) to try, and what counts as a PASS.

This is a living document — when a scenario reveals a real gap, either fix
`bin/oo` and update the scenario, or leave the gap noted here so the next
person doesn't have to rediscover it. Rerun this whole file (by hand or via
an agent) whenever `bin/oo` gains new commands, to confirm nothing regressed.

Results of the first run (2026-08-24) are inline as `> Result:` blocks.

---

1. **"Is this specific error real or noise right now?"** — a container
   name is reported as erroring; confirm before waking anyone up.
   `oo check <container>` (exit code 0/1/2) then `oo errors <container>`
   for the actual lines if FAIL.
   PASS = correct exit code + human-readable line detail on demand.

   > Result: PASS — `oo check domainscope-api` → `FAIL: domainscope-api
   > logged 5 error-looking line(s) in the last 15m` (exit 1); `oo errors
   > domainscope-api --limit 5` showed the real lines (Postgres query
   > cancellations: `"error":"pq: canceling statement due to user
   > request"`). Correct exit code, real human-readable detail on demand.

2. **"We just redeployed — did errors start right after?"** — classic
   the-incident-that-started-this-whole-tool scenario.
   `oo since-redeploy <container>`
   PASS = shows the deploy-relative window without the caller doing
   manual timestamp math.

   > Result: PASS — `oo since-redeploy domainscope-api --host
   > debian13-docker-prod` found first_seen at 2026-08-23T20:56:32Z (~2h
   > before "now") and printed the before/after window around it as
   > human timestamps. Independently cross-verified against `oo versions`
   > (below), which confirmed a real image change at that same moment —
   > the window genuinely was the redeploy, no manual math needed.

3. **"Is a failure in service A correlated with service B around this
   incident?"** — e.g. api container erroring, is its proxy also unhappy
   at the same moment.
   `oo compare <containerA> <containerB> --since <window>`
   PASS = interleaved, timestamp-ordered, both tagged so causality is
   readable at a glance.

   > Result: PASS — `oo compare domainscope-api go-infrastructure-fetch-cache
   > --since 2m --limit 500` interleaved both containers
   > (115 domainscope-api + 44 fetch-cache lines) in ascending timestamp
   > order, each tagged `[host] <name>`. Note: at the default --limit 100
   > with an imbalanced-volume pair, the ASC-ordered LIMIT can be
   > consumed entirely by the noisier container before the quieter one
   > appears — bump --limit or narrow --since for lopsided pairs.

4. **"This container keeps restarting — how often, and since when?"**
   `oo restarts <container> --since 24h`
   PASS = gap list with human-readable timestamps, not raw epoch math.

   > Result: PARTIAL — output format is fine (human timestamps, clear
   > gap lines) when it finds something. But live testing found a real
   > miss: `oo restarts domainscope-api --since 24h` reported "no gaps
   > > 30s found ... looks continuously up", even though `oo versions`
   > and `oo since-redeploy` both independently confirmed the container
   > WAS redeployed ~2h earlier (fresh image, first log line at
   > 20:56:32Z, nothing before that in the 24h window). Root cause:
   > restarts only checks gaps BETWEEN consecutive returned log lines —
   > it never compares the earliest returned line against the start of
   > the --since window, so "the container did not exist/log for the
   > first N hours of the window" (the actual redeploy signal) is
   > invisible. Not fixed: correctly interpreting that boundary gap
   > (redeploy vs. "container is just younger than --since" vs. "log
   > retention doesn't go back that far") is a judgment call, not a
   > small deterministic addition. Separately fixed a related but
   > distinct issue live: `restarts` silently truncates via its
   > ORDER BY ASC LIMIT for noisy containers within the window, which
   > could produce a false "continuously up" from only PARTIAL data —
   > now warns on stderr when the row cap is hit (see bin/oo diff).

5. **"What version is actually running vs. what we think we deployed?"**
   `oo versions <container> --since 24h`
   PASS = shows version-string transitions if the container logs one.

   > Result: PASS — `oo versions domainscope-api --since 24h` showed a
   > clean transition: `1.96.0` (20:56:32–21:11:52) →
   > `1.96.0-gdprevidence` (21:11:52–now), each with first/last-seen and
   > line counts. Exactly the deploy-history view promised, no git/GHCR
   > lookup needed.

6. **"Fleet-wide: what's new / different in the last hour?"** — coarse
   triage entry point with no specific container in mind yet.
   `oo new` (default `--since 1h --baseline 24h`)
   PASS = surfaces containers active now that weren't in the baseline
   window, without the caller supplying container names up front.

   > Result: PARTIAL — technically correct (34 container names in the
   > last 1h not seen in the prior 24h baseline, zero names supplied by
   > the caller), but in practice the output was ~97% Docker
   > auto-generated ephemeral names (`adoring_jemison`,
   > `pensive_dirac`, ...) from short-lived pentest-tool run containers,
   > which churn constantly by design and drown out the one signal
   > that'd actually matter (a real newly-deployed persistent service).
   > Not fixed: filtering "looks like Docker's random name generator"
   > is a heuristic judgment call (some operators may WANT to see
   > those, e.g. hunting a port squatter), not a small deterministic
   > addition.

7. **"Scriptable health gate"** — something (a cron job, another agent)
   needs a machine-checkable yes/no, not prose.
   `oo check <container>; echo $?`
   PASS = 0 healthy / 1 FAIL / 2 UNKNOWN, and `2` is distinguishable from
   `1` so a caller doesn't treat "couldn't check" as "healthy" OR as "on
   fire" — both are wrong defaults for different reasons.

   > Result: PASS — all three exit codes verified live: `oo check
   > go-fleet-call-tracer` → `OK` (exit 0); `oo check domainscope-api`
   > with real errors present → `FAIL: ... 5 error-looking line(s)`
   > (exit 1); bad creds via a scratch env → `UNKNOWN: ... check failed
   > to run` (exit 2). One line of output each, no prose. (The UNKNOWN
   > message quality was cryptic before a fix applied during this run —
   > see scenario 20.)

8. **"A user reports a specific error string — find every occurrence."**
   `oo grep <container> "<exact phrase, incl. punctuation/apostrophes>"`
   PASS = correct matches even when the phrase has SQL-special
   characters (apostrophes) in it.

   > Result: PASS — `oo grep "can't evaluate field Assets" --container
   > go-fleet-grafana --since 6h` returned the correct real matches (a
   > Grafana template-rendering error) with the apostrophe intact and
   > unescaped-looking in the output; `sql_escape` correctly handled it
   > server-side without breaking the query or silently returning empty.

9. **"Is this container's traffic dropping or spiking?"**
   `oo rate <container> --since 6h --bucket 30m`
   PASS = a bucketed histogram, not just a total count.

   > Result: GAP found and FIXED — `oo rate go-html-proxy --since 6h
   > --bucket 30m` at the default --limit 10000 rendered only 2 buckets
   > (20:30, 21:00) with nothing after, looking exactly like traffic
   > collapsed to zero. Root cause: `ORDER BY _timestamp ASC LIMIT
   > 10000` silently stops at the row cap, not at "now", for any
   > container noisier than --limit rows within --since. Re-running
   > with --limit 100000 revealed the truth: traffic had actually
   > INCREASED for the next 90 minutes before tapering. This is a
   > dangerous silent-wrong-answer shape for exactly the question this
   > scenario asks. Fixed live in bin/oo: `cmd_rate` now warns on
   > stderr ("hit --limit (N) rows before reaching the end of --since
   > ...") whenever the row cap is hit, verified against the same
   > go-html-proxy query post-fix.

10. **"Fleet-wide: rank what's erroring the most right now."**
    `oo summary`
    PASS = composes hosts/containers/top-error-containers into one view
    without three separate manual queries.

    > Result: PASS — `oo summary --since 1h` composed hosts (2, with
    > line/container counts), top 8 noisiest containers, and top 8
    > error-containers into one output from a single invocation. Real
    > data surfaced correctly (e.g. ollama-wrapper-service's
    > node-exporter sidecar as the top error-count container).

11. **"A bug report says 'around 14:32 UTC' — show me that container's
    logs bracketing that moment."**
    `oo context <container> --around "<timestamp>"` or the
    `since-redeploy`-style `--before`/`--after` window
    PASS = returns a tight window, not the whole day.

    > Result: PASS — `oo context domainscope-api "2026-08-23T23:04:18Z"
    > --before 30s --after 30s --host debian13-docker-prod` returned
    > exactly the ~60s bracket around the given ISO8601 timestamp, not
    > the whole day. (Actual flag is `--before`/`--after` positionally
    > after `<container> <timestamp>`, not a `--around` flag — the
    > scenario's shorthand doesn't match the real syntax verbatim, but
    > the documented usage in the header covers it and the behavior is
    > correct.)

12. **"What containers are even emitting logs on this host?"** — sanity
    check before assuming a container is queryable at all.
    `oo containers --host <host>`
    PASS = accurate live list, empty (not error) if the host ships no
    `docker_logs`.

    > Result: PASS — `oo containers --host debian13-docker-prod --since
    > 1h` returned an accurate host-scoped list (domainscope-api,
    > domainscope-staging-api, domainscope-frontend, plus ephemeral
    > pentest-tool containers). `oo containers --host
    > nonexistent-host-xyz` returned empty output with exit 0, not an
    > error.

13. **"Is the log pipeline itself alive?"** — meta health check of the
    Vector shippers, not of any one service.
    `oo hosts`
    PASS = lists exactly the hosts actually running vector-log-shipper
    (2 as of 2026-08-23: the dockerhost VM + prod docker host).

    > Result: PASS — `oo hosts --since 24h` listed exactly 2 hosts
    > (`ubuntuvm1`, `debian13-docker-prod`) with line counts and
    > distinct-container counts, matching the expected shipper count.

14. **"I need to watch this container live while reproducing a bug."**
    `oo tail <container>` (run briefly, Ctrl-C / kill)
    PASS = streams promptly, resumes cleanly on transient errors (per
    pass-2's fix), leaves no orphaned temp file or process on exit.

    > Result: PASS — `oo tail go-html-proxy --interval 3`, run for ~9s
    > under `timeout`, streamed real lines promptly on the first poll
    > cycle. After exit: no leftover `oo_tail_cursor.*` temp file in
    > `$TMPDIR`, no orphaned `oo tail`/related process in `ps aux`.

15. **"None of the canned commands answer this — I need custom SQL."** —
    e.g. "how many distinct hosts logged a rate-limit message today."
    `oo query "SELECT COUNT(DISTINCT host) ..." --since 24h`
    PASS = correct results, and a deliberately malformed query fails
    with a clean `_oo_error`, not a traceback or false empty result.

    > Result: PASS — `oo query "SELECT COUNT(DISTINCT host) as n FROM
    > docker_logs WHERE message ILIKE '%rate limit%' OR ... " --since
    > 24h` returned `{"hits":[{"n":2}],...}` correctly. A deliberately
    > malformed query (`SELECT * FRUM docker_logs WHERE bad syntax
    > HERE`) failed cleanly: `error: sql parser error: Expected end of
    > statement, found: FRUM` on stderr, exit 1 — no traceback, no false
    > empty result.

16. **"I'll need this exact diagnostic query again next week."**
    `oo save <name> "<sql>"` → `oo saved` → `oo run <name>`
    PASS = round-trips correctly; survives concurrent saves (pass-3 fix)
    without corrupting the store.

    > Result: PASS — `oo save error-rate "SELECT container_name,
    > count(*) ..."` → `oo saved` listed it correctly → `oo run
    > error-rate --since 1h` executed it and returned real results.
    > Concurrency: fired 20 `oo save` calls in parallel (scratch
    > OO_SAVE_DIR, not the real `~/.oo`) — resulting queries.json stayed
    > valid JSON with all 21 keys (20 concurrent + 1 earlier), `oo
    > saved` listed all 21 with no corruption.

17. **"Hand this query to a human teammate who prefers the web UI."**
    `oo link "<sql>" --since <window>`
    PASS = produces a URL/params a human can paste to reproduce the
    exact same query in the OpenObserve UI.

    > Result: PASS (with a nice-to-have noted) — `oo link "SELECT * FROM
    > docker_logs WHERE container_name = 'go-html-proxy'" --since 2h`
    > printed the web UI base URL, the exact query text, and the
    > resolved microsecond-epoch time range as copy-paste text. It is
    > three things to paste into three different UI fields rather than
    > one clickable deep-link (would need OpenObserve's exact URL query
    > param scheme to go further) — meets the stated bar but a true
    > one-paste link would be a nicer version of this.

18. **"Before writing raw SQL, what streams/fields even exist?"**
    `oo streams` then `oo stream-info docker_logs`
    PASS = accurate schema so a hand-written `oo query` doesn't have to
    guess field names.

    > Result: GAP found and FIXED — `oo streams` correctly listed the 3
    > streams (default, default1, docker_logs) with doc counts/time
    > ranges. But `oo stream-info docker_logs` returned `"total_fields":
    > 0` and empty `index_fields`/`full_text_search_keys` arrays — it
    > hit the same list endpoint `oo streams` uses, which does not
    > report per-field schema for a dynamic-schema stream. It never
    > actually answered "what fields exist." Confirmed OpenObserve has a
    > dedicated `/api/{org}/streams/{name}/schema` endpoint that returns
    > the real field list (43 fields for docker_logs, e.g.
    > `container_name: Utf8`, `host: Utf8`, `_timestamp: Int64`). Fixed
    > live in bin/oo: `cmd_stream_info` now calls that endpoint directly
    > and prints the real schema; added a strict `[A-Za-z0-9_]+`
    > allowlist on the stream-name argument first since it now goes into
    > a URL path embedded in the ssh command string (no argv-passing
    > indirection available there, unlike everywhere else in the file) —
    > verified an injection-attempt name is rejected before ever
    > reaching ssh. Re-verified `oo stream-info docker_logs` returns the
    > full field list, and `oo stream-info totally_fake_stream_xyz`
    > fails cleanly (exit 1, "stream not found").

19. **"Same container name exists on two different hosts — which one am
    I actually looking at?"**
    `oo logs <container> --host <host>` vs. without `--host`
    PASS = `--host` actually disambiguates; every per-container command
    supports it consistently (pass-3 closed the one gap, `compare`).

    > Result: PASS — found `domainscope-staging-api` genuinely exists on
    > both hosts. `oo logs domainscope-staging-api --since 10m --limit
    > 200` without `--host` returned lines tagged from both
    > `debian13-docker-prod` (134) and `ubuntuvm1` (66); with `--host
    > ubuntuvm1` only ubuntuvm1 lines came back, with `--host
    > debian13-docker-prod` only that host's lines came back. `--host`
    > genuinely disambiguates.

20. **"Garbage input from a human or another agent"** — typo'd container
    name, malformed `--since`, missing required arg, wrong password.
    Run several: `oo logs nonexistent-container-xyz`,
    `oo logs <container> --since 5xyz`, `oo grep <container>` (no
    pattern), bad creds via a scratch env.
    PASS = every case fails (or empty-results) cleanly with a one-line
    human-readable message and correct exit code — never a raw Python
    traceback, never a silent false "no results" for what was actually a
    malformed request.

    > Result: PARTIAL, with 2 real gaps found and FIXED, plus one
    > cosmetic issue left as-is:
    > - `oo logs nonexistent-container-xyz` → clean empty-results
    >   message on stderr, exit 0. PASS.
    > - `oo logs go-html-proxy --since 5xyz` → clean `error: invalid
    >   duration: 5xyz ...`, exit 1. PASS.
    > - `oo grep` (no args at all) → clean usage message, exit 1. PASS
    >   (see cosmetic note below).
    > - `oo grep go-html-proxy` (the classic footgun — passing a
    >   container name as if it were `oo grep <container>` with no
    >   pattern) → silently treated as pattern="go-html-proxy" and
    >   searched fleet-wide; returned an honest "no matches" empty
    >   result rather than erroring. Not a crash, but a real trap: the
    >   caller's actual mistake (wrong argument order) is invisible.
    > - Bad creds (scratch env, wrong password) → GAP found and FIXED:
    >   before, every command surfaced a bare Python JSON-decoder
    >   artifact (`error: Expecting value: line 1 column 1 (char 0)`)
    >   for BOTH a bad-creds response and a totally unrelated network
    >   failure, hiding OpenObserve's actual "Unauthorized Access" body.
    >   Fixed in `oo_search_range`'s error handling to surface the real
    >   response text; re-verified: `error: non-JSON response from
    >   OpenObserve: Unauthorized Access`.
    > - Unreachable/wrong `OPENOBSERVE_HOST` → GAP found and FIXED: no
    >   curl invocation in the file had `--connect-timeout`/`--max-time`,
    >   so this hung for **132 seconds** with zero feedback before curl's
    >   own OS-level timeout finally fired. Added `--connect-timeout 10
    >   --max-time 60` to all curl calls; re-verified the same failure
    >   now surfaces in ~11s with a clear message ("empty response
    >   (connection/timeout failure...)").
    > - Missing required positional arg elsewhere (`oo compare
    >   go-html-proxy` with only one container, `oo since-redeploy` with
    >   none) → fails with exit 1 and the correct usage text, but every
    >   one of these leaks bash's internal parameter-expansion prefix
    >   (`./bin/oo: line 631: 1: usage: ...`) ahead of the actual
    >   message. Not a traceback and still one line ending in readable
    >   usage text, so it clears the PASS bar, but it's cosmetically
    >   noisy — left as-is since fixing it means converting every
    >   `${1:?msg}` across ~15 command functions to an explicit check,
    >   which is a broad refactor, not a small fix.
    > - `oo bogus-command` → clean `unknown command: bogus-command`
    >   followed by full usage, exit 1. PASS, and notably cleaner than
    >   the missing-positional-arg case above.

---

## How to (re)run this

Fetch real credentials from private `fleet-state/OPS.md` ("OpenObserve
root credentials"), export `OPENOBSERVE_USER`/`OPENOBSERVE_PASSWORD`/
`OPENOBSERVE_HOST`, and work through the list against live production
data (read-only — never write/delete via these queries). Use real
container/host names from `oo containers`/`oo hosts` rather than
inventing them. Record PASS/PARTIAL/GAP per scenario with the actual
command run.
