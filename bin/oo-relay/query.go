package main

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// Hard caps. They are constants on purpose: a caller can lower them, never raise them.
const (
	stream         = "docker_logs"
	maxSpan        = 7 * 24 * time.Hour
	maxFutureSkew  = time.Minute
	defaultLimit   = 100
	maxLimit       = 500
	maxContainsLen = 256 // bytes
)

// Container and host names are matched exactly, so they only need the characters
// Docker and the fleet actually use. This charset has no quote, backslash,
// whitespace or SQL punctuation, so these two values cannot alter the SQL.
var nameRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

// logRequest is the whole caller-controlled surface. Unknown JSON fields (sql,
// stream, org, ...) are rejected by the decoder, not ignored.
type logRequest struct {
	ContainerName string `json:"container_name"`
	Host          string `json:"host"`
	Contains      string `json:"contains"`
	Start         string `json:"start"` // RFC3339, required
	End           string `json:"end"`   // RFC3339, optional, default now
	Limit         int    `json:"limit"`
	Order         string `json:"order"` // "asc" (default) or "desc"
}

// builtQuery is what the relay sends to OpenObserve, plus what it needs to
// re-check the answer.
type builtQuery struct {
	SQL         string
	StartMicros int64
	EndMicros   int64
	Limit       int
	Contains    string // exact, case-sensitive substring the rows are re-checked against
}

var errInvalid = errors.New("invalid request")

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", errInvalid, fmt.Sprintf(format, args...))
}

// buildQuery validates a request and renders the one SQL shape the relay is
// allowed to send. now is a parameter so tests can pin the clock.
func buildQuery(req logRequest, now time.Time) (builtQuery, error) {
	var q builtQuery

	if !nameRe.MatchString(req.ContainerName) {
		return q, invalid("container_name is required and may only contain letters, digits, '_', '.', '-' (max 128)")
	}
	if req.Host != "" && !nameRe.MatchString(req.Host) {
		return q, invalid("host may only contain letters, digits, '_', '.', '-' (max 128)")
	}

	start, err := parseTime("start", req.Start)
	if err != nil {
		return q, err
	}
	end := now
	if req.End != "" {
		if end, err = parseTime("end", req.End); err != nil {
			return q, err
		}
	}
	if !end.After(start) {
		return q, invalid("end must be after start")
	}
	if end.After(now.Add(maxFutureSkew)) {
		return q, invalid("end is in the future")
	}
	if end.Sub(start) > maxSpan {
		return q, invalid("time range is longer than %d days; page through it with several requests", int(maxSpan/(24*time.Hour)))
	}

	limit := req.Limit
	switch {
	case limit == 0:
		limit = defaultLimit
	case limit < 0 || limit > maxLimit:
		return q, invalid("limit must be between 1 and %d", maxLimit)
	}

	order := strings.ToLower(req.Order)
	switch order {
	case "":
		order = "asc"
	case "asc", "desc":
	default:
		return q, invalid("order must be asc or desc")
	}

	pattern, err := likePattern(req.Contains)
	if err != nil {
		return q, err
	}

	where := "container_name = '" + req.ContainerName + "'"
	if req.Host != "" {
		where += " AND host = '" + req.Host + "'"
	}
	if pattern != "" {
		where += " AND message LIKE '%" + pattern + "%'"
	}
	q.SQL = "SELECT _timestamp, container_name, host, message FROM " + stream +
		" WHERE " + where + " ORDER BY _timestamp " + strings.ToUpper(order) +
		fmt.Sprintf(" LIMIT %d", limit)
	q.StartMicros = start.UnixMicro()
	q.EndMicros = end.UnixMicro()
	q.Limit = limit
	q.Contains = req.Contains
	return q, nil
}

func parseTime(field, value string) (time.Time, error) {
	if value == "" {
		return time.Time{}, invalid("%s is required (RFC3339, e.g. 2026-11-03T12:00:00Z)", field)
	}
	t, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		return time.Time{}, invalid("%s must be RFC3339, e.g. 2026-11-03T12:00:00Z", field)
	}
	if t.Year() < 2020 {
		return time.Time{}, invalid("%s is implausibly old", field)
	}
	return t, nil
}

// likePattern turns the caller's substring into the inside of a SQL string
// literal used in LIKE '%...%'.
//
//   - control characters and invalid UTF-8 are rejected;
//   - a single quote is doubled, the standard SQL escape;
//   - '%', '_' and '\' are LIKE metacharacters (and '\' may also be a string
//     escape in some SQL dialects), so each becomes '_', the one-character
//     wildcard. The result can only match more rows than the exact substring,
//     never fewer; the relay drops the extras by re-checking every row for the
//     exact substring (see server.go). No backslash ever reaches the SQL.
func likePattern(s string) (string, error) {
	if s == "" {
		return "", nil
	}
	if len(s) > maxContainsLen {
		return "", invalid("contains is longer than %d bytes", maxContainsLen)
	}
	if !utf8.ValidString(s) {
		return "", invalid("contains is not valid UTF-8")
	}
	var b strings.Builder
	for _, r := range s {
		switch {
		case r < 0x20 || r == 0x7f:
			return "", invalid("contains must not contain control characters")
		case r == '\'':
			b.WriteString("''")
		case r == '%' || r == '_' || r == '\\':
			b.WriteByte('_')
		default:
			b.WriteRune(r)
		}
	}
	return b.String(), nil
}
