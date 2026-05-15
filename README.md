# services-registry

Single source of truth for the services across the `0exec.com` and `0crawl.com`
meshes. Dashboards (`hub.scrapetheworld.org`, `catalog.0exec.com`,
`services-dashboard.0crawl.com`) consume this file instead of hard-coding their
own service lists.

Adding or changing a service = one PR to one file.

## Stable URL

```
https://raw.githubusercontent.com/baditaflorin/services-registry/main/services.json
```

### Sliced URLs — fetch less when you only need part of the registry

`services.json` is ~280 KB, ~250 entries, ~26 fields each. For most
consumers (especially AI agents on a token budget) that's wildly more
than needed. The generator emits seven sibling projection files,
each a stable URL at the same path:

| URL suffix                | shape                                                       | size  | use when |
|---------------------------|-------------------------------------------------------------|-------|----------|
| `services.ids.json`       | `["a11y-quick", "accessibility-score", …]`                 | ~5 KB | "what services exist?" |
| `services.names.json`     | `[{id, name}]`                                              | ~13 KB | rendering a picker or menu |
| `services.minimal.json`   | `[{id, name, mesh, kind, category, language, trl, url}]`   | ~44 KB | catalog overview, list views |
| `services.urls.json`      | `[{id, url, health_url, example_path, auth_help}]`         | ~63 KB | building Open links / smoke targets |
| `services.trl.json`       | `[{id, trl, trl_ceiling, trl_assessed_at, trl_assessor}]`  | ~31 KB | TRL audits, re-scoring runs |
| `services.ports.json`     | `[{id, host_port, container_port}]`                        | ~12 KB | port allocation, conflict checks |
| `services.deploy.json`    | `[{id, mesh, kind, runtime, language, repo_url}]`          | ~40 KB | fleet-runner deploy targeting |

Slices are **derived** from `services.json` — never hand-edit them. They
are written compact (single line) on purpose: the human-readable view is
`services.json`; slices exist to minimize transferred bytes / tokens.
Rebuild without re-querying GitHub:

```bash
python3 bin/generate.py --slices-only
```

`bin/generate.py` (the normal full run) rebuilds them automatically
after writing `services.json`.

## What's in here

| file                       | purpose                                                            |
|----------------------------|--------------------------------------------------------------------|
| `services.json`            | the registry (array of entries)                                    |
| `services.<slice>.json`    | seven projection files (see "Sliced URLs" above) — auto-derived    |
| `schema/v1.json`           | JSON Schema for an entry                                           |
| `services.summary.txt`     | counts by mesh + category, rebuilt by `bin/build.py`               |
| `bin/generate.py`          | rebuild `services.json` + slices from GitHub topics + `overrides.json` |
| `bin/notify-consumers.sh`  | tell the catalog + hub to re-fetch (run after `git push`)          |
| `overrides.json`           | per-slug patches (curated names, descriptions, custom example URLs)|
| `bin/sync.sh`              | (legacy) snapshot the previous three sources                       |
| `bin/build.py`             | (legacy) merge those snapshots — superseded by `generate.py`       |
| `sources/`                 | (legacy) upstream snapshots, gitignored                            |

## Entry shape

```json
{
  "id":           "go-js-proxy",
  "name":         "Proxy (Go+JS)",
  "description":  "JS-rendering HTTP proxy",
  "category":     "proxy",
  "mesh":         "0exec",
  "tags":         ["go", "proxy"],
  "url":          "https://go-js-proxy.0exec.com",
  "health_url":   "https://go-js-proxy.0exec.com/_gw_health",
  "repo_url":     "https://github.com/baditaflorin/go-js-proxy",
  "example_path": "/?url=https://example.com",
  "auth": {
    "type":        "api_key",
    "query_param": "api_key",
    "header":      "X-API-Key"
  }
}
```

See [`schema/v1.json`](schema/v1.json) for the full contract.

## Bootstrap a new builder / ops machine

Everything an operator needs to set up a fresh Builder LXC (or any
new machine that wants to run `fleet-runner deploy`) lives in
[`scripts/`](scripts/). One command brings the box from zero to
fully-working:

```bash
# As root on the new machine, after SSH keys to github.com are set up:
curl -fsSL https://raw.githubusercontent.com/baditaflorin/services-registry/main/scripts/lxc-bootstrap.sh | bash
```

What it does (idempotent — re-run anytime to refresh):

1. apt-install `git`, `python3`, `golang`, `docker.io`, `docker-buildx`, `jq`, `curl`.
2. Clone (or pull) `services-registry`, `go_fleet_runner`, `go-common` into `/root/workspace/`.
3. Build `fleet-runner` from source and install to `/usr/local/bin/`.
4. Run `scripts/gen-etc-hosts.sh` to write the split-horizon `/etc/hosts` block — every fleet FQDN → the internal gateway IP (`10.10.10.10`). Bypasses the NAT-hairpin issue where the LXC can't reach the bastion's public IP cleanly from inside the mesh.
5. Generate an `ed25519` SSH key (if absent), print the public key, and tell the operator exactly which hosts (bastion / gateway / dockerhost) need it in their `authorized_keys`.
6. Run `fleet-runner clone-missing` so every service workspace is pulled.

No secrets are written by this script. The SSH private key never leaves the box; the public key is printed to stdout for manual install at the three fleet hops (whose own `authorized_keys` files are the only thing that matters for trust).

After install:

```bash
fleet-runner converge          # full drift report
fleet-runner state snapshot    # live fleet state
fleet-runner deploy <repo>     # idempotent end-to-end deploy
```

The `/etc/hosts` block alone can be re-applied:

```bash
sudo ./scripts/gen-etc-hosts.sh                          # local services.json
sudo ./scripts/gen-etc-hosts.sh --registry-url <url>     # from the public copy
sudo ./scripts/gen-etc-hosts.sh --dry-run                # preview only
```

