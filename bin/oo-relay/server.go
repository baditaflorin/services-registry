package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	openObserveOrg   = "default"
	maxRequestBody   = 4096
	maxUpstreamBody  = 16 << 20
	maxMessageBytes  = 8192
	maxConcurrent    = 4
	upstreamTimeout  = 35 * time.Second
	upstreamSearchTO = 30 // seconds, passed to OpenObserve
)

type relay struct {
	tokens      [][]byte
	fleetTokens [][]byte
	upstream    string
	user        string
	password    string
	client      *http.Client
	now         func() time.Time
	gate        chan struct{}
}

func newRelay(tokens [][]byte, upstream, user, password string) *relay {
	return &relay{
		tokens:   tokens,
		upstream: strings.TrimRight(upstream, "/"),
		user:     user,
		password: password,
		client:   &http.Client{Timeout: upstreamTimeout, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		now:      time.Now,
		gate:     make(chan struct{}, maxConcurrent),
	}
}

func (r *relay) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /version", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, 200, map[string]string{"version": strings.TrimSpace(relayVersion), "commit": relayCommit})
	})
	mux.Handle("GET /v1/fleet/coverage", r.authenticateTokens(r.fleetTokens, http.HandlerFunc(r.fleetCoverage)))
	mux.Handle("POST /v1/fleet/logs", r.authenticateTokens(r.fleetTokens, http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) { r.fleetRead(w, q, false) })))
	mux.Handle("POST /v1/fleet/traces", r.authenticateTokens(r.fleetTokens, http.HandlerFunc(func(w http.ResponseWriter, q *http.Request) { r.fleetRead(w, q, true) })))
	mux.Handle("POST /v1/logs", r.authenticate(http.HandlerFunc(r.logs)))
	return mux
}

func (r *relay) authenticate(next http.Handler) http.Handler {
	return r.authenticateTokens(r.tokens, next)
}
func (r *relay) authenticateTokens(tokens [][]byte, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		const prefix = "Bearer "
		authorization := req.Header.Get("Authorization")
		if !strings.HasPrefix(authorization, prefix) {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		provided := []byte(strings.TrimPrefix(authorization, prefix))
		ok := 0
		// Compare against every token without stopping early.
		for _, t := range tokens {
			if len(provided) == len(t) {
				ok |= subtle.ConstantTimeCompare(provided, t)
			}
		}
		if ok != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, req)
	})
}

type logRow struct {
	Timestamp       string `json:"timestamp"`
	TimestampMicros int64  `json:"timestamp_unix_micros"`
	ContainerName   string `json:"container_name"`
	Host            string `json:"host"`
	Message         string `json:"message"`
	Truncated       bool   `json:"message_truncated,omitempty"`
}

type logResponse struct {
	Status    string   `json:"status"`
	Stream    string   `json:"stream,omitempty"`
	Count     int      `json:"count"`
	Limit     int      `json:"limit,omitempty"`
	Truncated bool     `json:"truncated"`
	NextStart string   `json:"next_start,omitempty"`
	NextEnd   string   `json:"next_end,omitempty"`
	Rows      []logRow `json:"rows"`
	Error     string   `json:"error,omitempty"`
}

func (r *relay) logs(w http.ResponseWriter, req *http.Request) {
	defer req.Body.Close()
	var in logRequest
	decoder := json.NewDecoder(io.LimitReader(req.Body, maxRequestBody))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&in); err != nil {
		writeJSON(w, http.StatusBadRequest, logResponse{Status: "invalid_request", Error: "body must be one JSON object with only: container_name, host, contains, start, end, limit, order"})
		return
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		writeJSON(w, http.StatusBadRequest, logResponse{Status: "invalid_request", Error: "request must contain one JSON object"})
		return
	}
	q, err := buildQuery(in, r.now())
	if err != nil {
		writeJSON(w, http.StatusBadRequest, logResponse{Status: "invalid_request", Error: strings.TrimPrefix(err.Error(), errInvalid.Error()+": ")})
		return
	}

	select {
	case r.gate <- struct{}{}:
		defer func() { <-r.gate }()
	default:
		w.Header().Set("Retry-After", "2")
		writeJSON(w, http.StatusTooManyRequests, logResponse{Status: "busy", Error: "too many concurrent queries; retry shortly"})
		return
	}

	hits, err := r.search(req.Context(), q)
	if err != nil {
		log.Printf("query failed container=%s host=%s: %v", in.ContainerName, in.Host, err)
		writeJSON(w, http.StatusBadGateway, logResponse{Status: "unavailable", Error: "OpenObserve query unavailable; this is not an empty result"})
		return
	}
	resp := shape(q, in, hits)
	log.Printf("query container=%s host=%s contains_len=%d start=%d end=%d limit=%d returned=%d remote=%s",
		in.ContainerName, in.Host, len(in.Contains), q.StartMicros, q.EndMicros, q.Limit, resp.Count, req.RemoteAddr)
	writeJSON(w, http.StatusOK, resp)
}

