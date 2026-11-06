package main

import (
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestTraceCensusIsBoundedCachedAndHonest(t *testing.T) {
	s, calls := fleetTestRelay(t, func(r *http.Request) string {
		b, _ := io.ReadAll(r.Body)
		if r.URL.Query().Get("type") != "traces" || !strings.Contains(string(b), "GROUP BY service_name") || strings.Contains(string(b), "events") {
			t.Fatal(string(b))
		}
		return fmt.Sprintf(`{"hits":[{"service_name":"reported-service","spans":42,"last_seen":%d}]}`, fixedNow.Add(-time.Minute).UnixMicro())
	})
	for i := 0; i < 2; i++ {
		req, _ := http.NewRequest("GET", s.URL+"/v1/fleet/trace-census", nil)
		req.Header.Set("Authorization", "Bearer "+tokenB)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		b, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != 200 || !strings.Contains(string(b), `"spans":42`) || !strings.Contains(string(b), "coverage remain unknown") {
			t.Fatal(resp.StatusCode, string(b))
		}
	}
	if *calls != 1 {
		t.Fatal("census repeated an uncached expensive query", *calls)
	}
}
func TestTraceCensusRejectsPartialOrMisTimedEvidence(t *testing.T) {
	for _, body := range []string{`{"hits":[],"is_partial":true}`, `{"hits":[{"service_name":"s","spans":2,"last_seen":1}]}`} {
		s, _ := fleetTestRelay(t, func(*http.Request) string { return body })
		req, _ := http.NewRequest("GET", s.URL+"/v1/fleet/trace-census", nil)
		req.Header.Set("Authorization", "Bearer "+tokenB)
		resp, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		resp.Body.Close()
		if resp.StatusCode != 502 {
			t.Fatal(resp.StatusCode)
		}
	}
}
