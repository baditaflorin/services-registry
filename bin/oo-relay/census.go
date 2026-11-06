package main

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"
)

type tracedService struct {
	Service  string `json:"service_name"`
	Spans    int64  `json:"spans"`
	LastSeen string `json:"last_seen"`
}
type traceCensus struct {
	Status    string          `json:"status"`
	Window    string          `json:"window"`
	CheckedAt string          `json:"checked_at"`
	Services  []tracedService `json:"services"`
	Truncated bool            `json:"truncated"`
	Note      string          `json:"note"`
}

func (r *relay) traceCensus(w http.ResponseWriter, req *http.Request) {
	if req.URL.RawQuery != "" {
		writeJSON(w, 400, map[string]string{"status": "invalid_request", "error": "query parameters are not accepted"})
		return
	}
	r.censusMu.Lock()
	if len(r.censusCache) > 0 && r.now().Sub(r.censusAt) < 5*time.Minute {
		body := append([]byte{}, r.censusCache...)
		r.censusMu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.Write(body)
		return
	}
	if r.censusBusy {
		r.censusMu.Unlock()
		w.Header().Set("Retry-After", "2")
		writeJSON(w, 429, map[string]string{"status": "busy", "error": "trace census collection is running"})
		return
	}
	r.censusBusy = true
	r.censusMu.Unlock()
	defer func() { r.censusMu.Lock(); r.censusBusy = false; r.censusMu.Unlock() }()
	release := r.fleetGate(w)
	if release == nil {
		return
	}
	defer release()
	now := r.now()
	q := builtQuery{SQL: "SELECT service_name, COUNT(*) AS spans, MAX(_timestamp) AS last_seen FROM default GROUP BY service_name ORDER BY spans DESC LIMIT 100", StartMicros: now.Add(-time.Hour).UnixMicro(), EndMicros: now.UnixMicro(), Limit: 100}
	body, err := r.fleetSearch(req.Context(), q, true)
	if err != nil {
		writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "trace census could not be read; coverage remains unknown"})
		return
	}
	var env struct {
		Hits    []map[string]json.RawMessage `json:"hits"`
		Partial bool                         `json:"is_partial"`
	}
	if json.Unmarshal(body, &env) != nil || env.Hits == nil || env.Partial {
		writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "trace census schema is missing or partial"})
		return
	}
	out := traceCensus{Status: "ok", Window: "1h", CheckedAt: now.UTC().Format(time.RFC3339Nano), Services: []tracedService{}, Truncated: len(env.Hits) >= 100, Note: "Observed spans in this bounded window only. Registry and fleet-wide instrumentation coverage remain unknown."}
	for i, h := range env.Hits {
		if i >= 100 {
			break
		}
		service := safeScalar(scalar(h, "service_name"), 128)
		spans, e1 := strconv.ParseInt(scalar(h, "spans"), 10, 64)
		seen, e2 := strconv.ParseInt(scalar(h, "last_seen"), 10, 64)
		if service == "" || e1 != nil || e2 != nil || spans < 1 || seen < q.StartMicros || seen > q.EndMicros {
			writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "trace census observation is invalid"})
			return
		}
		out.Services = append(out.Services, tracedService{Service: service, Spans: spans, LastSeen: time.UnixMicro(seen).UTC().Format(time.RFC3339Nano)})
	}
	b, _ := json.Marshal(out)
	r.censusMu.Lock()
	r.censusCache = append([]byte{}, b...)
	r.censusAt = now
	r.censusMu.Unlock()
	writeJSON(w, 200, out)
}
