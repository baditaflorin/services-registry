#!/usr/bin/env python3
"""sync-service-params: put each service's declared inputs and a working example address into overrides.json.

Why. The hub builds "Open" and "Run example" links from the registry's `example_path`, and for most services that
is only the category default (/?url=https://example.com), while the service's own service.yaml says what it needs:
`api.params` (name, required, type, description) and `test.url`. On 2026-10-29, 161 of the 251 services that declare
a required param had an example that lacks one, so the link answered a 400. This script reads the facts file that
bin/extract-service-facts.py wrote from the clones and the registry, and edits overrides.json (the hand-curated
source; services.json and the slices are generated from it by bin/generate.py).

For each service that has a facts row and its own block in overrides.json:

  params           the declared inputs: name, required, type, short description, example when known (from
                   service.yaml, else the value the final example_path sends for it; never a secret-shaped one).
  example_path     decided by the first rule that applies, looking at the required params a link can carry (all
                   but the {placeholders} of the endpoint's path):
    (a) keep       the current example_path already carries every one of them (or there are none): never replaced.
    (b) test-url   else the service's own test.url, sanitised, when it carries all of them. Sanitising: a leading
                   /t/<token> prefix and the service's own directory segment (the old path-routed form) are
                   stripped, every api_key query parameter is removed (the hub adds the key at click time; a key
                   must never be stored in the registry), anything left that still holds a {placeholder}, a
                   secret-shaped value or a credential-named parameter is refused. A test.url that is only a
                   health path (/health, /selftest, ...) is not an example.
    (c) params     else a path built from api.endpoint and the declared params: the param's example, else its
                   default, else the fixed safe table below (domain=example.com, url=https://example.com, ...).
                   The entry then carries `"example_path_source": "derived-from-params"` so it stays visible.
                   Never for a service whose api.method is POST without GET: its params are a request body.
    (d) untouched  else the example is left as it is and the service is REPORTED with the reason (a required param
                   with no safe example, a credential-named param, an endpoint that is a template, ...).
  A service without a facts row, without its own block in overrides.json (the $expand and $external rows) or whose
  repository several registry entries share is never touched, and no required param is ever invented: `required`
  is copied from service.yaml.

Idempotent: a second run over the result changes nothing. Dry run by default (a summary: counts per rule, the services
left untouched and why); --apply writes overrides.json in place, every byte outside the changed entries kept.
Run `python3 bin/generate.py --only <ids>` afterwards (the summary prints the ids) to regenerate services.json and
the slices.

    python3 bin/extract-service-facts.py --workspace /root/workspace --out /tmp/service-facts.json   # on the builder
    python3 bin/sync-service-params.py --facts /tmp/service-facts.json            # dry run
    python3 bin/sync-service-params.py --facts /tmp/service-facts.json --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DERIVED = "derived-from-params"
DESCRIPTION_MAX = 100

# What a click may send for a required param that has no `example`/`default` in service.yaml: documented, public,
# harmless values only. A name that is not here is NOT guessed; the service is reported instead.
SAFE_EXAMPLES = {
    "domain": "example.com",
    "target": "example.com",
    "url": "https://example.com",
    "host": "example.com",
    "hostname": "example.com",
    "apex": "example.com",
    "root": "example.com",
    "q": "anthropic claude",
    "query": "anthropic claude",
    "ip": "8.8.8.8",
}

# test.url that only probes the process is not an example of the service's work.
HEALTH_PATHS = frozenset({"/health", "/healthz", "/selftest", "/_gw_health", "/ready", "/readyz", "/live", "/livez",
                          "/version", "/ping", "/metrics"})

# Query parameters carrying the caller's key: removed from a test.url, never stored.
KEY_PARAMS = frozenset({"api_key", "apikey", "x-api-key", "x_api_key"})
# A param with one of these names never gets an invented or copied example value.
CREDENTIAL_NAMES = frozenset({"api_key", "apikey", "secret", "password", "passwd", "client_secret", "access_token",
                              "authorization", "private_key"})
SECRET_VALUE = re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*|\bsk_(?:live|test)_|\bfb_[a-z0-9]{20,}|\b[a-f0-9]{32,}\b|"
                          r"-----BEGIN|\bgh[pousr]_\w{20,}|\bAKIA[0-9A-Z]{16}\b")
BODY_REASON = "its params travel in the request body"
PLACEHOLDER = re.compile(r"[{}]")


# -- pure helpers (unit-tested) ------------------------------------------------------------------------------------

def norm(name):
    return re.sub(r"[-_]", "", (name or "").lower())


def is_secretish(name, value):
    """True when a param name or its value must not be copied into the registry."""
    return (name or "").lower() in CREDENTIAL_NAMES or bool(SECRET_VALUE.search(str(value)))


def query_of(path):
    """{name: value} of the query of a path (the last of a repeated name)."""
    return {k: v for k, v in parse_qsl(path.partition("?")[2], keep_blank_values=True)}


def carries(path, names):
    """True when the path's query has every name with a non-empty value."""
    q = query_of(path)
    return all(q.get(n) for n in names)


