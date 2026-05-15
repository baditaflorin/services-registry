# Unattended automation — runbook for agents

Canonical operating doc for any AI agent (Claude / Gemini / Haiku / etc.)
working in this fleet **without a human in the loop**. Three new fleet
primitives let you ship a brand-new service from "local code" to "live
with DNS, scope, and secrets" with zero operator intervention beyond a
single `fleet-runner deploy`.

Read this BEFORE you ask the user for tokens, IP addresses, or where
secrets live. The answers are codified here.

---

## The unattended chain

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   go-fleet-secrets    (encrypted vault: NaCl secretbox, scoped reads)│
│        :18140         holds hcloud_token, github_pat, smtp_*,        │
│                       hackerone_api_token, …                         │
│                                                                      │
│           ▼ reads "hcloud_token" with X-Auth-User scope check        │
│                                                                      │
│   go-fleet-dns-sync   (Hetzner Cloud API reconciler)                 │
│        :18141         services-registry → DNS, idempotent,           │
│                       30-min ticker, extras NEVER auto-deleted       │
│                                                                      │
│           ▼ DNS green, port green, secrets green                     │
│                                                                      │
│   go-fleet-preflight  (pre-deploy checklist)                         │
│        :18142         registry + DNS + port + secrets in parallel    │
│                       returns 200 if green, 424 with checklist if red│
│                                                                      │
│           ▼ deploy decision                                          │
│                                                                      │
│   fleet-runner deploy go_<id>    (existing fleet tool on LXC 108)    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## How to add a new service unattended

For an agent operating without a human:

### 1. Create the local repo

```bash
mkdir -p /Users/live/Documents/Codex/2026-05-08/<repo-name>
cd <repo-name>
# Standard fleet scaffold (main.go, handler.go, service.yaml, Dockerfile, etc.)
# Copy structure from a similar repo (e.g. go-pentest-target-normalizer).
```

`service.yaml` MUST have at minimum:
- `id`, `name`, `version`, `port`, `category`
- `health` block
- `nginx.subdomain: <id>.0exec.com` (or `.0crawl.com`)

### 2. Push to private GitHub

```bash
git init -q && git branch -M main && git add -A
git -c user.email=baditaflorin@gmail.com -c user.name=baditaflorin commit -q -m "feat: scaffold <id> v0.1.0"
gh repo create "baditaflorin/<id>" --private --source=. --remote=origin --push
```

### 3. Set 3 topics on the repo

The topic-driven generator at `services-registry/bin/generate.py` discovers
new repos by GitHub topic search. Missing topics → invisible:

```bash
gh api -X PUT "repos/baditaflorin/<id>/topics" \
  -F "names[]=mesh-0exec" \
  -F "names[]=<your-fleet-tag>" \
  -F "names[]=category-<your-category>"
```

Valid categories (enum in generator):
`app, archive, content, domains, geo, infrastructure, nlp, ocr, peer_to_peer, proxy, recon, security, seo, visualization, web_analysis, wellness`

### 4. Add an `overrides.json` entry

Hand-curated fields (port, description, TRL, example_path) belong in
`services-registry/overrides.json` — the generator merges them onto the
auto-derived skeleton. Add via a small Python snippet:

```python
import json
with open('overrides.json') as f: o = json.load(f)
o['<id>'] = {
    'container_port': <port>, 'host_port': <port>,
    'description': '...',
    'example_path': '/health',
    'trl': 5,
    'trl_evidence': '...',
    'trl_ceiling': 8, 'trl_ceiling_reason': '...',
    'trl_assessed_at': '<YYYY-MM-DD>',
    'trl_assessor': '<agent-id>',
}
with open('overrides.json', 'w') as f: json.dump(o, f, indent=2, sort_keys=True)
```

### 5. Regenerate the registry

```bash
cd services-registry
python3 bin/generate.py        # rebuilds services.json + 7 slice files
git add services.json overrides.json services.*.json services.summary.txt
git -c user.email=baditaflorin@gmail.com -c user.name=baditaflorin commit -m "registry: add <id>"
git push
```

### 6. Wait OR force DNS sync

`go-fleet-dns-sync` has a 30-min ticker that automatically detects new
registry entries and creates A records. To skip the wait:

```bash
curl -s -X POST 'https://go-fleet-dns-sync.0exec.com/sync?api_key=default_token' | jq
```

### 7. Verify with preflight

```bash
curl -fsS 'https://go-fleet-preflight.0exec.com/preflight/<id>?api_key=default_token' | jq
```

- **200 + `{"ok": true, ...}`** → ready to deploy
- **424 Failed Dependency** → check `.checks[] | select(.ok == false)` for what failed

### 8. Deploy

```bash
# Via the local shim (private SSH topology lives in fleet-state/OPS.md):
fleet-runner deploy go_<id>
```

Done. Total time: <5 minutes if you force-sync, ~30 min if you wait for the ticker.

---

## Secrets — every token lives in the vault

All infrastructure tokens (Hetzner Cloud, GitHub PAT, SMTP creds,
HackerOne / Bugcrowd / Intigriti API keys, etc.) live in
`go-fleet-secrets` — NEVER in env vars on dockerhost, NEVER in service
repos, NEVER in `services.json` or `overrides.json`.

### Seed a secret (admin)

