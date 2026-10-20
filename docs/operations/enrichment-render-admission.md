# Enrichment render admission

The shared fetch cache gates only cache misses entering the JS, JS-network and
HTML renderer chains. Cached hits bypass admission, and direct HTTP fetches have
a separate MAX_FETCH_INFLIGHT limit. MAX_RENDER_INFLIGHT=96 replaces the earlier
12-slot containment setting after both browser renderer pools reached five hosts.
The observed pool exposes 35 JS and 20 JS-network slots, plus four independent
HTML proxy backends. The frontend cap is shared across those request classes.
The cache app memory ceiling rises from 3 GiB to 6 GiB to allow for larger
concurrent response buffers; its CPU ceiling remains four cores. This is a
closely observed trial: allow for growth from current RSS to the full 6 GiB
ceiling when estimating host headroom. Check RSS/peak/OOM and backend rejections,
and roll back if useful throughput falls or memory approaches its limit.
It is a bounded frontend allowance, not a promise of 96 Chromium sessions.

Use the private runtime recipe for the approved pooled renderer URLs; preserve
local fallback URLs only for an unreachable gateway, not for every HTTP error.
Do not set public mesh HTTPS hosts to direct egress.

Verify loadshed_in_flight and loadshed_shed_total for gate=render, per-service
DomainScope attempts and fresh successful stored results. A target challenge page
is not usable site content and must not be counted as a successful detector run.
Transient proxy/renderer failures must preserve the previous successful result.

For a configuration-only change, render the exact one-service Compose overlay,
inspect the diff, preserve the previous overlay and protected environment, and
recreate only the app with its verified existing immutable image. Never pull or
recreate Redis for an admission setting. Roll back both configuration files if
health/work probes fail. Keep the durable DomainScope batch running.
