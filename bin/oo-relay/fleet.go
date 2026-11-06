package main

// These projections expose reviewed scalar evidence only. They share neither
// arbitrary SQL nor the OpenObserve root identity with the Hub.
import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type fleetRead struct {
	Container string `json:"container_name,omitempty"`
	Host      string `json:"host,omitempty"`
	Contains  string `json:"contains,omitempty"`
	Service   string `json:"service_name,omitempty"`
	Trace     string `json:"trace_id,omitempty"`
	Start     string `json:"start"`
	End       string `json:"end,omitempty"`
	Limit     int    `json:"limit,omitempty"`
}

var traceIDRe = regexp.MustCompile(`^[a-f0-9]{32}$`)
var spanIDRe = regexp.MustCompile(`^[a-f0-9]{16}$`)

func buildFleetRead(in fleetRead, traces bool, now time.Time) (builtQuery, error) {
	// Reuse the existing time/limit/substring rules, then narrow to 24 hours and100 rows.
	base := logRequest{ContainerName: "fleet", Start: in.Start, End: in.End, Limit: in.Limit, Contains: in.Contains, Order: "desc"}
	q, err := buildQuery(base, now)
	if err != nil {
		return q, err
	}
	if q.EndMicros-q.StartMicros > int64(24*time.Hour/time.Microsecond) || q.Limit > 100 {
		return q, invalid("fleet reads allow 24 hours and 100 rows")
	}
	for _, v := range []string{in.Container, in.Host, in.Service} {
		if v != "" && !nameRe.MatchString(v) {
			return q, invalid("invalid exact identity")
		}
	}
	where := []string{}
	table := "docker_logs"
	cols := "_timestamp, container_name, host, message"
	if traces {
		if in.Container != "" || in.Host != "" || in.Contains != "" {
			return q, invalid("trace reads accept service_name and trace_id only")
		}
		if in.Trace != "" && !traceIDRe.MatchString(in.Trace) {
			return q, invalid("invalid trace_id")
		}
		if in.Service != "" {
			where = append(where, "service_name = '"+in.Service+"'")
		}
		if in.Trace != "" {
			where = append(where, "trace_id = '"+in.Trace+"'")
		}
		table = "default"
		cols = "_timestamp, trace_id, span_id, reference_parent_span_id, service_name, operation_name, start_time, end_time, duration, span_status, http_request_method, http_response_status_code"
	} else {
		if in.Service != "" || in.Trace != "" {
			return q, invalid("log reads accept container_name and host only")
		}
		if in.Container != "" {
			where = append(where, "container_name = '"+in.Container+"'")
		}
		if in.Host != "" {
			where = append(where, "host = '"+in.Host+"'")
		}
		pattern, e := likePattern(in.Contains)
		if e != nil {
			return q, e
		}
		if pattern != "" {
			where = append(where, "message LIKE '%"+pattern+"%'")
		}
	}
	q.SQL = "SELECT " + cols + " FROM " + table
	if len(where) > 0 {
		q.SQL += " WHERE " + strings.Join(where, " AND ")
	}
	q.SQL += " ORDER BY _timestamp "
	if traces && in.Trace != "" {
		q.SQL += "ASC"
	} else {
		q.SQL += "DESC"
	}
	q.SQL += fmt.Sprintf(" LIMIT %d", q.Limit)
	return q, nil
}
func (r *relay) fleetGate(w http.ResponseWriter) func() {
	select {
	case r.gate <- struct{}{}:
		return func() { <-r.gate }
	default:
		w.Header().Set("Retry-After", "2")
		writeJSON(w, 429, map[string]string{"status": "busy", "error": "too many concurrent reads"})
		return nil
	}
}
func (r *relay) fleetRead(w http.ResponseWriter, req *http.Request, traces bool) {
	media, _, err := mime.ParseMediaType(req.Header.Get("Content-Type"))
	if err != nil || media != "application/json" {
		writeJSON(w, 415, map[string]string{"status": "invalid_request", "error": "application/json required"})
		return
	}
	if req.URL.RawQuery != "" {
		writeJSON(w, 400, map[string]string{"status": "invalid_request", "error": "query parameters are not accepted"})
		return
	}
	defer req.Body.Close()
	var in fleetRead
	d := json.NewDecoder(http.MaxBytesReader(w, req.Body, maxRequestBody))
	d.DisallowUnknownFields()
	if d.Decode(&in) != nil || d.Decode(new(any)) != io.EOF {
		writeJSON(w, 400, map[string]string{"status": "invalid_request", "error": "one typed evidence request required"})
		return
	}
	q, err := buildFleetRead(in, traces, r.now())
	if err != nil {
		writeJSON(w, 400, map[string]string{"status": "invalid_request", "error": strings.TrimPrefix(err.Error(), errInvalid.Error()+": ")})
		return
	}
	release := r.fleetGate(w)
	if release == nil {
		return
	}
	defer release()
	body, err := r.fleetSearch(req.Context(), q, traces)
	if err != nil {
		writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "OpenObserve evidence could not be read; this is not an empty result"})
		return
	}
	if !traces {
		var envelope struct {
			Partial bool `json:"is_partial"`
		}
		if json.Unmarshal(body, &envelope) != nil || envelope.Partial {
			writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "log query returned partial evidence"})
			return
		}
		hits, e := parseHits(body)
		if e != nil {
			writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "log evidence schema is invalid"})
			return
		}
		filtered := []hit{}
		for _, h := range hits {
			if len(filtered) >= q.Limit {
				break
			}
			if in.Container != "" && h.Container != in.Container {
				continue
			}
			if in.Host != "" && h.Host != in.Host {
				continue
			}
			filtered = append(filtered, h)
		}
		out := shape(q, logRequest{Contains: in.Contains, Order: "desc"}, filtered)
		if len(hits) >= q.Limit {
			out.Truncated = true
		}
		writeJSON(w, 200, out)
		return
	}
	out, err := shapeTrace(body, q.Limit)
	if err != nil {
		writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "trace evidence schema is invalid"})
		return
	}
	writeJSON(w, 200, out)
}
func (r *relay) fleetSearch(ctx context.Context, q builtQuery, traces bool) ([]byte, error) {
	payload, _ := json.Marshal(map[string]any{"query": map[string]any{"sql": q.SQL, "start_time": q.StartMicros, "end_time": q.EndMicros, "from": 0, "size": q.Limit}, "search_type": "ui", "timeout": 15})
	endpoint := "/api/" + openObserveOrg + "/_search"
	if traces {
		endpoint += "?type=traces"
	}
	return r.fleetHTTP(ctx, http.MethodPost, endpoint, payload)
}
func (r *relay) fleetHTTP(ctx context.Context, method, path string, body []byte) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, r.upstream+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.SetBasicAuth(r.user, r.password)
	resp, err := r.client.Do(req)
	if err != nil {
		return nil, errors.New("OpenObserve transport failed")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("OpenObserve returned HTTP%d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxUpstreamBody+1))
	if err != nil || len(data) > maxUpstreamBody {
		return nil, errors.New("OpenObserve response exceeds read budget")
	}
	return data, nil
}

type traceRow struct {
	Timestamp  string  `json:"timestamp"`
	Trace      string  `json:"trace_id"`
	Span       string  `json:"span_id"`
	Parent     string  `json:"parent_span_id,omitempty"`
	Service    string  `json:"service_name"`
	Operation  string  `json:"operation_name"`
	Start      string  `json:"start_time_unix_nanos"`
	End        string  `json:"end_time_unix_nanos"`
	Duration   float64 `json:"duration_seconds"`
	Status     string  `json:"status"`
	Method     string  `json:"http_method,omitempty"`
	HTTPStatus string  `json:"http_status,omitempty"`
}
type traceResponse struct {
	Status    string     `json:"status"`
	Stream    string     `json:"stream"`
	Count     int        `json:"count"`
	Limit     int        `json:"limit"`
	Truncated bool       `json:"truncated"`
	Rows      []traceRow `json:"rows"`
}

func scalar(m map[string]json.RawMessage, key string) string {
	v := m[key]
	var s string
	if json.Unmarshal(v, &s) == nil {
		return s
	}
	var n json.Number
	if json.Unmarshal(v, &n) == nil {
		return n.String()
	}
	return ""
}
func safeScalar(s string, n int) string {
	s = strings.Map(func(r rune) rune {
		if r < 32 || r == 127 {
			return -1
		}
		return r
	}, redact(s))
	if len(s) > n {
		s = strings.ToValidUTF8(s[:n], "")
	}
	return s
}
func shapeTrace(body []byte, limit int) (traceResponse, error) {
	out := traceResponse{Status: "ok", Stream: "default", Limit: limit, Rows: []traceRow{}}
	var env struct {
		Hits    []map[string]json.RawMessage `json:"hits"`
		Partial bool                         `json:"is_partial"`
	}
	if json.Unmarshal(body, &env) != nil || env.Hits == nil || env.Partial {
		return out, errors.New("missing or partial trace hits")
	}
	out.Truncated = len(env.Hits) >= limit
	for _, h := range env.Hits {
		if len(out.Rows) >= limit {
			out.Truncated = true
			break
		}
		trace, span := scalar(h, "trace_id"), scalar(h, "span_id")
		if !traceIDRe.MatchString(trace) || !spanIDRe.MatchString(span) {
			return out, errors.New("invalid trace identity")
		}
		start, e1 := strconv.ParseInt(scalar(h, "start_time"), 10, 64)
		end, e2 := strconv.ParseInt(scalar(h, "end_time"), 10, 64)
		at, e3 := strconv.ParseInt(scalar(h, "_timestamp"), 10, 64)
		if e1 != nil || e2 != nil || e3 != nil || start <= 0 || end < start {
			return out, errors.New("invalid trace timing")
		}
		parent := scalar(h, "reference_parent_span_id")
		if !spanIDRe.MatchString(parent) {
			parent = ""
		}
		op := scalar(h, "operation_name")
		if i := strings.IndexByte(op, '?'); i >= 0 {
			op = op[:i]
		}
		status := safeScalar(scalar(h, "span_status"), 20)
		if status != "OK" && status != "ERROR" && status != "UNSET" {
			status = "unknown"
		}
		out.Rows = append(out.Rows, traceRow{Timestamp: time.UnixMicro(at).UTC().Format(time.RFC3339Nano), Trace: trace, Span: span, Parent: parent, Service: safeScalar(scalar(h, "service_name"), 128), Operation: safeScalar(op, 160), Start: strconv.FormatInt(start, 10), End: strconv.FormatInt(end, 10), Duration: float64(end-start) / 1e9, Status: status, Method: safeScalar(scalar(h, "http_request_method"), 16), HTTPStatus: safeScalar(scalar(h, "http_response_status_code"), 8)})
	}
	out.Count = len(out.Rows)
	return out, nil
}

type streamCoverage struct {
	Name      string `json:"name"`
	Type      string `json:"stream_type"`
	Documents int64  `json:"documents"`
	Earliest  string `json:"earliest_at,omitempty"`
	Latest    string `json:"latest_at,omitempty"`
}

func (r *relay) fleetCoverage(w http.ResponseWriter, req *http.Request) {
	if req.URL.RawQuery != "" {
		writeJSON(w, 400, map[string]string{"status": "invalid_request", "error": "query parameters are not accepted"})
		return
	}
	release := r.fleetGate(w)
	if release == nil {
		return
	}
	defer release()
	rows := []streamCoverage{}
	for _, typ := range []string{"logs", "traces"} {
		body, err := r.fleetHTTP(req.Context(), http.MethodGet, "/api/"+openObserveOrg+"/streams?type="+typ+"&fetchSchema=false", nil)
		if err != nil {
			writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "stream coverage could not be read"})
			return
		}
		var env struct {
			List []struct {
				Name  string `json:"name"`
				Type  string `json:"stream_type"`
				Stats struct {
					Docs int64 `json:"doc_num"`
					Min  int64 `json:"doc_time_min"`
					Max  int64 `json:"doc_time_max"`
				} `json:"stats"`
			} `json:"list"`
		}
		if json.Unmarshal(body, &env) != nil || env.List == nil {
			writeJSON(w, 502, map[string]string{"status": "unavailable", "error": "stream coverage schema invalid"})
			return
		}
		for _, s := range env.List {
			if !((s.Type == "logs" && (s.Name == "docker_logs" || s.Name == "host_logs")) || (s.Type == "traces" && s.Name == "default")) {
				continue
			}
			row := streamCoverage{Name: s.Name, Type: s.Type, Documents: s.Stats.Docs}
			if s.Stats.Min > 0 {
				row.Earliest = time.UnixMicro(s.Stats.Min).UTC().Format(time.RFC3339Nano)
			}
			if s.Stats.Max > 0 {
				row.Latest = time.UnixMicro(s.Stats.Max).UTC().Format(time.RFC3339Nano)
			}
			rows = append(rows, row)
		}
	}
	writeJSON(w, 200, map[string]any{"status": "ok", "checked_at": r.now().UTC().Format(time.RFC3339Nano), "streams": rows, "note": "Retained stream metadata only. Missing host/service observations do not establish instrumentation or healthy coverage."})
}
