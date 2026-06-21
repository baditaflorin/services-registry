#!/usr/bin/env bash
# fleet-diag.sh — one-shot infrastructure triage for the domainscope fleet
#
# Usage (from laptop):
#   ssh -J root@0docker.com ubuntu_vm@10.10.10.20 'sudo bash /opt/scripts/fleet-diag.sh'
# Or pipe from local:
#   ssh -J root@0docker.com ubuntu_vm@10.10.10.20 'sudo bash -s' < fleet-diag.sh
#
# What it checks (~40s):
#   1. Host resources (CPU load, RAM, disk)
#   2. Critical infra containers — fetch-cache, its proxy deps (js-proxy, html-proxy,
#      js-proxy-network), Redis, apikey, local categorizer. Shows uptime + restart count
#      to catch "healthy but just restarted 30 seconds ago" cases.
#   3. Fetch cache 200/502 ratio + latency last 2 min
#   4. Categorizer on 10.10.10.30 — health + timed POST probe (flags if close to 30s limit)
#   5. Domain processor state + concurrency + failure rate/5min
#   6. Upstream enricher errors from categorizer: proxy-timeout vs 404 (expected) vs 400 (bug)
#   7. Nginx connection-refused error count

set -euo pipefail

RED='\033[0;31m'; YEL='\033[0;33m'; GRN='\033[0;32m'; BLU='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'

sep()  { echo -e "\n${BOLD}${BLU}═══ $1 ═══${NC}"; }
ok()   { echo -e "  ${GRN}✓${NC} $*"; }
warn() { echo -e "  ${YEL}⚠${NC}  $*"; }
err()  { echo -e "  ${RED}✗${NC} $*"; }
note() { echo -e "     $*"; }

# Human-readable uptime from ISO-8601 start timestamp
human_uptime() {
  python3 -c "
from datetime import datetime, timezone
try:
    t = '$1'[:26].rstrip('Z') + '+00:00'
    s = int((datetime.now(timezone.utc) - datetime.fromisoformat(t)).total_seconds())
    if s < 0:     print('?')
    elif s < 60:  print(f'{s}s')
    elif s < 3600:print(f'{s//60}m{s%60:02d}s')
    else:         print(f'{s//3600}h{(s%3600)//60}m')
except: print('?')
" 2>/dev/null || echo "?"
}

# Returns 0 if started within last N seconds, 1 otherwise
started_within() {
  local started="$1" secs="$2"
  python3 -c "
from datetime import datetime, timezone
try:
    t = '$started'[:26].rstrip('Z') + '+00:00'
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(t)).total_seconds()
    exit(0 if 0 < age < $secs else 1)
except: exit(1)
" 2>/dev/null
}

# ── 1. HOST RESOURCES ──────────────────────────────────────────────────────────
sep "HOST RESOURCES (10.10.10.20)"
LOAD=$(cut -d' ' -f1-3 /proc/loadavg)
CORES=$(nproc)
LOAD1=$(cut -d' ' -f1 /proc/loadavg)
echo "  CPU cores: $CORES  |  Load avg (1/5/15): $LOAD"
if python3 -c "import sys; sys.exit(0 if float('$LOAD1') > $CORES * 2 else 1)" 2>/dev/null; then
  err "LOAD CRITICAL: $LOAD1 > $(( CORES * 2 )) (2× core count) — system overloaded"
elif python3 -c "import sys; sys.exit(0 if float('$LOAD1') > $CORES else 1)" 2>/dev/null; then
  warn "Load elevated: $LOAD1 > $CORES cores"
else
  ok "Load normal: $LOAD1"
fi
free -h | awk '/Mem:/{
  gsub(/i/,"",$2); gsub(/i/,"",$3); gsub(/i/,"",$4)
  printf "  RAM: used=%s / total=%s  free=%s\n",$3,$2,$4
}'
df -h / | awk 'NR==2{
  pct=$5+0
  msg=sprintf("  Disk /: used=%s / %s (%s full)", $3, $2, $5)
  if(pct>=90) print "  \033[0;31m✗\033[0m " msg " — CRITICAL"
  else if(pct>=80) print "  \033[0;33m⚠\033[0m  " msg " — WARNING"
  else print "  \033[0;32m✓\033[0m " msg
}'

# ── 2. CRITICAL INFRA CONTAINERS ───────────────────────────────────────────────
sep "CRITICAL INFRASTRUCTURE CONTAINERS"