def sanitise_path(raw, repo=None):
    """A test.url / endpoint as a registry example path, or None when it cannot be one.

    Strips a leading /t/<token> prefix and then the service's own directory segment (`/go_x`, `/go-x`), removes
    api_key parameters, keeps everything else (path, other parameters, their order and raw encoding)."""
    raw = (raw or "").strip()
    if not raw.startswith("/") or raw.startswith("//") or any(c.isspace() for c in raw):
        return None
    path, sep, query = raw.partition("?")
    m = re.match(r"/t/[^/]*(/.*)?$", path)
    if m:
        path = m.group(1) or "/"
    if repo:
        m = re.match(r"/([^/]+)(/.*)?$", path)
        if m and norm(m.group(1)) == norm(repo):
            path = m.group(2) or "/"
    kept = [pair for pair in query.split("&") if pair and pair.partition("=")[0].lower() not in KEY_PARAMS]
    return path + ("?" + "&".join(kept) if kept else "")


def encode_value(value):
    """A param value for a query string: readable, still valid (space becomes %20, `:` `/` `@` `,` stay)."""
    out = []
    for ch in str(value):
        if ch.isalnum() and ch.isascii() or ch in "-._~:/@,":
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def scalar(value):
    """A param example/default as the text a query carries, or None when it is not a plain scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def endpoint_path(facts):
    """The path of api.endpoint without its query (`/?api_key={token}` is `/`) and without a /t/{token} prefix;
    None when there is no endpoint or it is not a plain path."""
    endpoint = (facts.get("api") or {}).get("endpoint")
    if not endpoint:
        return None
    path = sanitise_path(endpoint, facts.get("repo"))
    return None if path is None else path.partition("?")[0]


def path_params(facts):
    """Names of the declared params that are {placeholders} of the endpoint's path, not query params."""
    in_path = set(re.findall(r"\{([^{}]+)\}", endpoint_path(facts) or ""))
    return [p["name"] for p in facts.get("params") or [] if p["name"] in in_path]


def post_only(facts):
    """True when api.method names methods and GET is not one of them."""
    methods = [m.strip() for m in ((facts.get("api") or {}).get("method") or "").upper().split(",") if m.strip()]
    return bool(methods) and "GET" not in methods


def required_query(facts):
    """The required params a link has to carry in its query string (path placeholders are not among them)."""
    in_path = set(path_params(facts))
    return [p for p in facts.get("params") or [] if p.get("required") and p["name"] not in in_path]


def param_value(param):
    """(value, source) for a param: its own example, else its default, else the fixed table; (None, why) if none."""
    if is_secretish(param["name"], ""):
        return None, f"required param {param['name']!r} is a credential: no example is stored"
    for key in ("example", "default"):
        v = scalar(param.get(key))
        if v is not None:
            if is_secretish(param["name"], v):
                return None, f"required param {param['name']!r} has a secret-shaped {key}: no example is stored"
            return v, key
    if param["name"] in SAFE_EXAMPLES:
        return SAFE_EXAMPLES[param["name"]], "table"
    return None, f"required param {param['name']!r} has no example, default or safe table value"


