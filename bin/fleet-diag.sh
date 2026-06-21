#!/usr/bin/env bash
# fleet-diag.sh — parallel one-shot triage for the domainscope fleet
#
# Usage (from laptop):
#   ssh -J root@0docker.com ubuntu_vm@10.10.10.20 'sudo bash /opt/scripts/fleet-diag.sh'
#
# What it checks (sections A-F run in parallel after HOST):
#   HOST  — CPU load, RAM, disk (instant)
#   A. Critical infra containers: fetch-cache, js-proxy, html-proxy, js-proxy-network,
#      Redis, apikey, local categorizer. Batched docker stats+inspect (1 call each),
#      shows uptime + restart flag + PID explosion alert for go-js-proxy.
#   B. Fetch cache quality: 200/502 ratio, avg latency, top 10 callers (enricher flood detection)
#   C. Categorizer on 10.10.10.30 — health, timed POST probe, resource stats (all in parallel)
#   D. Domain processors — state, concurrency, fail rate/5min (4 log reads in parallel)
#   E. Upstream enricher error breakdown: proxy-timeout vs 404 (expected) vs 400 (bug)
#   F. Nginx connection-refused count on webgateway
#
# Wall clock: ~8-15s healthy, ~35s if categorizer POST is slow (dominates).
# Compares to ~60-90s sequential.

set -euo pipefail

RED='\033[0;31m'; YEL='\033[0;33m'; GRN='\033[0;32m'; BLU='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'
sep()  { echo -e "\n${BOLD}${BLU}═══ $1 ═══${NC}"; }
ok()   { echo -e "  ${GRN}✓${NC} $*"; }
warn() { echo -e "  ${YEL}⚠${NC}  $*"; }
err()  { echo -e "  ${RED}✗${NC} $*"; }
note() { echo -e "     $*"; }

# ── HOST (instant — reads /proc, no I/O waits) ─────────────────────────────────
sep "HOST RESOURCES (10.10.10.20)"
LOAD=$(cut -d' ' -f1-3 /proc/loadavg)
CORES=$(nproc)
LOAD1=$(cut -d' ' -f1 /proc/loadavg)
echo "  CPU cores: $CORES  |  Load avg (1/5/15): $LOAD"
if python3 -c "import sys; sys.exit(0 if float('$LOAD1') > $CORES * 2 else 1)" 2>/dev/null; then
  err "LOAD CRITICAL: $LOAD1 > $(( CORES * 2 )) (2× cores) — system overloaded"
elif python3 -c "import sys; sys.exit(0 if float('$LOAD1') > $CORES else 1)" 2>/dev/null; then
  warn "Load elevated: $LOAD1 > $CORES cores"
else
  ok "Load normal: $LOAD1"
fi
free -h | awk '/Mem:/{gsub(/i/,"",$2);gsub(/i/,"",$3);gsub(/i/,"",$4);printf "  RAM: used=%s / total=%s  free=%s\n",$3,$2,$4}'
df -h / | awk 'NR==2{p=$5+0;m=sprintf("  Disk /: used=%s/%s (%s)",$3,$2,$5);
  if(p>=90)print "  \033[0;31m✗\033[0m "m" — CRITICAL";
  else if(p>=80)print "  \033[0;33m⚠\033[0m  "m" — WARNING";
  else print "  \033[0;32m✓\033[0m "m}'

# ── PARALLEL SECTIONS (A-F launched simultaneously) ────────────────────────────
SCRIPT_T0=$(date +%s%3N 2>/dev/null || date +%s)
TMP_A=$(mktemp); TMP_B=$(mktemp); TMP_C=$(mktemp)
TMP_D=$(mktemp); TMP_E=$(mktemp); TMP_F=$(mktemp)
trap 'rm -f "$TMP_A" "$TMP_B" "$TMP_C" "$TMP_D" "$TMP_E" "$TMP_F"' EXIT

