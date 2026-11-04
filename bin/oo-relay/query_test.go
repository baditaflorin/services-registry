package main

import (
	"errors"
	"strings"
	"testing"
	"time"
)

var fixedNow = time.Date(2026, 11, 4, 9, 0, 0, 0, time.UTC)

func base() logRequest {
	return logRequest{ContainerName: "domainscope-api", Start: "2026-11-03T12:00:00Z", End: "2026-11-03T18:30:00Z"}
}

func TestBuildQueryAcceptanceShape(t *testing.T) {
	req := base()
	req.Host = "0docker_docker_prod"
	req.Contains = "[ext-traits-backfill]"
	q, err := buildQuery(req, fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	want := "SELECT _timestamp, container_name, host, message FROM docker_logs WHERE container_name = 'domainscope-api' AND host = '0docker_docker_prod' AND message LIKE '%[ext-traits-backfill]%' ORDER BY _timestamp ASC LIMIT 100"
	if q.SQL != want {
		t.Fatalf("sql mismatch\n got: %s\nwant: %s", q.SQL, want)
	}
	if q.StartMicros != time.Date(2026, 11, 3, 12, 0, 0, 0, time.UTC).UnixMicro() || q.EndMicros != time.Date(2026, 11, 3, 18, 30, 0, 0, time.UTC).UnixMicro() {
		t.Fatalf("bad range %d..%d", q.StartMicros, q.EndMicros)
	}
	if q.Limit != 100 {
		t.Fatalf("default limit = %d", q.Limit)
	}
}

func TestNamesRejectInjection(t *testing.T) {
	bad := []string{
		"", "a b", "a'b", "a' OR '1'='1", "a'; DROP TABLE docker_logs;--", "a--b'", "a/*", "a\\", "a\nb", "a\x00",
		"a;b", "a\"b", "a)", "-leading-dash", ".dot", "a%b", "ä", strings.Repeat("a", 129),
	}
	for _, v := range bad {
		req := base()
		req.ContainerName = v
		if _, err := buildQuery(req, fixedNow); !errors.Is(err, errInvalid) {
			t.Errorf("container_name %q accepted", v)
		}
		req = base()
		req.Host = v
		if v == "" {
			continue // host is optional
		}
		if _, err := buildQuery(req, fixedNow); !errors.Is(err, errInvalid) {
			t.Errorf("host %q accepted", v)
		}
	}
}

func TestContainsIsEscaped(t *testing.T) {
	cases := map[string]string{
		"plain":                 "plain",
		"it's":                  "it''s",
		"'; DROP TABLE x; --":   "''; DROP TABLE x; --", // quote doubled, so still inside the literal
		"100%":                  "100_",
		"a_b":                   "a_b",
		`back\slash`:            "back_slash",
		`\'`:                    "_''",
		`\' OR 1=1 --`:          "_'' OR 1=1 --",
		"[ext-traits-backfill]": "[ext-traits-backfill]",
		"héllo":                 "héllo",
	}
	for in, want := range cases {
		got, err := likePattern(in)
		if err != nil || got != want {
			t.Errorf("likePattern(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
}

// Whatever the caller sends, the literal opened by "LIKE '%" must stay open
// until the closing "%'": every quote inside must be doubled, and no backslash
// may remain.
func TestContainsCannotCloseTheLiteral(t *testing.T) {
	for _, in := range []string{"'", "''", "'''", `\'`, `\\'`, "a'b'c", "'; SELECT 1 --", "%' OR '%'='", `x\`, "'’"} {
		p, err := likePattern(in)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(p, `\`) {
			t.Errorf("backslash survived for %q: %q", in, p)
		}
		if strings.Contains(strings.ReplaceAll(p, "''", ""), "'") {
			t.Errorf("unpaired quote for %q: %q", in, p)
		}
	}
}

func TestContainsRejects(t *testing.T) {
	for _, in := range []string{"a\nb", "a\rb", "a\x00b", "a\tb", "a\x7fb", "\xff\xfe", strings.Repeat("x", maxContainsLen+1)} {
		if _, err := likePattern(in); !errors.Is(err, errInvalid) {
			t.Errorf("contains %q accepted", in)
		}
	}
	if _, err := likePattern(strings.Repeat("x", maxContainsLen)); err != nil {
		t.Errorf("max-length contains rejected: %v", err)
	}
}

func TestTimeRangeCaps(t *testing.T) {
	cases := []struct {
		name       string
		start, end string
		ok         bool
	}{
		{"exactly 7d", "2026-10-28T00:00:00Z", "2026-11-04T00:00:00Z", true},
		{"just over 7d", "2026-10-27T23:59:59Z", "2026-11-04T00:00:00Z", false},
		{"inverted", "2026-11-03T18:00:00Z", "2026-11-03T12:00:00Z", false},
		{"empty range", "2026-11-03T12:00:00Z", "2026-11-03T12:00:00Z", false},
		{"future end", "2026-11-04T08:00:00Z", "2026-11-04T09:05:00Z", false},
		{"small future skew ok", "2026-11-04T08:00:00Z", "2026-11-04T09:00:30Z", true},
		{"missing start", "", "2026-11-04T08:00:00Z", false},
		{"not RFC3339", "yesterday", "2026-11-04T08:00:00Z", false},
		{"epoch zero", "1970-01-01T00:00:00Z", "1970-01-02T00:00:00Z", false},
		{"unix number", "1762171200", "2026-11-04T08:00:00Z", false},
	}
	for _, c := range cases {
		req := base()
		req.Start, req.End = c.start, c.end
		_, err := buildQuery(req, fixedNow)
		if (err == nil) != c.ok {
			t.Errorf("%s: err=%v, want ok=%v", c.name, err, c.ok)
		}
	}
}

func TestEndDefaultsToNow(t *testing.T) {
	req := base()
	req.Start, req.End = "2026-11-04T08:00:00Z", ""
	q, err := buildQuery(req, fixedNow)
	if err != nil || q.EndMicros != fixedNow.UnixMicro() {
		t.Fatalf("end default: %v %d", err, q.EndMicros)
	}
}

func TestLimitAndOrder(t *testing.T) {
	for _, l := range []int{-1, maxLimit + 1, 1 << 30} {
		req := base()
		req.Limit = l
		if _, err := buildQuery(req, fixedNow); !errors.Is(err, errInvalid) {
			t.Errorf("limit %d accepted", l)
		}
	}
	req := base()
	req.Limit = maxLimit
	req.Order = "DESC"
	q, err := buildQuery(req, fixedNow)
	if err != nil || !strings.HasSuffix(q.SQL, "ORDER BY _timestamp DESC LIMIT 500") {
		t.Fatalf("max limit/desc: %v %s", err, q.SQL)
	}
	for _, o := range []string{"asc; DROP", "newest", "_timestamp"} {
		req := base()
		req.Order = o
		if _, err := buildQuery(req, fixedNow); !errors.Is(err, errInvalid) {
			t.Errorf("order %q accepted", o)
		}
	}
}

// Whatever combination is valid, the SQL has exactly the one allowed shape.
func TestSQLAlwaysSelectsFixedColumnsFromFixedStream(t *testing.T) {
	req := base()
	req.Contains = "x' UNION SELECT password FROM users --"
	req.Host = "h1"
	q, err := buildQuery(req, fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(q.SQL, "SELECT _timestamp, container_name, host, message FROM docker_logs WHERE ") {
		t.Fatalf("unexpected sql: %s", q.SQL)
	}
	if strings.Count(q.SQL, ";") != 0 && !strings.Contains(q.SQL, "LIKE '%x'' UNION") {
		t.Fatalf("injected text escaped the literal: %s", q.SQL)
	}
}
