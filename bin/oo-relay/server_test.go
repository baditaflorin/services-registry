package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	tokenA = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"
	tokenB = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
)

type fakeOO struct {
	srv      *httptest.Server
	lastBody atomic.Value // string
	lastAuth atomic.Value // string
	reply    func() (int, string)
	calls    atomic.Int32
}

func newFakeOO(t *testing.T, reply func() (int, string)) *fakeOO {
	f := &fakeOO{reply: reply}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		b, _ := io.ReadAll(r.Body)
		f.lastBody.Store(string(b))
		u, p, _ := r.BasicAuth()
		f.lastAuth.Store(u + ":" + p)
		if r.URL.Path != "/api/default/_search" || r.Method != http.MethodPost {
			t.Errorf("unexpected upstream call %s %s", r.Method, r.URL.Path)
		}
		code, body := f.reply()
		w.WriteHeader(code)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func newTestRelay(t *testing.T, f *fakeOO) *httptest.Server {
	r := newRelay([][]byte{[]byte(tokenA), []byte(tokenB)}, f.srv.URL, "root@example.com", "pw-not-a-token")
	r.now = func() time.Time { return fixedNow }
	s := httptest.NewServer(r.routes())
	t.Cleanup(s.Close)
	return s
}

func post(t *testing.T, s *httptest.Server, token, body string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, s.URL+"/v1/logs", strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out
}

const goodBody = `{"container_name":"domainscope-api","host":"0docker_docker_prod","contains":"[ext-traits-backfill]","start":"2026-11-03T12:00:00Z","end":"2026-11-03T18:30:00Z","limit":10}`

func TestAuth(t *testing.T) {
	f := newFakeOO(t, func() (int, string) { return 200, `{"hits":[]}` })
	s := newTestRelay(t, f)
	for _, tok := range []string{"", "nope", tokenA[:63], tokenA + "0", strings.ToUpper(tokenA)} {
		if code, _ := post(t, s, tok, goodBody); code != http.StatusUnauthorized {
			t.Errorf("token %q -> %d", tok, code)
		}
	}
	if f.calls.Load() != 0 {
		t.Fatal("unauthenticated request reached OpenObserve")
	}
	for _, tok := range []string{tokenA, tokenB} {
		if code, _ := post(t, s, tok, goodBody); code != http.StatusOK {
			t.Errorf("valid token rejected: %d", code)
		}
	}
	// Basic auth, query-string tokens and other methods are not accepted.
	resp, _ := http.Get(s.URL + "/v1/logs?token=" + tokenA)
	if resp.StatusCode == http.StatusOK {
		t.Error("GET /v1/logs accepted")
	}
	for _, p := range []string{"/api/default/_search", "/v1/sql", "/v1/crux/provider-events", "/"} {
		r, _ := http.Post(s.URL+p, "application/json", strings.NewReader("{}"))
		if r.StatusCode != http.StatusNotFound && r.StatusCode != http.StatusMethodNotAllowed {
			t.Errorf("%s -> %d", p, r.StatusCode)
		}
	}
	if r, _ := http.Get(s.URL + "/healthz"); r.StatusCode != 200 {
		t.Error("healthz")
	}
}

func TestRejectsUnknownFieldsAndBadJSON(t *testing.T) {
	f := newFakeOO(t, func() (int, string) { return 200, `{"hits":[]}` })
	s := newTestRelay(t, f)
	bodies := []string{
		`{"container_name":"a","start":"2026-11-04T08:00:00Z","sql":"SELECT * FROM users"}`,
		`{"container_name":"a","start":"2026-11-04T08:00:00Z","stream":"default"}`,
		`{"container_name":"a","start":"2026-11-04T08:00:00Z","org":"other"}`,
		`{"container_name":"a","start":"2026-11-04T08:00:00Z"} {"container_name":"b"}`,
		`not json`, ``, `[]`, `{"container_name":5}`,
		`{"container_name":"a' OR '1'='1","start":"2026-11-04T08:00:00Z"}`,
		`{"container_name":"a","start":"2026-11-04T08:00:00Z","limit":501}`,
		`{"container_name":"a","start":"2026-10-01T00:00:00Z","end":"2026-11-04T00:00:00Z"}`,
	}
	for _, b := range bodies {
		if code, out := post(t, s, tokenA, b); code != http.StatusBadRequest || out["status"] != "invalid_request" {
			t.Errorf("body %q -> %d %v", b, code, out)
		}
	}
	if code, _ := post(t, s, tokenA, `{"container_name":"`+strings.Repeat("a", 5000)+`"}`); code != http.StatusBadRequest {
		t.Errorf("oversized body -> %d", code)
	}
	if f.calls.Load() != 0 {
		t.Fatal("an invalid request reached OpenObserve")
	}
}

func TestHappyPathBuildsFixedUpstreamRequest(t *testing.T) {
	hits := `{"hits":[
	 {"_timestamp":1762171201000001,"container_name":"domainscope-api","host":"0docker_docker_prod","message":"[ext-traits-backfill] started run=1"},
	 {"_timestamp":1762171202000002,"container_name":"domainscope-api","host":"0docker_docker_prod","message":"[ext-traits-backfill] key ak_0123456789abcdef0123456789abcdef used"}]}`
	f := newFakeOO(t, func() (int, string) { return 200, hits })
	s := newTestRelay(t, f)
	code, out := post(t, s, tokenA, goodBody)
	if code != 200 || out["status"] != "ok" || out["count"].(float64) != 2 || out["truncated"] != false {
		t.Fatalf("%d %v", code, out)
	}
	rows := out["rows"].([]any)
	row := rows[0].(map[string]any)
	ts, err := time.Parse(time.RFC3339Nano, row["timestamp"].(string))
	if err != nil || row["timestamp_unix_micros"].(float64) != 1762171201000001 || ts.UnixMicro() != 1762171201000001 {
		t.Errorf("row %v", row)
	}
	if strings.Contains(rows[1].(map[string]any)["message"].(string), "ak_0123") {
		t.Error("credential was not redacted")
	}
	for k := range row {
		switch k {
		case "timestamp", "timestamp_unix_micros", "container_name", "host", "message":
		default:
			t.Errorf("unexpected field %q in a row", k)
		}
	}

	var up struct {
		Query struct {
			SQL   string `json:"sql"`
			Start int64  `json:"start_time"`
			End   int64  `json:"end_time"`
			Size  int    `json:"size"`
		} `json:"query"`
	}
	if err := json.Unmarshal([]byte(f.lastBody.Load().(string)), &up); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(up.Query.SQL, "message LIKE '%[ext-traits-backfill]%'") || up.Query.Size != 10 {
		t.Errorf("upstream query %+v", up)
	}
	if f.lastAuth.Load() != "root@example.com:pw-not-a-token" {
		t.Error("relay did not use its own OpenObserve credential")
	}
	if strings.Contains(f.lastBody.Load().(string), tokenA) {
		t.Error("caller token forwarded upstream")
	}
}

func TestSubstringIsRecheckedExactly(t *testing.T) {
	// The SQL pattern for "a_b" is looser (matches "aXb" too); the relay must drop the extras.
	hits := `{"hits":[
	 {"_timestamp":1762171201000001,"container_name":"c","host":"h","message":"a_b exact"},
	 {"_timestamp":1762171202000001,"container_name":"c","host":"h","message":"aXb loose"}]}`
	f := newFakeOO(t, func() (int, string) { return 200, hits })
	s := newTestRelay(t, f)
	_, out := post(t, s, tokenA, `{"container_name":"c","contains":"a_b","start":"2026-11-03T12:00:00Z","end":"2026-11-03T18:00:00Z"}`)
	if out["count"].(float64) != 1 {
		t.Fatalf("%v", out)
	}
}

func TestPagingHints(t *testing.T) {
	hits := `{"hits":[
	 {"_timestamp":1762171201000001,"container_name":"c","host":"h","message":"one"},
	 {"_timestamp":1762171202000002,"container_name":"c","host":"h","message":"two"}]}`
	f := newFakeOO(t, func() (int, string) { return 200, hits })
	s := newTestRelay(t, f)
	_, out := post(t, s, tokenA, `{"container_name":"c","start":"2026-11-03T12:00:00Z","end":"2026-11-03T18:00:00Z","limit":2}`)
	if out["truncated"] != true || out["next_start"] == nil || out["next_end"] != nil {
		t.Fatalf("asc: %v", out)
	}
	_, out = post(t, s, tokenA, `{"container_name":"c","order":"desc","start":"2026-11-03T12:00:00Z","end":"2026-11-03T18:00:00Z","limit":2}`)
	if out["truncated"] != true || out["next_end"] == nil || out["next_start"] != nil {
		t.Fatalf("desc: %v", out)
	}
}

func TestUpstreamFailureIsNeverAnEmptyResult(t *testing.T) {
	for name, reply := range map[string]func() (int, string){
		"http 401":        func() (int, string) { return 401, "Unauthorized Access" },
		"http 500":        func() (int, string) { return 500, "boom" },
		"not json":        func() (int, string) { return 200, "oops" },
		"error object":    func() (int, string) { return 200, `{"code":400,"message":"bad sql"}` },
		"float timestamp": func() (int, string) { return 200, `{"hits":[{"_timestamp":1.5,"message":"x"}]}` },
	} {
		f := newFakeOO(t, reply)
		s := newTestRelay(t, f)
		code, out := post(t, s, tokenA, goodBody)
		if code != http.StatusBadGateway || out["status"] != "unavailable" || out["rows"] != nil {
			t.Errorf("%s: %d %v", name, code, out)
		}
		if strings.Contains(strings.ToLower(toJSON(out)), "unauthorized access") || strings.Contains(toJSON(out), "pw-not-a-token") {
			t.Errorf("%s: upstream detail leaked: %v", name, out)
		}
	}
}

func toJSON(v any) string { b, _ := json.Marshal(v); return string(b) }

func TestLongMessagesAreTrimmed(t *testing.T) {
	long := strings.Repeat("é", maxMessageBytes) // 2 bytes each
	f := newFakeOO(t, func() (int, string) {
		return 200, `{"hits":[{"_timestamp":1762171201000001,"container_name":"c","host":"h","message":"` + long + `"}]}`
	})
	s := newTestRelay(t, f)
	_, out := post(t, s, tokenA, `{"container_name":"c","start":"2026-11-03T12:00:00Z","end":"2026-11-03T18:00:00Z"}`)
	row := out["rows"].([]any)[0].(map[string]any)
	if row["message_truncated"] != true || len(row["message"].(string)) > maxMessageBytes {
		t.Fatalf("not trimmed: %v", row["message_truncated"])
	}
}

func TestConcurrencyGate(t *testing.T) {
	release := make(chan struct{})
	started := make(chan struct{}, maxConcurrent)
	f := &fakeOO{}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started <- struct{}{}
		<-release
		_, _ = w.Write([]byte(`{"hits":[]}`))
	}))
	t.Cleanup(f.srv.Close)
	s := newTestRelay(t, f)
	done := make(chan int, maxConcurrent)
	for i := 0; i < maxConcurrent; i++ {
		go func() { c, _ := post(t, s, tokenA, goodBody); done <- c }()
	}
	for i := 0; i < maxConcurrent; i++ {
		<-started
	}
	if code, out := post(t, s, tokenA, goodBody); code != http.StatusTooManyRequests || out["status"] != "busy" {
		t.Errorf("overflow request: %d %v", code, out)
	}
	close(release)
	for i := 0; i < maxConcurrent; i++ {
		if c := <-done; c != 200 {
			t.Errorf("queued request: %d", c)
		}
	}
}

func TestReadTokens(t *testing.T) {
	dir := t.TempDir()
	write := func(content string) string {
		p := filepath.Join(dir, "t")
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	if tk, err := readTokens(write(tokenA+"\n"+tokenB+"\n\n"), "pw"); err != nil || len(tk) != 2 {
		t.Fatalf("two tokens: %v %d", err, len(tk))
	}
	for name, content := range map[string]string{
		"empty": "", "short": "abcd", "non-hex": strings.Repeat("z", 64),
		"too many":        strings.Repeat(tokenA+"\n", maxTokens+1),
		"equals password": tokenA,
	} {
		pw := "pw"
		if name == "equals password" {
			pw = tokenA
		}
		if _, err := readTokens(write(content), pw); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	if _, err := readTokens(filepath.Join(dir, "missing"), "pw"); err == nil {
		t.Error("missing file accepted")
	}
}