# ── A: Critical infra containers ───────────────────────────────────────────────
# Single docker stats call + single docker inspect call instead of 7+7 sequential.
# Two HTTP health probes run in parallel within this section.
(
  CRIT_NAMES=(
    go-infrastructure-fetch-cache
    go_infrastructure_fetch_cache_redis
    go-js-proxy
    go-html-proxy
    go-js-proxy-network
    go-apikey-service
    go-url-categorizer-api
  )

  # One docker stats call for all 7 containers (~1.5s vs 10-14s sequential)
  STATS=$(docker stats --no-stream \
    --format $'{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}' \
    "${CRIT_NAMES[@]}" 2>/dev/null || true)

  # One docker inspect call for all 7 containers
  INSP=$(docker inspect \
    --format $'{{.Name}}\t{{.State.Status}}\t{{.State.Health.Status}}\t{{.RestartCount}}\t{{.State.StartedAt}}\t{{.HostConfig.Memory}}' \
    "${CRIT_NAMES[@]}" 2>/dev/null || true)

  # HTTP health probes in parallel (don't wait for each before starting next)
  _hf=$(mktemp); _hc=$(mktemp)
  curl -s -o/dev/null -w '%{http_code}' --max-time 4 http://localhost:18205/health >"$_hf" 2>/dev/null &
  curl -s -o/dev/null -w '%{http_code}' --max-time 4 http://localhost:18244/health >"$_hc" 2>/dev/null &
  wait
  FC_HTTP=$(cat "$_hf"); CAT_HTTP=$(cat "$_hc")
  rm -f "$_hf" "$_hc"

  sep "CRITICAL INFRASTRUCTURE CONTAINERS"
  echo "  ── fetch-cache + proxy backends ──"

  # One Python call formats all 7 containers — avoids 14+ python3 subprocess forks
  INSP_DATA="$INSP" STATS_DATA="$STATS" \
  python3 - "$FC_HTTP" "$CAT_HTTP" << 'PY'
import sys, os
from datetime import datetime, timezone

fc_http  = sys.argv[1] if len(sys.argv) > 1 else ''
cat_http = sys.argv[2] if len(sys.argv) > 2 else ''

G   = '\033[0;32m✓\033[0m'
Y   = '\033[0;33m⚠\033[0m '
R   = '\033[0;31m✗\033[0m'
YEL = '\033[0;33m'
NC  = '\033[0m'

# Parse inspect: one tab-separated line per container
insp = {}
for line in os.environ.get('INSP_DATA', '').strip().split('\n'):
    if not line.strip():
        continue
    parts = line.split('\t')
    if len(parts) < 6:
        continue
    name = parts[0].lstrip('/')
    insp[name] = {
        'status':   parts[1],
        'health':   parts[2] or 'n/a',
        'restarts': int(parts[3]) if parts[3].isdigit() else 0,
        'started':  parts[4],
        'memlim':   int(parts[5]) if parts[5].isdigit() else 0,
    }

# Parse stats: one tab-separated line per container
stats = {}
for line in os.environ.get('STATS_DATA', '').strip().split('\n'):
    if not line.strip():
        continue
    parts = line.split('\t')
    if len(parts) < 4:
        continue
    stats[parts[0]] = {'cpu': parts[1], 'mem': parts[2], 'pids': parts[3]}

now = datetime.now(timezone.utc)

def fmt_uptime(s):
    if s < 0:    return '?'
    if s < 60:   return f'{s}s'
    if s < 3600: return f'{s//60}m{s%60:02d}s'
    return f'{s//3600}h{(s%3600)//60}m'

def show(name, http=''):
    d = insp.get(name, {})
    s = stats.get(name, {})
    status   = d.get('status',   'missing')
    health   = d.get('health',   'n/a')
    restarts = d.get('restarts', 0)
    started  = d.get('started',  '')
    memlim   = d.get('memlim',   0)
    cpu      = s.get('cpu',      'n/a')
    mem      = s.get('mem',      'n/a')
    pids     = s.get('pids',     '?')

    memlim_hr = f'{memlim/1073741824:.1f}GB' if memlim > 0 else 'unlim'

    age_s  = -1
    uptime = '?'
    try:
        t     = started[:26].rstrip('Z') + '+00:00'
        age_s = int((now - datetime.fromisoformat(t)).total_seconds())
        uptime = fmt_uptime(age_s)
    except Exception:
        pass

    restart_flag = ''
    if restarts > 0 and 0 <= age_s < 600:
        restart_flag = f'  {YEL}[JUST RESTARTED — uptime {uptime}]{NC}'

    line = (f'{name}  cpu={cpu}  mem={mem} (limit={memlim_hr})'
            f'  pids={pids}  up={uptime}  restarts={restarts}  health={health}')

    if   status != 'running':   sym = R; line += f'  STATUS={status}'
    elif health == 'unhealthy': sym = R
    elif health == 'starting':  sym = Y
    else:                       sym = G

    print(f'  {sym} {line}{restart_flag}')

    if name == 'go-js-proxy':
        try:
            pnum = int(''.join(c for c in pids if c.isdigit()))
            if   pnum > 200:
                print(f'  {R}  go-js-proxy {pids} PIDs — Chrome explosion: docker restart go-js-proxy')
            elif pnum > 100:
                print(f'  {Y} go-js-proxy {pids} PIDs — Chrome leak building, watch closely')
        except Exception:
            pass

    if http:
        if http == '200':
            print(f'       HTTP /health → {http}')
        else:
            print(f'  {R}  HTTP /health → {http}')

show('go-infrastructure-fetch-cache', fc_http)
show('go_infrastructure_fetch_cache_redis')
show('go-js-proxy')
show('go-html-proxy')
show('go-js-proxy-network')
print('  ── auth + local categorizer ──')
show('go-apikey-service')
show('go-url-categorizer-api', cat_http)
PY
) > "$TMP_A" 2>&1 &
PID_A=$!

