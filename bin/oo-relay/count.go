package main

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Counts are for questions such as "how many POST /v1/captures per day did
// this container serve", which the 500-row /v1/logs cannot answer. They return
// numbers only, never a message, so they expose less than /v1/logs does.
//
// Every bucket is one plain `SELECT COUNT(*) ... WHERE ...` over its own time
// window (the same query shape the trace census uses), so the relay does not
// depend on OpenObserve's histogram() output format. The price is one upstream
// query per bucket, which is why the number of buckets is capped.
const (
	maxDayBuckets  = 8  // a 7-day span that does not start at midnight touches 8 UTC days
	maxHourBuckets = 25 // a 24-hour span that does not start on the hour touches 25 hours
	maxHourSpan    = 24 * time.Hour
)

type countRequest struct {
	ContainerName string `json:"container_name"`
	Host          string `json:"host"`
	Contains      string `json:"contains"`
	Start         string `json:"start"`
	End           string `json:"end"`
	Bucket        string `json:"bucket"` // "day" (default, UTC days) or "hour" (UTC hours, span at most 24 h)
}

type countBucket struct {
	Start string `json:"start"` // bucket start, UTC; the first bucket starts at the requested start
	End   string `json:"end"`   // bucket end, UTC; the last bucket ends at the requested end
	Count int64  `json:"count"`
}

type countResponse struct {
	Status  string        `json:"status"`
	Stream  string        `json:"stream,omitempty"`
	Bucket  string        `json:"bucket,omitempty"`
	Total   int64         `json:"total"`
	Exact   bool          `json:"exact"`
	Buckets []countBucket `json:"buckets"`
	Error   string        `json:"error,omitempty"`
}

type countWindow struct{ From, To time.Time }

type countPlan struct {
	Where   string
	Bucket  string
	Windows []countWindow
	Exact   bool
}

// buildCount validates a request and works out the buckets. Names, times and
// the span follow the same rules as /v1/logs. The substring is matched with the
// same LIKE pattern; when it contains '%', '_' or '\' the pattern is looser than
// the substring and the relay cannot re-check rows it never reads, so the
// answer is marked "exact": false and is an upper bound.
func buildCount(req countRequest, now time.Time) (countPlan, error) {
	var p countPlan
	if !nameRe.MatchString(req.ContainerName) {
		return p, invalid("container_name is required and may only contain letters, digits, '_', '.', '-' (max 128)")
	}
	if req.Host != "" && !nameRe.MatchString(req.Host) {
		return p, invalid("host may only contain letters, digits, '_', '.', '-' (max 128)")
	}
	start, err := parseTime("start", req.Start)
	if err != nil {
		return p, err
	}
	end := now
	if req.End != "" {
		if end, err = parseTime("end", req.End); err != nil {
			return p, err
		}
	}
	if !end.After(start) {
		return p, invalid("end must be after start")
	}
	if end.After(now.Add(maxFutureSkew)) {
		return p, invalid("end is in the future")
	}
	if end.Sub(start) > maxSpan {
		return p, invalid("time range is longer than %d days", int(maxSpan/(24*time.Hour)))
	}

	p.Bucket = strings.ToLower(req.Bucket)
	var step time.Duration
	var limit int
	switch p.Bucket {
	case "", "day":
		p.Bucket, step, limit = "day", 24*time.Hour, maxDayBuckets
	case "hour":
		if end.Sub(start) > maxHourSpan {
			return p, invalid("bucket hour needs a time range of at most 24 hours")
		}
		step, limit = time.Hour, maxHourBuckets
	default:
		return p, invalid("bucket must be day or hour")
	}

	start, end = start.UTC(), end.UTC()
	for from := start; from.Before(end); {
		to := from.Truncate(step).Add(step)
		if to.After(end) {
			to = end
		}
		p.Windows = append(p.Windows, countWindow{From: from, To: to})
		from = to
	}
	if len(p.Windows) > limit {
		return p, invalid("too many buckets")
	}

	pattern, err := likePattern(req.Contains)
	if err != nil {
		return p, err
	}
	p.Exact = !strings.ContainsAny(req.Contains, `%_\`)
	p.Where = "container_name = '" + req.ContainerName + "'"
	if req.Host != "" {
		p.Where += " AND host = '" + req.Host + "'"
	}
	if pattern != "" {
		p.Where += " AND message LIKE '%" + pattern + "%'"
	}
	return p, nil
}

func (r *relay) logCount(w http.ResponseWriter, req *http.Request) {
	defer req.Body.Close()
	var in countRequest
	decoder := json.NewDecoder(io.LimitReader(req.Body, maxRequestBody))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&in); err != nil {
		writeJSON(w, http.StatusBadRequest, countResponse{Status: "invalid_request", Error: "body must be one JSON object with only: container_name, host, contains, start, end, bucket"})
		return
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		writeJSON(w, http.StatusBadRequest, countResponse{Status: "invalid_request", Error: "request must contain one JSON object"})
		return
	}
	plan, err := buildCount(in, r.now())
	if err != nil {
		writeJSON(w, http.StatusBadRequest, countResponse{Status: "invalid_request", Error: strings.TrimPrefix(err.Error(), errInvalid.Error()+": ")})
		return
	}

	select {
	case r.gate <- struct{}{}:
		defer func() { <-r.gate }()
	default:
		w.Header().Set("Retry-After", "2")
		writeJSON(w, http.StatusTooManyRequests, countResponse{Status: "busy", Error: "too many concurrent queries; retry shortly"})
		return
	}

	out := countResponse{Status: "ok", Stream: stream, Bucket: plan.Bucket, Exact: plan.Exact, Buckets: []countBucket{}}
	for _, win := range plan.Windows {
		n, err := r.countWindow(req, plan.Where, win)
		if err != nil {
			log.Printf("count failed container=%s host=%s: %v", in.ContainerName, in.Host, err)
			writeJSON(w, http.StatusBadGateway, countResponse{Status: "unavailable", Error: "OpenObserve query unavailable; this is not a zero count"})
			return
		}
		out.Total += n
		out.Buckets = append(out.Buckets, countBucket{
			Start: win.From.Format(time.RFC3339Nano), End: win.To.Format(time.RFC3339Nano), Count: n,
		})
	}
	log.Printf("count container=%s host=%s contains_len=%d buckets=%d total=%d remote=%s",
		in.ContainerName, in.Host, len(in.Contains), len(out.Buckets), out.Total, req.RemoteAddr)
	writeJSON(w, http.StatusOK, out)
}

// countWindow asks OpenObserve for one number. A partial or malformed answer is
// an error, never a count.
func (r *relay) countWindow(req *http.Request, where string, win countWindow) (int64, error) {
	q := builtQuery{
		SQL:         "SELECT COUNT(*) AS n FROM " + stream + " WHERE " + where,
		StartMicros: win.From.UnixMicro(),
		EndMicros:   win.To.UnixMicro(),
		Limit:       1,
	}
	body, err := r.searchRaw(req.Context(), q)
	if err != nil {
		return 0, err
	}
	var env struct {
		Hits    []map[string]json.RawMessage `json:"hits"`
		Partial bool                         `json:"is_partial"`
	}
	if json.Unmarshal(body, &env) != nil || env.Partial || len(env.Hits) != 1 {
		return 0, errors.New("count answer is missing, partial or has more than one row")
	}
	n, err := strconv.ParseInt(scalar(env.Hits[0], "n"), 10, 64)
	if err != nil || n < 0 {
		return 0, errors.New("count answer is not a non-negative integer")
	}
	return n, nil
}