def from_test_url(facts, required):
    """Rule (b): (path, None) or (None, reason)."""
    raw = facts.get("test_url") or ""
    if not raw:
        return None, "no test.url"
    path = sanitise_path(raw, facts.get("repo"))
    if path is None:
        return None, "test.url is not a plain path"
    if path.partition("?")[0].rstrip("/") in HEALTH_PATHS:
        return None, f"test.url is only a health path ({path.partition('?')[0]})"
    if PLACEHOLDER.search(path):
        return None, "test.url still holds a {placeholder}"
    q = query_of(path)
    for k, v in q.items():
        if is_secretish(k, v):
            return None, f"test.url carries a secret-shaped or credential param ({k})"
    if not carries(path, [p["name"] for p in required]):
        return None, "test.url lacks a required param"
    return path, None


def from_params(facts, required):
    """Rule (c): (path, None) or (None, reason): api.endpoint plus the required query params with safe values."""
    if post_only(facts):
        return None, f"api.method is {facts['api']['method']}: {BODY_REASON}, a link cannot carry them"
    by_name = {p["name"]: p for p in facts.get("params") or []}
    path = endpoint_path(facts)
    if path is None:
        if (facts.get("api") or {}).get("endpoint"):
            return None, "api.endpoint is not a plain path"
        path = "/"
    for name in re.findall(r"\{([^{}]+)\}", path):
        value, why = param_value(by_name[name]) if name in by_name else (None, f"endpoint placeholder {{{name}}} is not a declared param")
        if value is None:
            return None, why
        path = path.replace("{%s}" % name, encode_value(value))
    if PLACEHOLDER.search(path):
        return None, "api.endpoint is a template this script cannot fill"
    pairs = []
    for p in required:
        value, why = param_value(p)
        if value is None:
            return None, why
        pairs.append(f"{p['name']}={encode_value(value)}")
    return path + ("?" + "&".join(pairs) if pairs else ""), None


def decide(facts, current):
    """What to do with one service's example_path.

    Returns {"rule": a|b|c|d, "example_path": new path or None (keep), "source": marker or None, "reason": str}."""
    required = required_query(facts)
    names = [p["name"] for p in required]
    if carries(current or "", names):
        return {"rule": "a", "example_path": None, "source": None,
                "reason": "already carries every required query param" if names else "no required query param"}
    path, why_b = from_test_url(facts, required)
    if path is not None:
        return {"rule": "b", "example_path": path, "source": None, "reason": "from the service's own test.url"}
    path, why_c = from_params(facts, required)
    if path is not None:
        return {"rule": "c", "example_path": path, "source": SOURCE_DERIVED, "reason": f"derived from params (test.url: {why_b})"}
    return {"rule": "d", "example_path": None, "source": None, "reason": f"{why_c}; test.url: {why_b}"}


def short_description(text, limit=DESCRIPTION_MAX):
    if not text or len(text) <= limit:
        return text
    cut = text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return (cut or text[: limit - 3]) + "..."


def registry_params(facts, example_path):
    """The `params` value for overrides.json. An example comes from service.yaml, else from the query of the final
    example_path (a value the registry itself sends), and never when it is secret-shaped."""
    shown = query_of(example_path or "")
    out = []
    for p in facts.get("params") or []:
        row = {"name": p["name"], "required": bool(p.get("required"))}
        if p.get("type"):
            row["type"] = p["type"]
        desc = short_description(p.get("description"))
        if desc:
            row["description"] = desc
        example = scalar(p.get("example"))
        if example is None:
            example = shown.get(p["name"]) or None
        if example is not None and not is_secretish(p["name"], example):
            row["example"] = example
        out.append(row)
    return out


# -- editing overrides.json in place -------------------------------------------------------------------------------

def dump_params(params):
    inner = ",\n".join("      " + json.dumps(p, ensure_ascii=False) for p in params)
    return "[\n" + inner + "\n    ]"


def dump_value(key, value):
    return dump_params(value) if key == "params" else json.dumps(value, ensure_ascii=False)


