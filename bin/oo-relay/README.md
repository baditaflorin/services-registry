# oo-relay — read-only log relay for the fleet OpenObserve

OpenObserve OSS has no read-only role: every user it knows is an admin. The root
password therefore lives on VM 630 (`0own-openobserve`) only, and this relay is
the one process there that holds it. Agents and operators get a narrow,
token-gated read path instead of the password, and `bin/oo` can use it
(`OO_RELAY_URL`, see `docs/operations/openobserve-log-relay.md`).

It is the generic sibling of the fixed-query CrUX relay
(`go-url-categorizer-api/src/cmd/crux-openobserve-relay`) and follows its pattern.

## What a caller can do

One call: `POST /v1/logs`, `Authorization: Bearer <relay token>`, JSON body:

| field | rule |
|---|---|
| `container_name` | required, exact match, `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}` |
| `host` | optional, exact match, same charset |
| `contains` | optional substring of the message, case-sensitive, at most 256 bytes, no control characters |
| `start` | required, RFC3339 |
| `end` | optional, RFC3339, default now, not in the future |
| `limit` | optional, 1 to 500, default 100 |
| `order` | optional, `asc` (default) or `desc` |

`end - start` is at most 7 days. Any other field (`sql`, `stream`, `org`, ...) is
rejected. The stream is fixed to `docker_logs`, the organization to `default`.

Response (`200`): `{"status":"ok","stream":"docker_logs","count":N,"limit":L,"truncated":bool,"next_start"|"next_end":...,"rows":[{"timestamp","timestamp_unix_micros","container_name","host","message","message_truncated"?}]}`.
Nothing else is returned. When `truncated` is true there may be more rows: ask
again with `start` (ascending) or `end` (descending) set to the hint; the row at
that exact microsecond repeats.

Errors always carry a `status` and never look like an empty result:
`401` unauthorized, `400` `invalid_request`, `429` `busy` (more than 4 concurrent
queries), `502` `unavailable` (OpenObserve did not answer; this is not zero rows).

There are no write endpoints, no SQL input, no stream or organization choice.

## How the query is built

`query.go` renders exactly one SQL shape:

```
SELECT _timestamp, container_name, host, message FROM docker_logs
WHERE container_name = '<name>' [AND host = '<host>'] [AND message LIKE '%<contains>%']
ORDER BY _timestamp ASC|DESC LIMIT <n>
```

OpenObserve's search API takes SQL text with no bound parameters, so the values
are made safe by construction instead:

- `container_name` and `host` are checked against a charset that has no quote,
  backslash, space or SQL punctuation, so they cannot change the statement.
- `contains` has control characters and bad UTF-8 rejected, single quotes
  doubled, and `%`, `_`, `\` turned into the one-character wildcard `_`. No
  backslash reaches the SQL. The SQL match can then only be looser than the
  exact substring, and the relay re-checks every returned row for the exact
  substring and drops the rest. (A very loose pattern can therefore return
  fewer than `limit` rows while `truncated` is true; page on.)
- the time range goes in as integers, `limit` as a checked integer, `order` as
  one of two words.

Returned messages are masked for known credential shapes (`ak_`, `li_`, `fb_`,
`ghp_`/`github_pat_`, `tskey-`, JWTs, `Bearer`/`Basic` values, `password=`-style
pairs, `user:pass@` URLs) and cut at 8 KB. This is a second line of defence;
logs are still stored as written, and `contains` is matched before masking.

## Auth and secrets

- The bearer token file holds one to eight lines, each 32 random bytes as 64 hex
  characters (several lines allow a rotation overlap). It is never the
  OpenObserve password; the relay refuses to start if they match.
- The root password is mounted read-only into this container from the VM 630
  file it already lives in. It is used only to call `POST /api/default/_search`.
- Neither value is logged. The access log has container, host, the length of
  `contains`, the time range, limit, row count and the caller's address.

## Run and test

```sh
cd bin/oo-relay
go vet ./... && go test ./...        # stdlib only; also runs in Woodpecker (relay-tests)
```

Deploy: `Dockerfile` and `compose.yaml` in this directory; runbook in
`docs/operations/openobserve-log-relay.md`.