check_container() {
  local name="$1" port="$2" health_path="${3:-SKIP}"
  local status health mem cpu pids restarts started uptime memlimit_bytes memlimit_hr

  status=$(    docker inspect "$name" --format '{{.State.Status}}'         2>/dev/null || echo "missing")
  health=$(    docker inspect "$name" --format '{{.State.Health.Status}}'  2>/dev/null || echo "n/a")
  mem=$(       docker stats   "$name" --no-stream --format '{{.MemUsage}}' 2>/dev/null || echo "n/a")
  cpu=$(       docker stats   "$name" --no-stream --format '{{.CPUPerc}}'  2>/dev/null || echo "n/a")
  pids=$(      docker stats   "$name" --no-stream --format '{{.PIDs}}'     2>/dev/null || echo "?")
  restarts=$(  docker inspect "$name" --format '{{.RestartCount}}'         2>/dev/null || echo "?")
  started=$(   docker inspect "$name" --format '{{.State.StartedAt}}'      2>/dev/null || echo "")
  memlimit_bytes=$(docker inspect "$name" --format '{{.HostConfig.Memory}}' 2>/dev/null || echo "0")
  memlimit_hr=$(python3 -c "m=int('$memlimit_bytes'); print(f'{m/1073741824:.1f}GB' if m>0 else 'unlim')" 2>/dev/null || echo "?")
  uptime=$(human_uptime "$started")

  local restart_flag=""
  if [[ "$restarts" != "?" && "$restarts" -gt 0 ]] && started_within "$started" 600; then
    restart_flag=" ${YEL}[JUST RESTARTED — uptime ${uptime}]${NC}"
  fi

  local line="${name}  cpu=${cpu}  mem=${mem} (limit=${memlimit_hr})  pids=${pids}  up=${uptime}  restarts=${restarts}  health=${health}"

  if   [[ "$status" != "running" ]]; then err  "$line  STATUS=$status$restart_flag"
  elif [[ "$health" == "unhealthy" ]]; then err "$line$restart_flag"
  elif [[ "$health" == "starting"  ]]; then warn "$line$restart_flag"
  else                                      ok   "$line$restart_flag"
  fi

  # Flag js-proxy PID explosion (headless Chrome leak)
  if [[ "$name" == "go-js-proxy" ]]; then
    local pnum="${pids//[^0-9]/}"
    if [[ -n "$pnum" && "$pnum" -gt 200 ]]; then
      err "  go-js-proxy has ${pids} PIDs — Chrome spawn explosion, restart immediately: docker restart go-js-proxy"
    elif [[ -n "$pnum" && "$pnum" -gt 100 ]]; then
      warn "  go-js-proxy has ${pids} PIDs — Chrome leak building, watch closely"
    fi
  fi

  if [[ -n "$port" && "$health_path" != "SKIP" ]]; then
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 \
           "http://localhost:$port$health_path" 2>/dev/null || echo "timeout")
    if [[ "$code" == "200" ]]; then note "  HTTP :${port}${health_path} → ${code}"
    else                           err  "  HTTP :${port}${health_path} → ${code}"
    fi
  fi
}

echo "  ── fetch-cache + its proxy backends ──"
check_container "go-infrastructure-fetch-cache"       "18205" "/health"
check_container "go_infrastructure_fetch_cache_redis" ""      "SKIP"
check_container "go-js-proxy"                         ""      "SKIP"
check_container "go-html-proxy"                       ""      "SKIP"
check_container "go-js-proxy-network"                 ""      "SKIP"

echo "  ── auth + local categorizer ──"
check_container "go-apikey-service"                   ""      "SKIP"
check_container "go-url-categorizer-api"              "18244" "/health"

# ── 3. FETCH CACHE QUALITY ─────────────────────────────────────────────────────
sep "FETCH CACHE QUALITY (last 2 min)"
fc_tmp=$(mktemp)
docker logs go-infrastructure-fetch-cache --since 2m 2>&1 \
  | grep '"msg":"request_completed"' > "$fc_tmp" 2>/dev/null || true

if [[ ! -s "$fc_tmp" ]]; then
  warn "No fetch cache request_completed logs in last 2 min (idle or container restarting)"
else
  # Read from temp file to avoid env var ARG_MAX overflow on high-traffic logs
  python3 - "$fc_tmp" << 'PY'
import sys, json, re
total = s200 = s502 = s404 = other = 0
durations = []
with open(sys.argv[1]) as f:
    for l in f:
        if not l.strip(): continue
        total += 1
        try:
            d = json.loads(l)
            c = d.get('status', 0)
            dur = d.get('duration', '')
            if   c == 200: s200 += 1
            elif c == 502: s502 += 1
            elif c == 404: s404 += 1
            else:          other += 1
            if dur:
                m = re.match(r'([\d.]+)', str(dur))
                if m: durations.append(float(m.group(1)))
        except: pass