def set_fields(text, changes):
    """overrides.json text with fields set per id, every other byte kept.

    changes: {id: {key: value, ...}} with key in example_path / example_path_source / params; a value of None
    removes the key. The block is located with the JSON decoder (some blocks close on the same line as their last
    key); an existing top-level line of the block is rewritten in place, a new key goes first in the block. The parsed
    result must differ from the input only in those keys of those blocks."""
    before = json.loads(text)
    for sid in sorted(changes):
        head = f'\n  "{sid}": {{'
        if text.count(head) != 1:
            raise ValueError(f"{sid}: expected one top-level block, found {text.count(head)}")
        brace = text.index(head) + len(head) - 1
        _, end = json.JSONDecoder().raw_decode(text, brace)
        block = text[brace:end]
        for key in ("params", "example_path", "example_path_source"):   # a new key goes first; the source follows example_path
            if key not in changes[sid]:
                continue
            value = changes[sid][key]
            lines = list(re.finditer(rf'\n    "{key}": ', block))
            if len(lines) > 1:
                raise ValueError(f"{sid}: more than one {key} line")
            if lines:
                start = lines[0].end()
                _, vend = json.JSONDecoder().raw_decode(block, start)
                if value is None:                                   # drop the whole line and keep the commas right
                    before_line, after_value = block[:lines[0].start()], block[vend:]
                    if after_value.startswith(","):
                        after_value = after_value[1:]
                    elif before_line.endswith(","):                 # it was the last key: the previous line loses its comma
                        before_line = before_line[:-1]
                    block = before_line + after_value
                else:
                    block = block[:start] + dump_value(key, value) + block[vend:]
            elif value is not None:
                new_line = f'    "{key}": {dump_value(key, value)},' + "\n"
                anchor = re.search(r'\n    "example_path": ', block) if key == "example_path_source" else None
                if anchor:                                          # right after the end of the example_path line
                    _, vend = json.JSONDecoder().raw_decode(block, anchor.end())
                    eol = block.index("\n", vend)
                    if block[vend:eol] != ",":
                        raise ValueError(f"{sid}: unexpected example_path layout")
                    block = block[:eol + 1] + new_line + block[eol + 1:]
                else:
                    if not block.startswith("{\n"):
                        raise ValueError(f"{sid}: unexpected block layout")
                    block = "{\n" + new_line + block[2:]
        text = text[:brace] + block + text[end:]
    after = json.loads(text)
    for sid in set(before) | set(after):
        a, b = before.get(sid), after.get(sid)
        if sid in changes:
            want = {k: v for k, v in changes[sid].items() if v is not None}
            drop = {k for k, v in changes[sid].items() if v is None}
            rest_a = {k: v for k, v in a.items() if k not in changes[sid]}
            rest_b = {k: v for k, v in b.items() if k not in changes[sid]}
            if rest_a != rest_b or any(b.get(k) != v for k, v in want.items()) or any(k in b for k in drop):
                raise ValueError(f"{sid}: edit changed more than {sorted(changes[sid])}")
        elif a != b:
            raise ValueError(f"{sid}: untouched block changed")
    return text


# -- plan ----------------------------------------------------------------------------------------------------------

def plan(facts_rows, services, overrides):
    """One row per facts row: {id, rule, reason, changes (dict for set_fields, may be empty), ...}."""
    by_id = {s["id"]: s for s in services}
    rows = []
    for facts in sorted(facts_rows, key=lambda r: r["id"]):
        sid = facts["id"]
        row = {"id": sid, "rule": None, "reason": "", "changes": {}}
        rows.append(row)
        entry = by_id.get(sid)
        block = overrides.get(sid)
        if entry is None:
            row.update(rule="skip", reason="not in the registry")
            continue
        if not isinstance(block, dict):
            row.update(rule="skip", reason="no per-slug block in overrides.json ($expand / $external / generated only)")
            continue
        current = entry.get("example_path") or ""
        d = decide(facts, current)
        row.update(rule=d["rule"], reason=d["reason"])
        final = d["example_path"] if d["example_path"] is not None else current
        changes = {}
        if d["example_path"] is not None and d["example_path"] != block.get("example_path"):
            changes["example_path"] = d["example_path"]
        if d["rule"] in ("b", "c"):
            if d["source"] != block.get("example_path_source"):
                changes["example_path_source"] = d["source"]
        params = registry_params(facts, final)
        if params and params != block.get("params"):
            changes["params"] = params
        row["changes"] = changes
        declared = {p["name"] for p in facts.get("params") or []}
        row["unknown_query"] = d["rule"] == "a" and bool(declared) and bool(set(query_of(current)) - declared)
        row["legacy_prefix"] = d["rule"] == "a" and bool(re.match(r"/([^/?]+)", current)) and \
            norm(re.match(r"/([^/?]+)", current).group(1)) == norm(facts.get("repo"))
    return rows


