## 2026-10-14
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
