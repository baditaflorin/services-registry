package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func fleetTestRelay(t *testing.T, reply func(*http.Request) string) (*httptest.Server, *int) {
	t.Helper()
	calls := 0
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		u, p, ok := r.BasicAuth()
		if !ok || u != "root@local" || p != "private-password" {
			t.Error("missing internal identity")
		}
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, reply(r))
	}))
	t.Cleanup(up.Close)
	r := newRelay([][]byte{[]byte(tokenA)}, up.URL, "root@local", "private-password")
	r.fleetTokens = [][]byte{[]byte(tokenB)}
	r.now = func() time.Time { return fixedNow }
	s := httptest.NewServer(r.routes())
	t.Cleanup(s.Close)
	return s, &calls
}
func fleetPost(t *testing.T, s *httptest.Server, path, token, body string) (int, string) {
	t.Helper()
	r, _ := http.NewRequest("POST", s.URL+path, strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Authorization", "Bearer "+token)
	resp, e := http.DefaultClient.Do(r)
	if e != nil {
		t.Fatal(e)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(b)
}

const fleetBody = `{"start":"2026-11-04T08:00:00Z","end":"2026-11-04T09:00:00Z","limit":10}`

func TestFleetReaderIdentityAndBounds(t *testing.T) {
	s, calls := fleetTestRelay(t, func(r *http.Request) string { return `{"hits":[]}` })
	for _, path := range []string{"/v1/fleet/logs", "/v1/fleet/traces"} {
		for _, tok := range []string{"", tokenA, "invalid"} {
			if code, _ := fleetPost(t, s, path, tok, fleetBody); code != 401 {
				t.Fatal(path, code)
			}
		}
	}
	if *calls != 0 {
		t.Fatal("legacy or missing identity reached upstream")
	}
	for _, body := range []string{`{"sql":"SELECT *"}`, `{"start":"2026-11-03T00:00:00Z","limit":10}`, `{"start":"2026-11-04T08:00:00Z","limit":101}`, `{"start":"2026-11-04T08:00:00Z","trace_id":"a' OR 1=1"}`, `{"start":"2026-11-04T08:00:00Z","service_name":"a'b"}`} {
		if code, _ := fleetPost(t, s, "/v1/fleet/traces", tokenB, body); code != 400 {
			t.Fatal(body, code)
		}
	}
	if *calls != 0 {
		t.Fatal("invalid query reached upstream")
	}
	if code, body := fleetPost(t, s, "/v1/fleet/traces", tokenB, fleetBody); code != 200 || !strings.Contains(body, `"count":0`) {
		t.Fatal(code, body)
	}
}
func TestFleetTraceProjectionHasOnlyScalarEvidence(t *testing.T) {
	s, _ := fleetTestRelay(t, func(r *http.Request) string {
		if r.URL.Path != "/api/default/_search" || r.URL.Query().Get("type") != "traces" {
			t.Error(r.URL)
		}
		b, _ := io.ReadAll(r.Body)
		if strings.Contains(string(b), "SELECT *") || strings.Contains(string(b), "events") {
			t.Error("unreviewed trace attributes requested")
		}
		return `{"hits":[{"_timestamp":1794003364259052,"trace_id":"320d1728d013361507a7afdf06d5199d","span_id":"06e38a60f407ed19","reference_parent_span_id":"dd3ac4d33fec7ede","service_name":"go-infrastructure-fetch-cache","operation_name":"GET /path?token=private","start_time":1794003364259052495,"end_time":1794003364260461739,"duration":1409,"span_status":"UNSET","http_response_status_code":"404","events":"password=secret-value"}]}`
	})
	code, body := fleetPost(t, s, "/v1/fleet/traces", tokenB, fleetBody)
	if code != 200 {
		t.Fatal(code, body)
	}
	var out traceResponse
	if json.Unmarshal([]byte(body), &out) != nil || len(out.Rows) != 1 {
		t.Fatal(body)
	}
	row := out.Rows[0]
	if row.Start != "1794003364259052495" || row.Duration != 0.001409244 || row.Parent != "dd3ac4d33fec7ede" || row.Operation != "GET /path" || row.Status != "UNSET" {
		t.Fatalf("timing/identity lost: %+v", row)
	}
	if strings.Contains(body, "secret-value") || strings.Contains(body, "private") || strings.Contains(body, "events") {
		t.Fatal("unreviewed attributes escaped")
	}
}
func TestFleetPartialResultsAreNotEmptySuccess(t *testing.T) {
	s, _ := fleetTestRelay(t, func(*http.Request) string { return `{"hits":[],"is_partial":true}` })
	for _, path := range []string{"/v1/fleet/logs", "/v1/fleet/traces"} {
		if code, body := fleetPost(t, s, path, tokenB, fleetBody); code != 502 {
			t.Fatal(code, body)
		}
	}
}
func TestFleetCoverageOmitsUnrelatedStreamsAndCredentials(t *testing.T) {
	s, _ := fleetTestRelay(t, func(r *http.Request) string {
		if r.Method != "GET" || r.URL.Path != "/api/default/streams" {
			t.Fatal(r.Method, r.URL)
		}
		return `{"list":[{"name":"docker_logs","stream_type":"logs","stats":{"doc_num":42,"doc_time_max":1794003364259052},"settings":{"token":"do-not-export"}},{"name":"private_other","stream_type":"logs","stats":{"doc_num":9}}]}`
	})
	r, _ := http.NewRequest("GET", s.URL+"/v1/fleet/coverage", nil)
	r.Header.Set("Authorization", "Bearer "+tokenB)
	resp, e := http.DefaultClient.Do(r)
	if e != nil {
		t.Fatal(e)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 || strings.Contains(string(body), "do-not-export") || strings.Contains(string(body), "private_other") || !strings.Contains(string(body), "instrumentation") {
		t.Fatal(resp.StatusCode, string(body))
	}
}