def summarise(rows):
    out = {"rules": {}, "changed": [], "untouched_d": [], "skipped": [], "legacy_prefix": [], "params_only": [],
           "unknown_query": []}
    for r in rows:
        out["rules"][r["rule"]] = out["rules"].get(r["rule"], 0) + 1
        if r["changes"]:
            out["changed"].append(r["id"])
            if "example_path" not in r["changes"]:
                out["params_only"].append(r["id"])
        if r["rule"] == "d":
            out["untouched_d"].append((r["id"], r["reason"]))
        if r["rule"] == "skip":
            out["skipped"].append((r["id"], r["reason"]))
        if r.get("legacy_prefix"):
            out["legacy_prefix"].append(r["id"])
        if r.get("unknown_query"):
            out["unknown_query"].append(r["id"])
    return out


def print_summary(rows, out=sys.stdout):
    s = summarise(rows)
    n_path = sum(1 for r in rows if "example_path" in r["changes"])
    n_params = sum(1 for r in rows if "params" in r["changes"])
    p = lambda *a: print(*a, file=out)
    p(f"services with a facts row: {len(rows)}")
    p(f"  (a) example already carries every required query param (or none is required): {s['rules'].get('a', 0)}")
    p(f"  (b) example_path replaced by the sanitised test.url:                            {s['rules'].get('b', 0)}")
    p(f"  (c) example_path derived from params (example_path_source set):                 {s['rules'].get('c', 0)}")
    p(f"  (d) left untouched, reported below:                                              {s['rules'].get('d', 0)}")
    p(f"  skipped, not touchable:                                                          {s['rules'].get('skip', 0)}")
    p(f"entries to change: {len(s['changed'])} ({n_path} example_path, {n_params} params; {len(s['params_only'])} params only)")
    body = [sid for sid, why in s["untouched_d"] if BODY_REASON in why]
    if body:
        p(f"  untouched, POST-only (params travel in the request body, a link cannot carry them): {len(body)}: {', '.join(body)}")
    for sid, why in s["untouched_d"]:
        if BODY_REASON not in why:
            p(f"  untouched {sid}: {why}")
    for sid, why in s["skipped"]:
        p(f"  skipped {sid}: {why}")
    if s["legacy_prefix"]:
        p(f"kept under (a) although the path starts with the service's own directory name ({len(s['legacy_prefix'])}): "
          + ", ".join(s["legacy_prefix"]))
    if s["unknown_query"]:
        p(f"kept under (a) although its query names none of the declared params ({len(s['unknown_query'])}; --json lists them): "
          + ", ".join(s["unknown_query"][:15]) + (", ..." if len(s["unknown_query"]) > 15 else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--facts", required=True, help="facts file written by bin/extract-service-facts.py")
    ap.add_argument("--registry", default=str(ROOT / "services.json"))
    ap.add_argument("--overrides", default=str(ROOT / "overrides.json"))
    ap.add_argument("--apply", action="store_true", help="write overrides.json (default: dry run)")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON instead of the summary")
    args = ap.parse_args(argv)

    facts = json.loads(Path(args.facts).read_text(encoding="utf-8"))
    if facts.get("schema") != 1:
        sys.exit(f"unsupported facts schema {facts.get('schema')!r}")
    services = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    overrides_path = Path(args.overrides)
    text = overrides_path.read_text(encoding="utf-8")
    rows = plan(facts["services"], services, json.loads(text))
    if args.json:
        json.dump(rows, sys.stdout, indent=1, ensure_ascii=False)
        print()
    else:
        print_summary(rows)
    changes = {r["id"]: r["changes"] for r in rows if r["changes"]}
    if not args.apply:
        print(f"dry run: {len(changes)} entries would change; --apply writes overrides.json", file=sys.stderr)
        return 0
    if not changes:
        print("nothing to change", file=sys.stderr)
        return 0
    overrides_path.write_text(set_fields(text, changes), encoding="utf-8")
    print(f"overrides.json: {len(changes)} entries changed; now run: python3 bin/generate.py --only "
          + ",".join(sorted(changes)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