The block is delimited by `# BEGIN fleet-split-horizon` / `# END fleet-split-horizon`; outside the block is untouched. Counter:current FQDNs as of last sync = 186.

## No secrets policy

The registry is **public**. It must never contain real API keys, signed tokens,
private endpoints, or anything you would not paste on a forum.

- For `auth.type = "api_key"` (the `0exec` mesh): consumers obtain a key
  out-of-band (issued on the docker VM with `apikey new`) and store it in their
  own browser / config. The registry only tells consumers *how* to send the key
  (`query_param` and `header`), not *what* it is.
- For `auth.type = "path_token"` (the `0crawl` mesh): the `public_demo_token`
  field is allowed and intentionally public. It must not provide privileged
  access — only enough for a "try it" link on a public dashboard.

If you find a real secret in this repo, treat it as a leak: rotate the credential
and open a PR to remove the value.

## How a consumer builds an "Open" link

Given an entry `s` and a user-supplied (or demo) token, construct the URL:

```js
function openLink(s, token) {
  if (s.auth.type === "none") {
    return s.url + (s.example_path || "");
  }
  if (s.auth.type === "path_token") {
    const t = token || s.auth.public_demo_token;
    if (!t) return null;
    const prefix = s.auth.path_template.replace("{token}", encodeURIComponent(t));
    return s.url + prefix + (s.example_path || "/");
  }
  // api_key
  if (!token) return null;
  const sep = (s.example_path || "").includes("?") ? "&" : "?";
  return s.url + (s.example_path || "/") + sep
       + s.auth.query_param + "=" + encodeURIComponent(token);
}
```

## How to add a service

The registry is regenerated from GitHub topics on every run of
`bin/generate.py`. There is no manual `services.json` editing.

1. Create or already-have the service repo under `baditaflorin/<name>`.
2. Tag it with the right topics — one mesh, one category, plus optional tags:
   ```bash
   gh repo edit baditaflorin/<name> \
     --add-topic mesh-0exec \
     --add-topic category-proxy \
     --add-topic go
   ```
3. (Optional) Add an entry to `overrides.json` if the auto-derived display
   name / description / example query needs to be hand-curated:
   ```json
   {
     "go-js-proxy": {
       "name": "Proxy (Go+JS)",
       "example_path": "/?url=https://example.com"
     }
   }
   ```
4. Regenerate and push:
   ```bash
   python3 bin/generate.py
   git add services.json services.*.json overrides.json services.summary.txt
   git commit -m "feat: add <slug>"
   git push
   bin/notify-consumers.sh    # tells the live dashboards to re-fetch
   ```

## Propagating CLAUDE.md / SERVICE-TEMPLATE.md after a change here

`CLAUDE.md` and `SERVICE-TEMPLATE.md` in this repo are the **canonical**
copies; per-repo copies in the ~274 fleet workspaces are propagated
snapshots, not independent edits. After merging a change to either file
**you must re-run propagation** — there is no automation today (see
[issue #2](https://github.com/baditaflorin/services-registry/issues/2)).

From any machine with SSH to the bastion:

```bash
ssh root@0docker.com 'pct exec 108 -- bash -lc "
  cd /root/workspace/services-registry &&
  git pull --ff-only &&
  /usr/local/bin/fleet-runner inject services-registry/CLAUDE.md CLAUDE.md &&
  /usr/local/bin/fleet-runner push \"docs(CLAUDE.md): propagate from services-registry\"
"'
```

Repeat with `SERVICE-TEMPLATE.md` if that file changed. The
`fleet-runner push` step uses `git add -A` per workspace — confirm all
workspaces are clean first with:

```bash
ssh root@0docker.com 'pct exec 108 -- bash -lc "
  cd /root/workspace && for d in */; do [ -d \$d/.git ] || continue;
  out=\$(cd \$d && git status --porcelain); [ -n \"\$out\" ] && echo \"DIRTY: \$d\"; done
"'
```

If any workspace shows dirty output unrelated to your propagation,
resolve it before running push (otherwise the sweep commits unrelated
files into that repo). Empty output = safe to propagate.

**Why this is manual:** [issue #2](https://github.com/baditaflorin/services-registry/issues/2)
tracks options to automate (GitHub Action on merge, daily cron, etc.).
The current consensus is that the cost of forgetting is low — per-repo
copies are ~99% identical to canonical even when stale, and AI agents
read whichever version they get at session start — so a documented
manual step beats fleet-wide auto-push for now.

## Topic conventions

| topic              | meaning                                                     |
|--------------------|-------------------------------------------------------------|
| `mesh-0exec`       | service in the 0exec.com mesh (auth: api_key)              |
| `mesh-0crawl`      | service in the 0crawl.com mesh (auth: path_token)          |
| `mesh-pages`       | static GitHub Pages site (no auth)                          |
| `category-<x>`     | one of: proxy, search, ocr, geo, nlp, content, domains,    |
|                    | security, recon, infrastructure, web-analysis, visualization |
| anything else      | rendered as a tag (e.g. `go`, `node`, `python`, `c`)       |

GitHub topics force lowercase + hyphens. The generator normalizes
`web-analysis` back to `web_analysis` for the `category` field, matching
the convention used by hub icons and the legacy 0crawl dashboard JSON.

## Consumers

- [`hub_scrapetheworld_org`](https://github.com/baditaflorin/hub_scrapetheworld_org) — admin GUI Directory panel
- [`go-catalog-service`](https://github.com/baditaflorin/go-catalog-service) — public catalog at `catalog.0exec.com`
- [`go_services_dashboard`](https://github.com/baditaflorin/go_services_dashboard) — public dashboard at `services-dashboard.0crawl.com`