```bash
ADMIN=$(cat /root/.fleet-secrets-admin.token)
curl -s -X POST 'https://go-fleet-secrets.0exec.com/secrets?api_key=default_token' \
  -H "X-Admin-Token: $ADMIN" \
  -d '{
    "name": "hcloud_token",
    "value": "<TOKEN>",
    "consumers": ["go-fleet-dns-sync"],
    "description": "Hetzner Cloud API token for DNS management"
  }'
```

### Read from a consumer service

A consumer holds its OWN keystore-issued api_key. The gateway converts
that into `X-Auth-User: <service-id>`, which the vault checks against the
consumers list:

```go
req, _ := http.NewRequest("GET", "https://go-fleet-secrets.0exec.com/secrets/hcloud_token", nil)
req.Header.Set("X-API-Key", os.Getenv("FLEET_API_KEY")) // this service's own key
resp, _ := http.DefaultClient.Do(req)
// {"name":"hcloud_token","value":"..."}
```

### Rotate a secret (the procedure that should fire after a leak)

1. Generate a new value at the source (Hetzner console, GitHub, etc.).
2. Patch the vault:
   ```bash
   curl -s -X POST 'https://go-fleet-secrets.0exec.com/secrets?api_key=default_token' \
     -H "X-Admin-Token: $ADMIN" \
     -d '{"name":"hcloud_token","value":"<NEW>","consumers":["go-fleet-dns-sync"]}'
   ```
3. Restart the consumer service (or wait 15 min for its keystore cache to TTL out).
4. **Delete the old token at the source.**

---

## Bootstrap (one-time, the chicken-and-egg)

`go-fleet-secrets` itself needs its master key out-of-band. On LXC 108:

```bash
openssl rand -hex 32 | sudo tee /root/.fleet-secrets-master.key
openssl rand -hex 32 | sudo tee /root/.fleet-secrets-admin.token
sudo chmod 600 /root/.fleet-secrets-*

# Pass to docker-compose via env_file. NEVER bake into image.
sudo tee /opt/services/go-fleet-secrets/.env <<EOF
SECRETS_MASTER_KEY=$(cat /root/.fleet-secrets-master.key)
SECRETS_ADMIN_TOKEN=$(cat /root/.fleet-secrets-admin.token)
EOF
sudo chmod 600 /opt/services/go-fleet-secrets/.env
```

**Keep a sealed offsite backup of both files.** If LXC 108 is reimaged
without these, the vault's data is unrecoverable.

---

## Hetzner DNS reference

- **Zone**: `0exec.com` — id `1285812` in Hetzner Cloud
- **API**: `https://api.hetzner.cloud/v1` (Bearer auth) — the **canonical**
  surface that supersedes the deprecated `dns.hetzner.com` Console API.
- **Gateway IP**: `176.9.123.221` — every fleet service points here; nginx
  on `mesh-0exec` terminates TLS + routes to the right upstream port.
- **Token env name**: `HCLOUD_TOKEN` (matches hcloud-cli + official Go SDK).
  `HETZNER_TOKEN` is kept as a back-compat alias.

---

## What an agent SHOULD do before deploying

```bash
# 1. preflight returns 200 (all green)?
curl -fsS "https://go-fleet-preflight.0exec.com/preflight/<id>?api_key=default_token" \
  | jq -e '.ok' || { echo "preflight failed"; exit 1; }

# 2. If red, see which check
curl -s "..." | jq '.checks[] | select(.ok == false)'

# 3. Fix the red check (force dns-sync, register port, add secret), re-run preflight
# 4. Deploy
fleet-runner deploy go_<id>

# 5. Smoke
curl -fsS "https://<id>.0exec.com/health"
```

---

## Anti-patterns observed in agent sessions

1. **"I'll ask the user for the Hetzner token."** Don't. It's in
   `go-fleet-secrets` under name `hcloud_token`. If your service is in
   that secret's `consumers` allowlist, you can read it.

2. **"I'll set HETZNER_TOKEN in /etc/environment on LXC 108."** Don't.
   Secrets live in the vault, not in environment files. The bootstrap
   path is acceptable for `go-fleet-secrets`'s own master key — nothing
   else.

3. **"I'll create the DNS A record manually in the Hetzner UI."** Don't.
   Add the service to the registry; the dns-sync ticker handles it. UI
   edits drift and break the idempotent reconciler.

4. **"`dns.hetzner.com/api/v1` is the Hetzner DNS API."** Deprecated.
   Use `api.hetzner.cloud/v1` (different auth, different resource model,
   different token type).

5. **"I'll deploy without preflight."** Don't. Preflight is two seconds;
   the cost of a half-deployed service that needs cleanup is hours.

---

## Repos in the chain

| Service | Role | Port | Repo |
|---------|------|------|------|
| `go-fleet-secrets` | Encrypted vault for tokens | 18140 | [github.com/baditaflorin/go-fleet-secrets](https://github.com/baditaflorin/go-fleet-secrets) |
| `go-fleet-dns-sync` | Registry → Hetzner DNS reconciler | 18141 | [github.com/baditaflorin/go-fleet-dns-sync](https://github.com/baditaflorin/go-fleet-dns-sync) |
| `go-fleet-preflight` | Pre-deploy checklist | 18142 | [github.com/baditaflorin/go-fleet-preflight](https://github.com/baditaflorin/go-fleet-preflight) |

Each repo has its own README with API details. This file is the operational
playbook that ties them together.
