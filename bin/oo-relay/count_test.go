package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func postCount(t *testing.T, s *httptest.Server, token, body string) (int, map[string]any) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, s.URL+"/v1/logs/count", strings.NewReader(body))
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

// fixedNow is 2026-11-04 in server_test.go, so these windows are in the past.
const countBody = `{"container_name":"go_capture_store-app-1","contains":"POST /v1/captures","start":"2026-11-02T10:00:00Z","end":"2026-11-04T06:00:00Z"}`

func TestCountNeedsTheRelayToken(t *testing.T) {
	f := newFakeOO(t, func() (int, string) { return 200, `{"hits":[{"n":1}]}` })
	s := newTestRelay(t, f)
	for _, token := range []string{"", "nope"} {
		if code, _ := postCount(t, s, token, countBody); code != 401 {
			t.Fatalf("token %q: %d", token, code)
		}
	}
	if f.calls.Load() != 0 {
		t.Fatal("an unauthenticated count reached OpenObserve")
	}
}

func TestCountDaysSumsOneQueryPerUTCDay(t *testing.T) {
	var mu sync.Mutex
	var sqls []string
	var starts, ends []int64
	f := newFakeOO(t, nil)
	var n atomic.Int64
	f.reply = func() (int, string) {
		var up struct {
			Query struct {
				SQL   string `json:"sql"`
				Start int64  `json:"start_time"`
				End   int64  `json:"end_time"`
			} `json:"query"`
		}
		_ = json.Unmarshal([]byte(f.lastBody.Load().(string)), &up)
		mu.Lock()
		sqls = append(sqls, up.Query.SQL)
		starts = append(starts, up.Query.Start)
		ends = append(ends, up.Query.End)
		mu.Unlock()
		v := n.Add(1) * 10
		return 200, `{"hits":[{"n":` + itoa(v) + `}]}`
	}
	s := newTestRelay(t, f)
	code, out := postCount(t, s, tokenA, countBody)
	if code != 200 || out["status"] != "ok" || out["bucket"] != "day" || out["exact"] != true {
		t.Fatalf("%d %v", code, out)
	}
	buckets := out["buckets"].([]any)
	if len(buckets) != 3 || out["total"].(float64) != 60 {
		t.Fatalf("buckets %v total %v", buckets, out["total"])
	}
	first := buckets[0].(map[string]any)
	if first["start"] != "2026-11-02T10:00:00Z" || first["end"] != "2026-11-03T00:00:00Z" {
		t.Errorf("first bucket %v", first)
	}
	last := buckets[2].(map[string]any)
	if last["start"] != "2026-11-04T00:00:00Z" || last["end"] != "2026-11-04T06:00:00Z" {
		t.Errorf("last bucket %v", last)
	}
	for _, q := range sqls {
		if !strings.HasPrefix(q, "SELECT COUNT(*) AS n FROM docker_logs WHERE container_name = 'go_capture_store-app-1'") ||
			!strings.Contains(q, "message LIKE '%POST /v1/captures%'") || strings.Contains(q, "GROUP BY") {
			t.Errorf("unexpected upstream SQL %q", q)
		}
	}
	for i := 1; i < len(starts); i++ {
		if starts[i] != ends[i-1] {
			t.Errorf("window %d does not start where the previous one ended", i)
		}
	}
}

func TestCountHourBucketsAreLimitedToADay(t *testing.T) {
	f := newFakeOO(t, func() (int, string) { return 200, `{"hits":[{"n":0}]}` })
	s := newTestRelay(t, f)
	code, out := postCount(t, s, tokenA, `{"container_name":"c","start":"2026-11-03T00:00:00Z","end":"2026-11-03T05:00:00Z","bucket":"hour"}`)
	if code != 200 || len(out["buckets"].([]any)) != 5 || out["total"].(float64) != 0 {
		t.Fatalf("%d %v", code, out)
	}
	code, out = postCount(t, s, tokenA, `{"container_name":"c","start":"2026-11-02T00:00:00Z","end":"2026-11-03T05:00:00Z","bucket":"hour"}`)
	if code != 400 || out["status"] != "invalid_request" {
		t.Fatalf("a 29 hour span with hour buckets must be refused: %d %v", code, out)
	}
}

func TestCountRejectsWhatLogsRejects(t *testing.T) {
	f := newFakeOO(t, func() (int, string) { return 200, `{"hits":[{"n":1}]}` })
	s := newTestRelay(t, f)
	for name, body := range map[string]string{
		"unknown field":   `{"container_name":"c","start":"2026-11-03T00:00:00Z","sql":"select 1"}`,
		"bad container":   `{"container_name":"c' OR 1=1","start":"2026-11-03T00:00:00Z"}`,
		"no start":        `{"container_name":"c"}`,
		"longer than 7 d": `{"container_name":"c","start":"2026-10-01T00:00:00Z","end":"2026-11-03T00:00:00Z"}`,
		"future end":      `{"container_name":"c","start":"2026-11-03T00:00:00Z","end":"2030-01-01T00:00:00Z"}`,
		"bad bucket":      `{"container_name":"c","start":"2026-11-03T00:00:00Z","bucket":"week"}`,
		"two objects":     `{"container_name":"c","start":"2026-11-03T00:00:00Z"}{"a":1}`,
		"control char":    "{\"container_name\":\"c\",\"start\":\"2026-11-03T00:00:00Z\",\"contains\":\"a\\u0001b\"}",
	} {
		if code, out := postCount(t, s, tokenA, body); code != 400 || out["status"] != "invalid_request" {
			t.Errorf("%s: %d %v", name, code, out)
		}
	}
	if f.calls.Load() != 0 {
		t.Fatal("an invalid count reached OpenObserve")
	}
}

func TestCountMarksALooserPatternAsNotExact(t *testing.T) {
	f := newFakeOO(t, func() (int, string) { return 200, `{"hits":[{"n":7}]}` })
	s := newTestRelay(t, f)
	code, out := postCount(t, s, tokenA, `{"container_name":"c","contains":"client_ip","start":"2026-11-03T00:00:00Z","end":"2026-11-03T06:00:00Z"}`)
	if code != 200 || out["exact"] != false {
		t.Fatalf("a substring with '_' is matched loosely and must say so: %d %v", code, out)
	}
	if strings.Contains(f.lastBody.Load().(string), `\\`) {
		t.Error("a backslash reached the SQL")
	}
}

func TestCountUpstreamFailureIsNeverZero(t *testing.T) {
	for name, reply := range map[string]func() (int, string){
		"http 500":     func() (int, string) { return 500, `boom` },
		"partial":      func() (int, string) { return 200, `{"hits":[{"n":3}],"is_partial":true}` },
		"no hits":      func() (int, string) { return 200, `{}` },
		"two rows":     func() (int, string) { return 200, `{"hits":[{"n":1},{"n":2}]}` },
		"not a number": func() (int, string) { return 200, `{"hits":[{"n":"many"}]}` },
		"negative":     func() (int, string) { return 200, `{"hits":[{"n":-1}]}` },
	} {
		f := newFakeOO(t, reply)
		s := newTestRelay(t, f)
		code, out := postCount(t, s, tokenA, countBody)
		if code != 502 || out["status"] != "unavailable" {
			t.Errorf("%s: %d %v", name, code, out)
		}
		if _, has := out["total"]; has && out["total"].(float64) != 0 {
			t.Errorf("%s: a failed count reported a total", name)
		}
	}
}

func itoa(n int64) string { b, _ := json.Marshal(n); return string(b) }