# ── B: Fetch cache quality + top callers (enricher flood detection) ─────────────
(
  sep "FETCH CACHE QUALITY (last 2 min)"
  fc_tmp=$(mktemp)
  ip_tmp=$(mktemp)

  # Fetch logs and build IP→service map in parallel
  docker logs go-infrastructure-fetch-cache --since 2m 2>&1 \
    | grep '"msg":"request_completed"' > "$fc_tmp" 2>/dev/null &
  PLOG=$!

  # IP→service map via one batched docker inspect over all running containers
  (
    CIDS=$(docker ps -q 2>/dev/null | tr '\n' ' ')
    if [[ -n "${CIDS// /}" ]]; then
      # shellcheck disable=SC2086
      docker inspect $CIDS \
        --format $'{{.Name}}\t{{range .NetworkSettings.Networks}}{{.IPAddress}},{{end}}' \
        2>/dev/null | sed 's|^/||' > "$ip_tmp" || true
    fi
  ) &
  PIPMAP=$!

  wait "$PLOG"   || true
  wait "$PIPMAP" || true

  if [[ ! -s "$fc_tmp" ]]; then
    warn "No fetch cache request_completed logs in last 2 min (idle or container restarting)"
  else
    python3 - "$fc_tmp" "$ip_tmp" << 'PY'
import sys, json, re, os
from collections import defaultdict

fc_file = sys.argv[1]
ip_file = sys.argv[2] if len(sys.argv) > 2 else ''

# Build IP → service name map
ip_map = {}
if ip_file:
    try:
        with open(ip_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) < 2:
                    continue
                name = parts[0]
                for ip in parts[1].split(','):
                    ip = ip.strip()
                    if ip:
                        ip_map[ip] = name
    except Exception:
        pass

total = s200 = s502 = s404 = other = 0
durations    = []
caller_counts = defaultdict(int)

with open(fc_file) as f:
    for line in f:
        if not line.strip():
            continue
        total += 1
        try:
            d   = json.loads(line)
            c   = d.get('status', 0)
            dur = d.get('duration', '')
            ip  = d.get('ip', '').rsplit(':', 1)[0]

            if   c == 200: s200 += 1
            elif c == 502: s502 += 1
            elif c == 404: s404 += 1
            else:          other += 1

            if dur:
                m = re.match(r'([\d.]+)', str(dur))
                if m:
                    durations.append(float(m.group(1)))

            if ip:
                caller_counts[ip] += 1
        except Exception:
            pass

p200 = s200 / total * 100 if total else 0
p502 = s502 / total * 100 if total else 0
avg  = sum(durations) / len(durations) if durations else 0
mxd  = max(durations) if durations else 0

if p200 >= 80:   sym, label = '\033[0;32m✓\033[0m', 'OK'
elif p200 >= 50: sym, label = '\033[0;33m⚠\033[0m ', 'DEGRADED'
else:            sym, label = '\033[0;31m✗\033[0m', 'CRITICAL — PROXY CHAIN DOWN'

print(f'  {sym} {total} reqs: 200={s200}({p200:.0f}%) 502={s502}({p502:.0f}%) 404={s404} other={other}')
print(f'       avg={avg:.2f}s  max={mxd:.2f}s  status={label}')
if p502 > 20:
    print(f'  \033[0;31m✗\033[0m  High 502 rate — js-proxy/html-proxy overloaded or crashed')
    print(f'       Fix: docker restart go-js-proxy go-html-proxy go-js-proxy-network')

if caller_counts:
    total_calls = sum(caller_counts.values())
    top = sorted(caller_counts.items(), key=lambda x: -x[1])[:10]
    print(f'\n  Top 10 callers (last 2min, {total_calls} total — these are enrichers, NOT processors):')
    for ip, n in top:
        pct   = n / total_calls * 100
        per_s = n / 120
        svc   = ip_map.get(ip, ip)
        print(f'       {n:6d} ({pct:4.1f}%, {per_s:.1f}/s)  {svc}')
    print()
    print(f'  NOTE: High request rate = enrichers retrying on 502s (retry storm).')
    print(f'        Breaking the cycle: stop domain processors to drain enricher queue.')
PY
  fi
  rm -f "$fc_tmp" "$ip_tmp"
) > "$TMP_B" 2>&1 &
PID_B=$!

