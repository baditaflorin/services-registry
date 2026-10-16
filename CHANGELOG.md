## 2026-10-16

- `fleet-graph` version 0.4.19 -> 0.4.23: the hand-pinned `version` in `overrides.json` had not followed the collector (live 0.4.21 since 2026-10-14, now 0.4.23 after the `/brief` fix #48 and the deploy unblock #49), so every `fleet-runner deploy go-fleet-graph` printed `preflight/version: live=0.4.21 registry=0.4.19`. `services.json` regenerated (one field); no other slice changes.
- `node-proxy` gets a `version` (0.1.1, the only tag of `baditaflorin/node-proxy`): the entry had none, so `fleet-runner deploy-pool` refused to place it on a non-Coolify host ("version missing in registry entry"), which kept node-proxy off lv3 even after #791 widened the allowlist. `overrides.json` gets the one hand-pinned line, `services.json` the regenerated field; the other slices are unchanged.
- `apikey-service` version 0.4.7 -> 0.7.5: the hand-pinned `version` in `overrides.json` had not moved since #303 (2026-10-01) while go_apikey_service went to 0.7.5 (its service.yaml, CHANGELOG and the running container `go-apikey-service`, built from a6bad2e). The stale pin made go-fleet-preflight report `live=0.7.5 registry=0.4.7` and look for a 0.4.7 image. Regenerated with `bin/generate.py --only apikey-service`; services.json changes only that value.
- `keystore-mesh` repo_url back to `https://github.com/baditaflorin/go_apikey_service` (#790): reverts the #781/#782 workaround now that fleet-runner v0.7.569 (go_fleet_runner#848, installed on the builder) prefers an exact id and skips runtime external entries in every repository-to-entry lookup.
- Widen the host placement allowlists so the capacity balancer has real choices on the new servers (the 2026-10-16 resize gave them 8-12 vCPU and 8-16 GB): `0mcp-runtime-general` (lv3) may now also run js-proxy, js-proxy-network, node-proxy, proxy-fingerprint and css-framework-detector; `0database-runtime` js-proxy, js-proxy-network, node-proxy and proxy-fingerprint; `0own-runtime` js-proxy-network, node-proxy and proxy-fingerprint (not js-proxy: a Chromium pool on that host's I/O-bound ZFS and 8 GB). Still `restricted` hosts with explicit ids, nothing else changes; the pools gain the members through `fleet-pool-onboard --add-member`, one service at a time.
- Raise `MAX_FETCH_INFLIGHT` for `infrastructure-fetch-cache` from 128 to 384 (the compose default is 256). Since about 12:55Z the enrichment producers doubled their load on the cache (70 -> 112 requests/s); with fetches holding a slot for 2-30 s the fetch gate sat at 128 in flight and shed about 40% of the requests with 503 (`loadshed_shed_total{gate="fetch"}` 43,881 against 64,944 admitted), which the producers turn into direct fetches: `FetchCacheFallbackStorm` has been firing since 13:06Z (fallback rate 0.2/s before, 25-30/s now). The shed requests are fetched directly by the producers anyway, so total origin concurrency does not grow, only the deduplication returns. Little's law on the admitted traffic (54/s at 128 in flight, about 2.4 s each) puts the need near 215 average for the full ~90/s demand; 384 leaves headroom. Revert if the cache's CPU or memory or the Webshare error rate climbs.
- Record the live value `MAX_COMMON_CRAWL_INFLIGHT=8` for `infrastructure-fetch-cache` in `overrides.json` (it said 4 since #721, but the running override on the dockerhost has carried 8 since the 2026-10-15 14:10Z hand edit, backups `docker-compose.override.yml.bak-commoncrawl-limit-*`), so the next `render-compose` does not silently lower it. No behaviour change: the container already runs with 8.
- Point `infrastructure-fetch-cache` at the gateway's internal js-proxy listener: per-slug env `JS_PROXY_URL=http://10.10.10.10:28006` (the load-balanced pool, members and weights shared with the public vhost; go_fleet_runner `internal_listener`, #850) and `JS_PROXY_FALLBACK_URL=http://go-js-proxy:8080` (the local container, used only when the listener cannot be connected to; fetch cache 0.3.46). `JS_PROXY_NETWORK_URL` and `HTML_PROXY_URL` are unchanged. Applied with `render-compose --filter infrastructure-fetch-cache`.
- Declare per-slug `mem_limit: 256m` for cognitive-load, domain-ad-network, domain-analytics-stack, domain-events-platform, domain-interactive-demo, domain-podcast-presence, gdpr-compliance and pentest-audit-log, matching the caps applied live on the 0docker dockerhost on 2026-10-16 (rule: max(4x current, 2x 7-day peak, 256 MiB)). These eight have no `docker-compose.override.yml` in their compose directory, so the registry cap reaches them only once their overlay is pushed (`render-compose <slug> --push`); a plain `fleet-runner deploy` does not write it.
- Fix the go-fleet-metrics-hub overlay render. `fleet-alertmanager` and `fleet-grafana-staging` had no `compose_service_name`, so both rendered as an `app:` block in `/opt/services/go-fleet-metrics-hub/docker-compose.override.yml`: a duplicate YAML key that Compose rejects, and no cap reached Alertmanager. Alertmanager now targets its `alertmanager` service with `compose_skip_networks` (it runs with `network_mode: host`, like Prometheus) and `mem_limit: 256m`. Staging Grafana now renders into its own Compose project directory and `grafana-staging` service with `compose_self_managed`, because that project is started with `-f docker-compose.staging.yml`, which does not auto-load an override; its cap belongs in that file.
- Refresh `services.summary.txt`, which had not been regenerated since the 2026-10-16 keystore-mesh port claims.

## 2026-10-15

- Enable Fleet Graph env projections and sync source versions for comment-extractor, cookie-checker, cors-scanner, cors-tester, and crlf-tester while preserving their Coolify app bindings.
- `bin/fleet-runner-shim`: token values no longer travel on the remote command line (visible in `ps` on the bastion and on LXC 108). Non-secret names stay there. A token set locally is sent only when `fleet-runner env-check --json` says the builder lacks it (or `FLEET_ENV_FORWARD_ALWAYS=1`), and then on stdin to `fleet-runner --env-from-stdin` in non-interactive calls; interactive calls send nothing secret and print one note. A builder too old to know `env-check` is sent no secret (one warning); `FLEET_ALLOW_ARGV_SECRETS=1` is the explicit, noisy escape hatch back to argv. Adds `bin/test_fleet_runner_shim.py` (stub ssh, TTY and non-TTY, skew matrix) and a frozen copy of the previous shim in `bin/testdata/` for the old-shim case. Needs go_fleet_runner with `env-check` / `--env-from-stdin` installed on the builder to take the stdin path; see go_fleet_runner `docs/ENV-FORWARDING.md`.
- Sync ASN Lookup 1.5.18 with the Coolify-compatible Pyroscope secret-reference release.
- Sync search-duck-go 0.4.19 to generated registry metadata after the merged Pyroscope canary release.
- Sync Search Duck 0.1.13 to the registry for the merged Pyroscope canary release.

- Keep the Scrape The World Hub on its existing project network when rendering the Graph writer mount; joining the shared `pentest_network` is outside this rollout.
- Pin the Scrape The World Hub fleet overlay to its production `hub-dashboard` Compose service and sync registry version 0.15.1, so Graph key mounts target the live service instead of creating an invalid `app` block.
- Sync merged Pyroscope-enabled releases domain-product-count 0.3.5 and domain-third-party-fetch-map 0.6.6, with service-scoped receiver settings for the next central rollout wave.

## 2026-10-15

- Enable service-scoped Pyroscope ingestion for five merged domain detector releases using each service manifest’s file-mounted Vault credentials and a 30-second upload interval.

- Sync five merged Pyroscope-enabled Go service versions to origin/main before the next central fleet-runner rollout wave.

- Sync Fleet Pixel 0.4.2 and Pentest Attack Chainer 0.3.4 registry versions to their origin/main source manifests for the Pyroscope rollout.
- Add `bin/generate.py --only <id>` to update one generated registry row and projections without applying unrelated topic-catalog drift.
- Align License Extractor 0.1.9 and declare its Vault-backed fetch-cache file mount after the canary found the Compose omission.
- Sync five DomainScope detector registry versions to merged `origin/main` source releases before staging the file-backed Fleet Graph writer rollout: ad-network 0.4.18, analytics-stack 0.8.1, events-platform 0.2.34, interactive-demo 0.2.19, and podcast-presence 0.3.23.

## 2026-10-14
- Enable the canonical Fleet Graph Compose overlay (GRAPH_ENABLED and the collector URL) for the twelve pentest infrastructure services Agent State, Asset Inventory, Attack Chainer, Audit Log, Continuous Monitor, HTTP Replay, Job Queue, Notify, Orchestrator, Payoff Tracker, Rate Coordinator and Walkthrough, so their reviewed hot-reload key mounts can be rendered.
- Enable the canonical Fleet Graph Compose overlay (GRAPH_ENABLED and the collector URL) for Fleet Token Ledger, Fleet Backup, Fleet Host Guard, Composite Runner, JS Proxy, JS Proxy Network and Random Proxy, so their reviewed hot-reload key mounts can be rendered.
- Enable Pyroscope delivery for Fleet Q, Fleet Sandbox Targets, Fleet Tail, and License Extractor; service PRs #19, #18, #18, and #31; scoped secret names only; Go Common v0.102.53.
- Enable the canonical Fleet Graph Compose overlay (GRAPH_ENABLED and the collector URL) for the eleven fleet-infra services Fleet Backoff Coordinator, Engagement Timeline, Fingerprint Cache, Persona, Pixel, Q, Sandbox Targets, Tail, Target Reputation, Vendor Disclosure Tracker and Webhook, so their reviewed hot-reload key mounts can be rendered.
- Enable service-scoped Pyroscope profile configuration for Fleet Tech Inferrer and Fleet Webhook Verifier after their source releases were merged.
- Align admin-finder 1.7.8 registry version and remove Coolify secret references from public environment overrides; the source manifest resolves them through validated coolify_secret_refs.
- Verify Pyroscope profile transport for admin-finder in digest-pinned staging, then sync 1.7.7 after fixing Coolify/Compose environment and version-label propagation.
- Enable the canonical Fleet Graph Compose overlay for Fleet Markdown, Fleet Read, and Pentest Target Normalizer, including the reviewed read-only hot-reload key mounts.
- Sync Firmographic Profile 1.4.1, HTTP Protocol Checker 2.4.13, Heading Outline Extractor 0.3.12, and Menu Extractor 0.1.9 to both origin/main service.yaml and live /version; their runtime images differ from origin/main only by documentation-only commits, so no rebuild or restart is needed.
- Keep Git Provider API Broker's published port bound to the Dockerhost's private gateway-upstream address through a public-registry boolean opt-in; fleet-runner resolves the actual address from private cluster configuration.
- Pin the Git Provider API Broker Compose overlay to its actual `git-provider-api-broker` service; the default `app` name created an empty phantom service and blocked Fleet Graph key-mount activation.
- Sync the Search Duck and Git Provider API Broker override versions with services.json and the released source v0.1.12 tags.
- Sync Search Duck and Git Provider API Broker registry versions to source v0.1.12 after aligning their canonical Graph runtime declarations.
- Enable file-backed Fleet Graph for Search Duck 0.1.11 and Git Provider API Broker 0.1.11 after bounded source and live dependency review.
- Keep task-credential-broker on its proven API-key path while retaining Fleet Graph integration; disable its Compose SPIFFE socket/mTLS opt-in because both local SPIRE agents are currently failing node attestation and no production caller of the broker's private mTLS listener is verified.
- Pin `fleet-capabilities` to its live Compose directory `/opt/services/fleet-capabilities`; registry rendering and the writer manifest must target the Docker Compose working directory proven by the live container label.
- Enable file-backed Fleet Graph writer configuration for five healthy active Go services: Fleet Counter, Fleet Dig, Fleet Fake, Fleet Pipe, and Fleet Port.
- Enable Fleet Graph writer configuration for five active Go services: Voice Search, Fleet Pwgen, Fleet Resolver Quorum, Fleet IP, and Fleet Visualizer.
- fleet-backup `mem_limit` override 12g -> 6g (matches go-fleet-backup 0.2.2: streaming archives peak at 3.4 GiB of mostly reclaimable page cache, about 100 MiB process memory). The generated compose overlay reads this value, so the compose file alone could not lower it.
- Set simulate-ddos `example_path` to `/health`: its load-generating route only exists under a secret path token, the service's own smoke test deliberately targets `/health`, and the scaffolded `/?target=example.com` always answered 404, which made no-key probes of that service meaningless.
- Add `bin/audit_server_new_auth.py` (with unit tests): lists services that build their server with `server.New` but install no keystore auth option. `server.Run` adds `WithKeystoreAuth` itself; `server.New` does not, and a Coolify host has no nginx key check in front. First run on 368 api_key services: 332 ok, 1 flagged (biz-classifier, compose, covered by the nginx edge, answers 401), 7 not using server.New, 28 with no readable main.go (all compose). Usage: `bin/audit_server_new_auth.py --github --runtime coolify --fail-on coolify`.
- Put the five Fleet Graph rollout versions in canonical services.json so fleet-runner deploy preflight and version tags agree with the released source; regenerate derived slices.
- Enable Fleet Graph writer configuration for Merchant Profile 1.1.5, SVI 2.3.5, SEO Audit 1.1.2, Vendor Security Scorecard 1.1.2, and Fleet Schema Validator 0.1.4; sync their canonical registry versions.
- Enable Fleet Graph writer configuration and sync release versions for SCC 2.5.4, Seasonal Detector 2.7.2, Team Member Extractor 0.2.3, ToS Finder 1.9.2, and Substack Scraper 1.1.6.

## 2026-10-09
- Sync five Go services to their merged profiling-enabled versions: captcha-detector 1.2.12, citation-reference-extractor 0.1.15, comment-extractor 1.4.7, consent-simulator 0.1.7, and crawl-web-application 1.3.4.
- Retire four browser-local Whisper apps from GitHub Pages and the active catalog; preserve their source repositories.
- Retire Kokoro OpenVINO Adapter from the active service catalog; retain its source repository as a backup.

# Changelog

## 2026-10-15
- Sync Fleet Capabilities to merged release 0.1.8 after fixing Pyroscope auth-file environment wiring.
- Sync seven pentest Fleet Graph rollout targets to current origin/main source versions: HTTP Replay 0.3.5, Job Queue 0.2.4, Notify 0.1.2, Orchestrator 0.2.3, Payoff Tracker 0.2.3, Rate Coordinator 0.2.5, and Walkthrough 0.3.3. The registry update does not itself deploy these versions.

## 2026-10-14
- Enable file-backed Fleet Graph writer configuration for five active Go services: Fleet Capabilities 0.1.7, Fleet Tech Inferrer 0.1.3, Fleet PoC Curl 0.1.2, Fleet HAR Builder 0.1.5, and Fleet Webhook Verifier 0.2.2; sync catalog versions to their source manifests.

- Enable Fleet Graph writer configuration for five active Go services and sync their catalog versions to the verified deployed origin/main releases: Domain AI Catalog Discovery 0.3.1, Team Size 3.0.13, Wps 2.3.3, Xss Payload 1.3.2, and Xss Scanner 1.5.8.

## 2026-10-13
- Add the optional private `served_by` field (name of the other deployment that answers for an entry through a gateway upstream; schema, generator pass-through, not in the public mirror). `url-categorizer-api` sets it (`domainscope`): its VM100 copy was retired and its vhost proxies to the domainscope prod API, so no container is expected on the docker hosts. `fleet-selftest-poll` and `fleet-container-audit` skip the dockerhost check for such entries.
- Add `bin/fleet-selftest-poll.py` and its CT108 systemd timer: polls `/selftest` of all 384 container services every 30 minutes (compose services on the docker hosts' LAN, no credential needed; coolify services on their edge) and exports `fleet_selftest_*` metrics. `SelftestCheckFailing` had no data source before; go-fleet-metrics-hub now alerts on it.
- Double canonical SPIFFE workload opt-ins on the 0docker Compose cohort: 43 additional 0exec services and 48 additional 0crawl services; leave the open Contact Route Detector canary PR out of this batch.
- Sync Google Taxonomy, Redirect Tracer, and Robots Analyzer catalog versions to their merged 1.5.6, 1.7.1, and 1.4.5 releases.

## 2026-10-12
- Opt Contact Route Detector into 0crawl SPIFFE workload identity.
- Remove the stale `kokoro-ovms-adapter` override. The adapter left the active catalog on 2026-10-09 and was deleted from the dockerhost on 2026-10-12 (containers, compose project, model files); its source repository is retained.
- Opt geo-places into 0exec SPIFFE workload identity.
- Sync Funding Signal to deployed 0.1.35 after the runtime identity fix; direct `/health`, `/selftest`, and `/version` returned 200, and gateway health returned 200. The gateway `/selftest` probe remains auth-gated (401).
- Opt three additional low-traffic 0crawl analyzers into SPIFFE workload identity.
- Opt four independent 0crawl extractors into SPIFFE workload identity.
- Opt two low-traffic, gateway-authenticated 0exec Pentest APIs into SPIFFE workload identity.
- Defer SPIFFE enrollment for fleet-payload-corpus and fleet-persona until their missing runtime prerequisites are resolved.
- Opt three zero-observed 0exec fleet services into SPIFFE workload identity.
- Sync ten production-deployed 0crawl service versions to their merged `origin/main` releases after direct health, self-test, and version probes: Backlink Profile 0.3.10, Podcast Presence 0.3.22, Tracker Density 0.3.3, Embed/Iframe Classifier 0.2.8, Emotional Tone 3.4.0, Leadership Extractor 0.2.2, Pricing Tier Parser 0.1.14, Reading Time Estimator 0.1.10, Social Proof Extractor 0.1.10, and Team Member Extractor 0.2.2. The 50-domain replays are unlabeled coverage checks and do not establish precision/recall.
- Opt five zero-observed 0exec fleet services into SPIFFE workload identity.

- Opt four 0exec domain-signal APIs into SPIFFE workload identity for the next Compose canary wave: domain-open-source-presence, domain-saas-host-mapper, domain-well-known-catalog, and domain-tech-eol-flagger.

- Opt Domain Email Deliverability Score into 0exec SPIFFE workload identity as a single-service rollout canary.

- Opt Login Analyzer into 0crawl SPIFFE workload identity.

- Regenerate the catalog from canonical sources; sync Domain FAQ Schema to merged 0.2.23 and record the live Chromium replay while holding TRL at 7 pending a labeled held-out JS sample.

- Opt Cas into 0crawl SPIFFE workload identity.

- Opt Dependency Counter and Accessibility Audit into 0crawl SPIFFE workload identity.

- Opt Embed Iframe Classifier into 0crawl SPIFFE workload identity; live enrollment and SVID/health checks will follow merge.

- Opt six low-traffic 0crawl Go services into SPIFFE workload identity: page-load-metrics, pwa-manifest, promo-code-detector, quote-extractor, reading-time-estimator, and robots-analyzer.

- Opt eight low-traffic 0crawl Go services into SPIFFE workload identity: go-oauth-mapper, hiring-footprint, hsts-checker, iban-bic-validator, js-framework-version-extractor, jwt-debugger, license-extractor, and list-extractor.

- Opt five 0crawl Go services into SPIFFE workload identity: color-contrast-checker, consent-simulator, crawl-web-application, crux-field-vitals, and css-framework-detector.

- fleet-edge-probe: skip services whose registry `lifecycle.status` is `sunset` (reported as `skipped`, exposed via `fleet_edge_probe_service_skipped{lifecycle}` / `fleet_edge_probe_skipped_total`) and accept `{"ok": true}` and `{"status": "success"}` health bodies, removing the `FleetServiceEdgeDown` false positives for named-entity-recognizer, task-credential-broker and fleet-persona. 5xx, timeouts, connection errors and `ok:false` still fail.
- Declare DomainScope API's direct dependency on Fetch Cache and sync registry versions for the Common Crawl source-selector release.

## 2026-10-11

- Sync Task Credential Broker to merged release 0.1.17.

- Enable Fleet Graph for ten current-release Go services with explicit, hot-reloadable writer identities.

## 2026-10-10

- Allow the 0mcp host address to reach Fleet Secrets so its explicitly scoped services can load allowlisted runtime secrets.

- Sync Captcha Detector 1.2.13 and Citation Reference Extractor 0.1.16 after file-backed Fleet Graph writer integration merged.
- Sync the canonical catalog and overrides to Ad Density 2.6.17, Brand Logo Extractor 0.1.19, and Broken Links 1.6.13 after their file-backed Fleet Graph writer changes merged.
- Sync A11y Quick version 1.5.15 after Fleet Graph credential integration merged.

- Declare observed Fetch Cache dependencies for 36 DomainScope-facing services and regenerate the dependency graph projection.
- Preserve existing Fleet Secrets routing and Git Provider Broker dependency/version metadata during full registry generation.

## 2026-10-09

- Approve Fleet Secrets' private host:port route and declare the API broker dependency for its Vault key read.
- Sync Company Name Extractor version 0.1.33 after its Vault bootstrap fix merged.
- Set the archive-only Common Crawl source for the breadcrumb, AMP, and image-alt pilot extractors; leave partial company-name and font-stack pilots on live fetches.
- Synchronize registry versions for the five Common Crawl extraction pilot services with their staged release receipts.
- Declare each pilot service dependency on Fetch Cache and Fleet Graph, then regenerate the derived dependency slice.
