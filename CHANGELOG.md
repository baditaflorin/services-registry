## 2026-10-09
- Sync five Go services to their merged profiling-enabled versions: captcha-detector 1.2.12, citation-reference-extractor 0.1.15, comment-extractor 1.4.7, consent-simulator 0.1.7, and crawl-web-application 1.3.4.
- Retire four browser-local Whisper apps from GitHub Pages and the active catalog; preserve their source repositories.
- Retire Kokoro OpenVINO Adapter from the active service catalog; retain its source repository as a backup.

# Changelog

## 2026-10-10

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
