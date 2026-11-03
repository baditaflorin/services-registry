package main

import "regexp"

// Best-effort masking of credential shapes in returned log lines. The relay
// hands log text to every agent, and the fleet's logs are not guaranteed to be
// clean, so the known token formats are masked on the way out. This is a
// second line of defence: logs must still not contain credentials (see
// docs/observability/CENTRAL-OPENOBSERVE.md).
var redactions = []struct {
	re   *regexp.Regexp
	with string
}{
	{regexp.MustCompile(`\b(?:ak|li|fb)_[A-Za-z0-9]{20,}`), "<redacted:fleet-key>"},
	{regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{20,}`), "<redacted:github-token>"},
	{regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}`), "<redacted:github-token>"},
	{regexp.MustCompile(`\btskey-[A-Za-z0-9-]{10,}`), "<redacted:tailscale-key>"},
	{regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`), "<redacted:jwt>"},
	{regexp.MustCompile(`(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}`), "$1 <redacted>"},
	{regexp.MustCompile(`(?i)(://[^/\s:@]+:)[^/\s@]+@`), "${1}<redacted>@"},
	{regexp.MustCompile(`(?i)\b(password|passwd|secret|token|api[_-]?key)(["']?\s*[:=]\s*["']?)[^\s"',;&]{4,}`), "${1}${2}<redacted>"},
}

func redact(s string) string {
	for _, r := range redactions {
		s = r.re.ReplaceAllString(s, r.with)
	}
	return s
}