p200 = s200/total*100 if total else 0
p502 = s502/total*100 if total else 0
avg  = sum(durations)/len(durations) if durations else 0
mxd  = max(durations) if durations else 0

if p200 >= 80:   sym, label = '\033[0;32m✓\033[0m', 'OK'
elif p200 >= 50: sym, label = '\033[0;33m⚠\033[0m ', 'DEGRADED'
else:            sym, label = '\033[0;31m✗\033[0m', 'CRITICAL — PROXY CHAIN DOWN'

print(f"  {sym} {total} reqs: 200={s200}({p200:.0f}%) 502={s502}({p502:.0f}%) 404={s404} other={other}")
print(f"       avg={avg:.2f}s  max={mxd:.2f}s  status={label}")
if p502 > 20:
    print(f"  \033[0;31m✗\033[0m  High 502 rate — js-proxy/html-proxy overloaded or crashed")
    print(f"       Fix: docker restart go-js-proxy go-html-proxy go-js-proxy-network")
PY
fi
rm -f "$fc_tmp"

# ── 4. CATEGORIZER (10.10.10.30) ───────────────────────────────────────────────
sep "CATEGORIZER API (10.10.10.30:23481)"
cat_health=$(curl -s --max-time 5 http://10.10.10.30:23481/health 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('data',{}).get('status','?'))" \
  2>/dev/null || echo "TIMEOUT")
[[ "$cat_health" == "up" ]] && ok "GET /health → $cat_health" || err "GET /health → $cat_health"

t0=$(date +%s%3N)
cat_post_raw=$(curl -s --max-time 35 -X POST http://10.10.10.30:23481/api/v1/categorize \
  -H 'Content-Type: application/json' -d '{"url":"example.com"}' 2>/dev/null || echo "CURL_TIMEOUT")
t1=$(date +%s%3N)
elapsed_ms=$(( t1 - t0 ))
elapsed_s=$(python3 -c "print(f'{$elapsed_ms/1000:.1f}s')")

cat_result=$(echo "$cat_post_raw" | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  cat=d.get('data',{}).get('category','?')
  ec=d.get('error',{}).get('code','')
  print(f'OK category={cat}' if not ec else f'ERROR {ec}')
except: print('PARSE_ERROR/TIMEOUT')
" 2>/dev/null || echo "TIMEOUT")

if [[ "$cat_result" == "OK"* ]]; then ok "POST /categorize → $cat_result  (${elapsed_s})"
else                                    warn "POST /categorize → $cat_result  (${elapsed_s})"
fi

# Response time vs 30s processor deadline
if python3 -c "import sys; sys.exit(0 if $elapsed_ms > 25000 else 1)" 2>/dev/null; then
  err "  Response time ${elapsed_s} > 25s — dangerously close to processor 30s timeout"
  note "  Fix: reduce NET_HEADER_TIMEOUT in categorizer compose (currently 30 → set to 15)"
elif python3 -c "import sys; sys.exit(0 if $elapsed_ms > 15000 else 1)" 2>/dev/null; then
  warn "  Response time ${elapsed_s} elevated (>15s) — slow domains may hit processor timeout"
fi

CAT_STATS=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
  root@10.10.10.30 "docker stats domainscope-api --no-stream --format \
  'cpu={{.CPUPerc}} mem={{.MemUsage}} pids={{.PIDs}}'" \
  2>/dev/null || echo "ssh_unavailable")
note "Stats (.30): $CAT_STATS"

