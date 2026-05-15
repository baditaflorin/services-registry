# Resume prompt — for a fresh agent picking up the pentest fleet

When a Claude session gets long and needs to be reset, paste the block at
the bottom of this file as the **first** message of the new session. It
carries forward what's been built, what's deployed, what's blocked, and
exactly where to resume.

The prompt is self-contained: the new agent has none of the prior
conversation. It points back at `CLAUDE.md` and `RUNBOOK-UNATTENDED.md`
in this repo (which are the canonical brief), and at the per-repo READMEs
for service-specific detail.

**Operational topology / SSH targets / IPs are NOT in this file** — those
live in private `fleet-state/OPS.md`. The prompt below references them by
role only.

---

## The prompt — copy from here down

```
You're picking up a multi-day session building a baditaflorin pentest fleet.
Your job right now: drive the end-to-end bug bounty flow against a real
target via the dashboard and find something submittable. We have ~24
fleet services involved, the chain works on paper, none of it has been
validated by a real bounty submission yet. That's the goal.

## Where everything lives

Local workspace root: /Users/live/Documents/Codex/2026-05-08/
~250 sibling repos under baditaflorin/* on GitHub. Canonical catalog:
  https://raw.githubusercontent.com/baditaflorin/services-registry/main/services.json
Operational playbook for unattended automation:
  https://github.com/baditaflorin/services-registry/blob/main/RUNBOOK-UNATTENDED.md
Fleet conventions + agent anti-patterns:
  https://github.com/baditaflorin/services-registry/blob/main/CLAUDE.md
Topology / SSH targets / IPs:
  private fleet-state/OPS.md (don't echo any of it back into public repos)
READ THE PUBLIC TWO BEFORE ASKING THE USER FOR ANYTHING.

## What's been built in recent sessions

11 new repos shipped to private GitHub + registered in services-registry:

  Pentest primitives (ports 18129-18133):
    go-pentest-screenshot          PNG + DOM hash via chromedp
    go-pentest-http-replay         curl-PoC capture, safehttp-backed
    go-pentest-asset-inventory     program-keyed asset SoT, /diff endpoint
    go-pentest-dedup-fingerprint   semantic finding dedup
    go-pentest-job-queue           durable SQLite work queue, lease+reaper

  Pentest applications (ports 18134-18137):
    go-pentest-continuous-monitor  daily diff -> new-asset scans
    go-pentest-finding-triage      8 rules, drops/promotes findings
    go-pentest-submit-bot          H1/BC/Intigriti/email adapters, dry-run default
    go-pentest-exploit-verifier    7 verifier classes (SSRF/XSS/IDOR/JWT/etc.)

  Master orchestrator (port 18138):
    go-pentest-orchestrator        state-machine ticker, drives everything else

  Walkthrough surfaces:
    go-pentest-cli                 terminal CLI binary (single-domain)
    go-pentest-walkthrough  18139  HTTP API equivalent of the CLI
    go-pentest-dashboard           STATIC, GitHub Pages, the human GUI

  Fleet infrastructure (ports 18140-18142):
    go-fleet-secrets    NaCl-secretbox vault for tokens
    go-fleet-dns-sync   Hetzner Cloud DNS reconciler (api.hetzner.cloud/v1)
    go-fleet-preflight  pre-deploy checklist runner

  Plus patches landed in fleet-shared code:
    go-common@v0.14.1   safehttp: TLS 1.2 fallback on TLS alert 80
    go-common@v0.14.2   safehttp: SAFEHTTP_ALLOW_PRIVATE_IPS env allowlist
    go_cors_scanner / go_tech_stack / go_sourcemap_finder / go_bucket_finder
                        ?url= is now canonical (?target= kept as alias),
                        deployed at v1.3.1
    go-pentest-httpx@v0.1.2   v0.14.2 + extra_hosts for badita.org vhosts
    fleet-wide CORS at the nginx gateway via snippets/cors.conf
    14 fleet DNS A-records created via go-fleet-dns-sync against
                          api.hetzner.cloud (zone id documented in OPS.md)

## The dashboard

Live at: https://baditaflorin.github.io/go-pentest-dashboard/
api_key field defaults to default_token (rate-limited public demo key).
Tabs: Preflight | Programs | Recon | Scan | Findings | Report
Header shows build SHA + age (from GitHub API) so you can confirm freshness.
Recon shows every probed host with status + title + tech, or the error if dead.

## What is NOT done

1. The new orchestrator/walkthrough/secrets/dns-sync/preflight containers
   are pushed to GHCR but NOT yet deployed to the dockerhost. They have DNS
   but no running container.
2. No actual bug-bounty submission has been made. The full chain has been
   exercised against anthropic.com in PASSIVE recon only (no exploitation,
   no fuzzing, no auth probing).
3. No fleet-wide dnsmasq for the NAT hairpin issue (scoped fix to httpx only).

## Known fleet-side bugs / gotchas you'll hit

- fleet-runner bump-version only updates main.go const Version, NOT meta.go
  or version.go. Some services (go_tech_stack) report a stale version even
  after deploy. Cosmetic only.
- fleet-runner deploy is idempotent and will SKIP rebuild if a healthy
  container is running, even when the registered version differs from the
  image tag. Workaround: do a force-rebuild on the build LXC with
  `docker buildx build --no-cache --push -t :TAG -t :latest .`, then update
  the dockerhost compose pin manually. See OPS.md for exact paths.
- Dockerhost and webgateway share a public IP. Scanners hitting fleet-hosted
  public hostnames trip a NAT hairpin (TLS alert 80). The
  SAFEHTTP_ALLOW_PRIVATE_IPS + extra_hosts pattern fixes this per-scanner;
  a dnsmasq would fix it fleet-wide.
- jsbundle-secrets is often 502 — fleet ops issue, the container is
  unhealthy. Not a dashboard bug.
- crt.sh (used by cert-transparency) is flaky; expect 404s for some domains.

## How to drive things

- fleet-runner from your Mac: `/Users/live/bin/fleet-runner` (shim that
  forwards to the build LXC). Full help on `--help`.
- Service-to-service auth: each fleet service requires an api_key. The demo
  default_token works against the public gateway (rate-limited). For real
  scans use a keystore-issued key.
- Hetzner Cloud API token for DNS: lives in user env as HCLOUD_TOKEN
  (canonical name). Older HETZNER_DNS_API_TOKEN is for the DEPRECATED DNS
  Console API — DO NOT use that, dns.hetzner.com is deprecated. Use
  api.hetzner.cloud/v1 with Bearer auth.

## What to do next, in priority order (USER's STATED GOAL: find a real
   submittable bug so they can pay for next month's Claude Max)

1. Pick a real bug-bounty program with broad scope where the existing
   passive scanners have real signal. Candidates (verified in-scope via
   go-pentest-bounty-scope-checker):
     h1-shopify         max $50k    *.shopify.com + *.myshopify.com
     h1-gitlab          max $35k    *.gitlab.com + *.gitlab.io
     h1-paypal          max $30k    paypal.com infra
     vdp-google         max $31k    *.google.com (VRP)
     h1-anthropic       max $25k    *.anthropic.com + *.claude.ai
   Don't waste cycles on Uber (9k+ subdomains, picked over).

2. Run a TAKEOVER SCAN across the picked program's subdomains. This is the
   single highest-EV passive pattern: one stale CNAME = $500-$5k bounty.
   Pattern that's already tested:
     GET https://go-pentest-subfinder.0exec.com/enum?domain=X&api_key=...
     for each host: GET https://go-pentest-takeover-checker.0exec.com/check?host=H&api_key=...
     filter where severity != "none"
   Don't try to do this from `bash | xargs -P` again — those scans kept
   getting stuck. Do it inline, sequential, with progress printed every 25
   hosts, append-each-line to a file, take ~5 min. The earlier session tried
   parallelism three times and the bash plumbing kept failing; the issue
   isn't the API, it's the parallelism. Stick with single-threaded
   foreground loop.

3. Run github-dorks via the gh CLI (NOT the deployed go-pentest-github-dorks
   service — that one's GITHUB_TOKEN env isn't set, so it returns 401).
   The user has gh authed; use `gh api search/code -f q='...'` for queries
   like:
     "AKIA" /AKIA[0-9A-Z]{16}/ org:Shopify
     filename:.env DB_PASSWORD org:GitLab
     "shopify_api_key" path:.env path:config
   Live-test the high-value hits (verified secrets pay $1k-$10k).

4. If anything hits in #2 or #3, immediately:
   a. POST the finding to https://go-pentest-findings-store.0exec.com/findings
   b. POST it to https://go-pentest-finding-triage.0exec.com/triage
      (look for decision == submit_ready)
   c. POST to https://go-pentest-report-templater.0exec.com/render
      to get the markdown
   d. Show the user the markdown for review BEFORE any submission.
      Submit-bot is NOT yet deployed; for the first real submission,
      generate the report and let the user paste it manually into
      HackerOne/Bugcrowd.

5. After validating a finding -> submission path works, optionally deploy
   go-pentest-orchestrator + go-pentest-walkthrough so the user can drive
   future scans through the dashboard's API instead of curl scripts.

## Hard rules

- DON'T build more new services unless strictly blocking. The user has
  said: "currently, instead of building more tools, we just want to have
  fucking this working." Prioritize SHIPPING a real finding over architecture.
- DON'T run active probes against bug-bounty targets without explicit
  user confirmation. Passive recon (subfinder, ct, httpx, wayback, takeover-
  checker, github-dorks) is fine; nuclei/dalfox/exploit-verifier against
  any specific target requires per-engagement go-ahead.
- For DNS in this fleet, use api.hetzner.cloud/v1 (NOT dns.hetzner.com).
- For secrets, use go-fleet-secrets via the SAFEHTTP_ALLOW_PRIVATE_IPS
  pattern. Don't add tokens to env files or service repos.
- Mac disk space is tight; clean /tmp between long scans.
- The user's email is baditaflorin@gmail.com (works at Anthropic).

## Open questions waiting on the user

- Whether to install dnsmasq on the dockerhost for fleet-wide hairpin
  resolution (vs per-scanner extra_hosts). Currently scoped to httpx for
  4 badita.org vhosts.
- Which bounty program to focus the real hunt on.

Start by:
1. Confirming the dashboard SHA matches the latest go-pentest-dashboard
   main commit (proves the user is seeing latest).
2. Asking the user which target program to hunt on (or pick Shopify and
   show the plan).
3. Running the takeover sweep, sequential, no xargs, progress every 25 hosts.

Don't introduce new services. Don't refactor anything. Find a bug.
```