type hit struct {
	Micros    int64
	Container string
	Host      string
	Message   string
}

// shape re-checks, redacts and trims the rows. The substring is re-checked
// exactly here because the SQL pattern may be looser (see likePattern). Paging
// hints come from the raw hit count, before this filter, so they stay right.
func shape(q builtQuery, in logRequest, hits []hit) logResponse {
	resp := logResponse{Status: "ok", Stream: stream, Limit: q.Limit, Rows: []logRow{}}
	resp.Truncated = len(hits) >= q.Limit
	if resp.Truncated && len(hits) > 0 {
		last := time.UnixMicro(hits[len(hits)-1].Micros).UTC().Format(time.RFC3339Nano)
		if strings.EqualFold(in.Order, "desc") {
			resp.NextEnd = last
		} else {
			resp.NextStart = last
		}
	}
	for _, h := range hits {
		if q.Contains != "" && !strings.Contains(h.Message, q.Contains) {
			continue
		}
		row := logRow{
			Timestamp:       time.UnixMicro(h.Micros).UTC().Format(time.RFC3339Nano),
			TimestampMicros: h.Micros,
			ContainerName:   h.Container,
			Host:            h.Host,
		}
		msg := redact(h.Message)
		if len(msg) > maxMessageBytes {
			msg = strings.ToValidUTF8(msg[:maxMessageBytes], "")
			row.Truncated = true
		}
		row.Message = msg
		resp.Rows = append(resp.Rows, row)
	}
	resp.Count = len(resp.Rows)
	return resp
}

func (r *relay) search(ctx context.Context, q builtQuery) ([]hit, error) {
	payload, err := json.Marshal(map[string]any{
		"query": map[string]any{
			"sql":        q.SQL,
			"start_time": q.StartMicros,
			"end_time":   q.EndMicros,
			"from":       0,
			"size":       q.Limit,
		},
		"search_type": "ui",
		"timeout":     upstreamSearchTO,
	})
	if err != nil {
		return nil, err
	}
	endpoint := r.upstream + "/api/" + openObserveOrg + "/_search"
	upReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	upReq.Header.Set("Content-Type", "application/json")
	upReq.SetBasicAuth(r.user, r.password)
	resp, err := r.client.Do(upReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("OpenObserve returned HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxUpstreamBody+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxUpstreamBody {
		return nil, errors.New("OpenObserve response too large")
	}
	return parseHits(body)
}

func parseHits(body []byte) ([]hit, error) {
	var result struct {
		Hits []struct {
			Timestamp json.Number `json:"_timestamp"`
			Container string      `json:"container_name"`
			Host      string      `json:"host"`
			Message   any         `json:"message"`
		} `json:"hits"`
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	if err := decoder.Decode(&result); err != nil {
		return nil, fmt.Errorf("decode OpenObserve response: %w", err)
	}
	if result.Hits == nil {
		return nil, errors.New("OpenObserve response has no hits array")
	}
	hits := make([]hit, 0, len(result.Hits))
	for _, h := range result.Hits {
		micros, err := h.Timestamp.Int64()
		if err != nil {
			return nil, fmt.Errorf("OpenObserve returned a non-integer _timestamp")
		}
		msg, _ := h.Message.(string)
		if !utf8.ValidString(msg) {
			msg = strings.ToValidUTF8(msg, "�")
		}
		hits = append(hits, hit{Micros: micros, Container: h.Container, Host: h.Host, Message: msg})
	}
	return hits, nil
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
