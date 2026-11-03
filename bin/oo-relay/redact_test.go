package main

import (
	"strings"
	"testing"
)

func TestRedact(t *testing.T) {
	secrets := []string{
		"ak_0123456789abcdef0123456789abcdef",
		"li_0123456789abcdef0123456789abcdef",
		"fb_0123456789abcdef0123456789abcdef",
		"ghp_0123456789abcdef0123456789abcdef",
		"github_pat_11ABCDEFG0123456789_abcdefghijkl",
		"tskey-auth-kABCDEF1234CNTRL-abcdefghijklmnop",
		"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
		"Bearer abcdefghijklmnopqrstuvwxyz012345",
	}
	keyed := []string{"hunter2hunter2"} // only recognisable next to a key name
	lines := []string{
		"auth failed for key %s",
		"Authorization: Bearer %s",
		`{"token":"%s"}`,
		"password=%s retry",
		"api_key: %s",
		"dial postgres://app:%s@db:5432/x",
	}
	secrets = append(secrets, keyed...)
	for _, line := range lines {
		for _, s := range secrets {
			if strings.Contains(line, "key %s") || strings.HasPrefix(line, "Authorization") {
				if s == keyed[0] {
					continue
				}
			}
			in := strings.ReplaceAll(line, "%s", s)
			if out := redact(in); strings.Contains(out, s) {
				t.Errorf("secret survived: %q -> %q", in, out)
			}
		}
	}
}

func TestRedactLeavesOrdinaryLines(t *testing.T) {
	for _, in := range []string{
		"[ext-traits-backfill] started run=42 domains=1000 batch=50",
		`level=info msg="request" status=200 path=/v1/domains tokens=5`,
		"max_tokens=4096",
		"bank_account_id=12 task_id=ak_ short",
	} {
		if out := redact(in); out != in {
			t.Errorf("changed %q -> %q", in, out)
		}
	}
}