# ── C: Categorizer on .30 (health + timed POST + stats, all in parallel) ────────
(
  sep "CATEGORIZER API (10.10.10.30:23481)"
  _hlt=$(mktemp); _prt=$(mktemp); _tms=$(mktemp); _stt=$(mktemp)

  # Health probe
  curl -s --max-time 5 http://10.10.10.30:23481/health >"$_hlt" 2>/dev/null &
  PHLT=$!

  # Timed POST probe (up to 35s — intentionally last because it's the slow one)
  (
    t0=$(date +%s%3N)
    curl -s --max-time 35 -X POST http://10.10.10.30:23481/api/v1/categorize \
      -H 'Content-Type: application/json' -d '{"url":"example.com"}' >"$_prt" 2>/dev/null || true
    echo $(( $(date +%s%3N) - t0 )) >"$_tms"
  ) &
  PPOST=$!

  # SSH stats on .30
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    root@10.10.10.30 \
    "docker stats domainscope-api --no-stream --format 'cpu={{.CPUPerc}} mem={{.MemUsage}} pids={{.PIDs}}'" \
    >"$_stt" 2>/dev/null &
  PSTT=$!

  wait "$PHLT"  || true
  wait "$PPOST" || true
  wait "$PSTT"  || true

  # Health result
  cat_health=$(python3 -c "
import sys, json
try:
    d = json.load(open('$_hlt'))
    print(d.get('data', {}).get('status', '?'))
except Exception:
    print('TIMEOUT')
" 2>/dev/null || echo "TIMEOUT")
  [[ "$cat_health" == "up" ]] && ok "GET /health → $cat_health" || err "GET /health → $cat_health"

  # POST probe result
  elapsed_ms=$(cat "$_tms" 2>/dev/null || echo "0")
  elapsed_s=$(python3 -c "print(f'{int(\"$elapsed_ms\")/1000:.1f}s')" 2>/dev/null || echo "?s")

  cat_result=$(python3 -c "
import sys, json
try:
    d = json.load(open('$_prt'))
    cat = d.get('data', {}).get('category', '?')
    ec  = d.get('error', {}).get('code', '')
    print(f'OK category={cat}' if not ec else f'ERROR {ec}')
except Exception:
    print('PARSE_ERROR/TIMEOUT')
" 2>/dev/null || echo "TIMEOUT")

  if [[ "$cat_result" == "OK"* ]]; then ok "POST /categorize → $cat_result  (${elapsed_s})"
  else                                    warn "POST /categorize → $cat_result  (${elapsed_s})"
  fi

  if python3 -c "import sys; sys.exit(0 if int('$elapsed_ms') > 25000 else 1)" 2>/dev/null; then
    err "  Response ${elapsed_s} > 25s — dangerously close to processor 30s timeout"
    note "  Fix: reduce NET_HEADER_TIMEOUT in categorizer compose (currently 30 → set to 15)"
  elif python3 -c "import sys; sys.exit(0 if int('$elapsed_ms') > 15000 else 1)" 2>/dev/null; then
    warn "  Response ${elapsed_s} elevated (>15s) — some domains will hit 30s timeout"
  fi

  note "Stats (.30): $(cat "$_stt" 2>/dev/null || echo 'ssh_unavailable')"
  rm -f "$_hlt" "$_prt" "$_tms" "$_stt"
) > "$TMP_C" 2>&1 &
PID_C=$!

# ── D: Domain processors (all 4 log reads launched in parallel) ─────────────────
(
  sep "DOMAIN PROCESSORS (load generators)"

  declare -A PROC_STATUS PROC_CONC PROC_FAILS PROC_OK
  SUFFIXES=(a b c d)
  PS_TMPS=()
  PL_TMPS=()
  NAMES_LIST=()

  for suffix in "${SUFFIXES[@]}"; do
    name="go-domainscope-com-${suffix}-13-icann-domains-domain-processor-1"
    _ps=$(mktemp); _pl=$(mktemp)
    PS_TMPS+=("$_ps")
    PL_TMPS+=("$_pl")
    NAMES_LIST+=("$name")

    docker inspect "$name" \
      --format $'{{.State.Status}}\t{{range .Config.Env}}{{.}}\n{{end}}' \
      >"$_ps" 2>/dev/null &
    docker logs "$name" --since 5m >"$_pl" 2>&1 &
  done
  wait  # all 8 background jobs done

  for i in "${!SUFFIXES[@]}"; do
    name="${NAMES_LIST[$i]}"
    _ps="${PS_TMPS[$i]}"
    _pl="${PL_TMPS[$i]}"

    status=$(head -1 "$_ps" 2>/dev/null | cut -f1)
    conc=$(grep '^CONCURRENCY=' "$_ps" 2>/dev/null | cut -d= -f2 | head -1 || echo "")
    fails=$(grep -c "Failed after" "$_pl" 2>/dev/null || echo "0")
    oks=$(grep -c "Successfully" "$_pl" 2>/dev/null || echo "0")

    PROC_STATUS[$name]="${status:-missing}"
    PROC_CONC[$name]="${conc:-?}"
    PROC_FAILS[$name]="${fails:-0}"
    PROC_OK[$name]="${oks:-0}"
    rm -f "$_ps" "$_pl"
  done

  running=0
  for suffix in "${SUFFIXES[@]}"; do
    name="go-domainscope-com-${suffix}-13-icann-domains-domain-processor-1"
    status="${PROC_STATUS[$name]:-missing}"
    if [[ "$status" == "running" ]]; then
      warn "$name: RUNNING  concurrency=${PROC_CONC[$name]}  ok=${PROC_OK[$name]}/5min  fail=${PROC_FAILS[$name]}/5min"
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
) > "$TMP_D" 2>&1 &
PID_D=$!

# ── E: Upstream enricher errors from categorizer logs ─────────────────────────
(
  sep "UPSTREAM ENRICHER ERRORS (last 5 min, from .30 categorizer)"
  cat_tmp=$(mktemp)
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    root@10.10.10.30 "docker logs domainscope-api --since 5m 2>&1" >"$cat_tmp" 2>/dev/null || true

  if [[ ! -s "$cat_tmp" ]]; then
    warn "Cannot reach 10.10.10.30 — run from bastion for this section"
    note "  ssh root@0docker.com 'ssh root@10.10.10.30 docker logs domainscope-api --since 5m'"
  else
    python3 - "$cat_tmp" << 'PY'
import sys, json
from collections import defaultdict

proxy_timeout = defaultdict(int)
not_found     = defaultdict(int)
bad_request   = defaultdict(int)
other_errors  = defaultdict(int)

with open(sys.argv[1]) as f:
    for line in f:
        if not line.strip():
            continue
        try:
            d   = json.loads(line)
            err = d.get('error', '')
            svc = d.get('service', 'unknown')
            if not err:
                continue
            if 'proxy: all providers failed' in err or 'timeout awaiting response headers' in err:
                proxy_timeout[svc] += 1
            elif 'HTTP 404' in err:
                not_found[svc] += 1
            elif 'HTTP 400' in err:
                bad_request[svc] += 1
            elif err:
                other_errors[svc] += 1
        except Exception:
            pass

pt = sum(proxy_timeout.values())
nf = sum(not_found.values())
br = sum(bad_request.values())
oe = sum(other_errors.values())

G = '\033[0;32m✓\033[0m'
R = '\033[0;31m✗\033[0m'
Y = '\033[0;33m⚠\033[0m '

if pt == 0:
    print(f'  {G}  No proxy-timeout errors — fetch-cache chain healthy')
else:
    print(f'  {R}  {pt} proxy-timeout errors (fetch-cache/proxy chain overloaded):')
    for svc, cnt in sorted(proxy_timeout.items(), key=lambda x: -x[1])[:10]:
        print(f'       {cnt:5d}× {svc}')

if bad_request:
    print(f'  {Y} {br} HTTP 400 errors (possible enricher API bug):')
    for svc, cnt in sorted(bad_request.items(), key=lambda x: -x[1])[:5]:
        print(f'       {cnt:5d}× {svc}')

if other_errors:
    print(f'  {Y} {oe} other upstream errors:')
    for svc, cnt in sorted(other_errors.items(), key=lambda x: -x[1])[:5]:
        print(f'       {cnt:5d}× {svc}')

if nf > 0:
    print(f'       {nf} HTTP 404s ({len(not_found)} enrichers) — domain data absent, not a bug')
PY
  fi
  rm -f "$cat_tmp"
) > "$TMP_E" 2>&1 &
PID_E=$!

# ── F: Nginx connection-refused count ─────────────────────────────────────────
(
  sep "NGINX (webgateway 10.10.10.10)"
  nginx_out=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    root@10.10.10.10 \
    "tail -n 500 /var/log/nginx/domainscope.scrapetheworld.org.error.log \
     | grep -c 'Connection refused'" 2>/dev/null || echo "ssh_unavailable")
  if [[ "$nginx_out" == "ssh_unavailable" ]]; then
    warn "Cannot SSH to webgateway — run from bastion"
  else
    [[ "$nginx_out" -gt 50 ]] && \
      err  "Nginx: $nginx_out conn-refused errors in last 500 lines — upstream DOWN" || \
      ok   "Nginx: $nginx_out conn-refused errors in last 500 lines"
  fi
) > "$TMP_F" 2>&1 &
PID_F=$!

# ── COLLECT AND PRINT IN ORDER ─────────────────────────────────────────────────
wait "$PID_A"; cat "$TMP_A"
wait "$PID_B"; cat "$TMP_B"
wait "$PID_C"; cat "$TMP_C"
wait "$PID_D"; cat "$TMP_D"
wait "$PID_E"; cat "$TMP_E"
wait "$PID_F"; cat "$TMP_F"

SCRIPT_T1=$(date +%s%3N 2>/dev/null || date +%s)
ELAPSED=$(( SCRIPT_T1 - SCRIPT_T0 ))

sep "DONE — $(date -u)"
echo ""
echo "  Elapsed: ${ELAPSED}ms"
echo ""
echo "  Quick remediation:"
echo "  # Break retry storm (stop processors):    docker stop go-domainscope-com-{a,b,c,d}-13-icann-domains-domain-processor-1"
echo "  # Restart js-proxy (kills Chrome leaks):  docker restart go-js-proxy"
echo "  # Restart html-proxy + network:           docker restart go-html-proxy go-js-proxy-network"
echo "  # Restart fetch cache (clears memory):    docker restart go-infrastructure-fetch-cache"
echo "  # Restart categorizer on .30:             ssh root@10.10.10.30 'docker restart domainscope-api'"
echo "  # Disk cleanup (docker):                  docker system prune -f --volumes"
