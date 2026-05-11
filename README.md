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

## What's in here

| file                       | purpose                                                            |
|----------------------------|--------------------------------------------------------------------|
| `services.json`            | the registry (array of entries)                                    |
| `schema/v1.json`           | JSON Schema for an entry                                           |
| `services.summary.txt`     | counts by mesh + category, rebuilt by `bin/build.py`               |
| `bin/sync.sh`              | refresh the upstream snapshots in `sources/` via `gh`              |
| `bin/build.py`             | merge the snapshots into `services.json`                           |
| `bin/notify-consumers.sh`  | tell the catalog + hub to re-fetch (run after `git push`)          |
| `sources/`                 | upstream snapshots — never hand-edited                             |

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

1. Add one entry to `services.json` matching `schema/v1.json`.
2. Run `python3 bin/build.py` if you also edited upstream sources, otherwise
   skip this step (manual edits are allowed; the rebuild is destructive).
3. Open a PR. Validators run in CI; once merged, all consuming dashboards
   pick up the change on their next refresh (≤ 5 min for `catalog.0exec.com`).

## How to refresh from upstream

```bash
bin/sync.sh        # pulls latest 0crawl services.json, 0exec catalog, hub directory
python3 bin/build.py   # rebuilds services.json + services.summary.txt
git diff services.json # review
```

Note: `build.py` is destructive — it overwrites `services.json` from the
snapshots. If you've made manual edits since the last sync, capture them in
the script before regenerating.

## Consumers

- [`hub_scrapetheworld_org`](https://github.com/baditaflorin/hub_scrapetheworld_org) — admin GUI Directory panel
- [`go-catalog-service`](https://github.com/baditaflorin/go-catalog-service) — public catalog at `catalog.0exec.com`
- [`go_services_dashboard`](https://github.com/baditaflorin/go_services_dashboard) — public dashboard at `services-dashboard.0crawl.com`