# ── 5. DOMAIN PROCESSORS ───────────────────────────────────────────────────────
sep "DOMAIN PROCESSORS (load generators)"
running=0
for suffix in a b c d; do
  name="go-domainscope-com-${suffix}-13-icann-domains-domain-processor-1"
  status=$(docker inspect "$name" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
  if [[ "$status" == "running" ]]; then
    recent_fails=$(docker logs "$name" --since 5m 2>&1 | grep -c "Failed after" || true)
    recent_ok=$(   docker logs "$name" --since 5m 2>&1 | grep -c "Successfully" || true)
    concurrency=$( docker inspect "$name" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
                   | grep '^CONCURRENCY=' | cut -d= -f2 || echo "?")
    warn "$name: RUNNING  concurrency=${concurrency}  ok=${recent_ok}/5min  fail=${recent_fails}/5min"
    running=$(( running + 1 ))
  else
    ok "$name: $status"
  fi
done
if [[ "$running" -eq 0 ]]; then
  note "All processors stopped — load reduced"
else
  warn "$running processor(s) running — to stop all:"
  note "  docker stop go-domainscope-com-{a,b,c,d}-13-icann-domains-domain-processor-1"
fi

# ── 6. UPSTREAM ENRICHER ERRORS ────────────────────────────────────────────────
sep "UPSTREAM ENRICHER ERRORS (last 5 min, from .30 categorizer)"
cat_tmp=$(mktemp)
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
  root@10.10.10.30 "docker logs domainscope-api --since 5m 2>&1" > "$cat_tmp" 2>/dev/null || true

if [[ ! -s "$cat_tmp" ]]; then
  warn "Cannot reach 10.10.10.30 via SSH — run from bastion for this section"
  note "  ssh root@0docker.com 'ssh root@10.10.10.30 docker logs domainscope-api --since 5m 2>&1'"
else
  python3 - "$cat_tmp" << 'PY'
import sys, json
from collections import defaultdict

proxy_timeout = defaultdict(int)
not_found     = defaultdict(int)
bad_request   = defaultdict(int)
other_errors  = defaultdict(int)

with open(sys.argv[1]) as f:
    for l in f:
        if not l.strip(): continue
        try:
            d = json.loads(l)
            err = d.get('error','')
            svc = d.get('service','unknown')
            if not err: continue
            if 'proxy: all providers failed' in err or 'timeout awaiting response headers' in err:
                proxy_timeout[svc] += 1
            elif 'HTTP 404' in err:
                not_found[svc] += 1
            elif 'HTTP 400' in err:
                bad_request[svc] += 1
            elif err:
                other_errors[svc] += 1
        except:
            pass

pt = sum(proxy_timeout.values())
nf = sum(not_found.values())
br = sum(bad_request.values())
oe = sum(other_errors.values())

if pt == 0:
    print(f"  \033[0;32m✓\033[0m  No proxy-timeout errors — fetch-cache chain healthy")
else:
    print(f"  \033[0;31m✗\033[0m  {pt} proxy-timeout errors (fetch-cache/proxy chain overloaded):")
    for svc, cnt in sorted(proxy_timeout.items(), key=lambda x:-x[1])[:10]:
        print(f"       {cnt:5d}× {svc}")

if bad_request:
    print(f"  \033[0;33m⚠\033[0m   {br} HTTP 400 errors (bad request — possible enricher API bug):")
    for svc, cnt in sorted(bad_request.items(), key=lambda x:-x[1])[:5]:
        print(f"       {cnt:5d}× {svc}")

if other_errors:
    print(f"  \033[0;33m⚠\033[0m   {oe} other upstream errors:")
    for svc, cnt in sorted(other_errors.items(), key=lambda x:-x[1])[:5]:
        print(f"       {cnt:5d}× {svc}")

if nf > 0:
    print(f"       {nf} HTTP 404s across {len(not_found)} enrichers (domain data absent — expected, not a bug)")
PY
fi
rm -f "$cat_tmp"

# ── 7. NGINX ───────────────────────────────────────────────────────────────────
sep "NGINX (webgateway 10.10.10.10)"
nginx_out=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
  root@10.10.10.10 \
  "tail -n 500 /var/log/nginx/domainscope.scrapetheworld.org.error.log \
   | grep -c 'Connection refused'" 2>/dev/null || echo "ssh_unavailable")
if [[ "$nginx_out" == "ssh_unavailable" ]]; then
  warn "Cannot SSH to webgateway — run from bastion: ssh root@0docker.com 'ssh root@10.10.10.10 ...'"
else
  [[ "$nginx_out" -gt 50 ]] && \
    err "Nginx: $nginx_out conn-refused errors in last 500 lines — upstream DOWN" || \
    ok  "Nginx: $nginx_out conn-refused errors in last 500 lines"
fi

sep "DONE — $(date -u)"
echo ""
echo "  Quick remediation:"
echo "  # Restart fetch cache (clears memory):    docker restart go-infrastructure-fetch-cache"
echo "  # Restart js-proxy (kills Chrome leaks):  docker restart go-js-proxy"
echo "  # Restart html-proxy:                     docker restart go-html-proxy go-js-proxy-network"
echo "  # Stop all processors:                    docker stop go-domainscope-com-{a,b,c,d}-13-icann-domains-domain-processor-1"
echo "  # Restart categorizer on .30:             ssh root@10.10.10.30 'docker restart domainscope-api'"
echo "  # Disk cleanup (docker):                  docker system prune -f --volumes"
