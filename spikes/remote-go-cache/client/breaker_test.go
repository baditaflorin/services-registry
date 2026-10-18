package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type fakeRT struct {
	calls  int
	err    error
	status int
}

func (f *fakeRT) RoundTrip(*http.Request) (*http.Response, error) {
	f.calls++
	if f.err != nil {
		return nil, f.err
	}
	return &http.Response{StatusCode: f.status, Body: io.NopCloser(strings.NewReader(""))}, nil
}

func req() *http.Request { r, _ := http.NewRequest("GET", "http://cache.invalid/x", nil); return r }

func TestOpensAfterThresholdAndSkipsTheServer(t *testing.T) {
	f := &fakeRT{err: errors.New("connection refused")}
	b := &breaker{rt: f, threshold: 3, cooldown: time.Hour}
	for i := 0; i < 3; i++ {
		if _, err := b.RoundTrip(req()); err == nil {
			t.Fatal("expected the transport error")
		}
	}
	for i := 0; i < 5; i++ {
		if _, err := b.RoundTrip(req()); !errors.Is(err, errCircuitOpen) {
			t.Fatalf("call %d: want circuit open, got %v", i, err)
		}
	}
	if f.calls != 3 {
		t.Fatalf("server was called %d times, want exactly 3", f.calls)
	}
	if trips, skipped := b.stats(); trips != 1 || skipped != 5 {
		t.Fatalf("trips=%d skipped=%d, want 1 and 5", trips, skipped)
	}
}

func TestClosesAfterCooldownAndProbesAgain(t *testing.T) {
	now := time.Unix(1000, 0)
	f := &fakeRT{err: errors.New("down")}
	b := &breaker{rt: f, threshold: 2, cooldown: 30 * time.Second, now: func() time.Time { return now }}
	b.RoundTrip(req())
	b.RoundTrip(req())
	if _, err := b.RoundTrip(req()); !errors.Is(err, errCircuitOpen) {
		t.Fatal("should be open")
	}
	now = now.Add(31 * time.Second)
	f.err, f.status = nil, 200
	resp, err := b.RoundTrip(req())
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("after the cooldown the server must be tried again: %v", err)
	}
}

func TestSuccessResetsTheFailureCount(t *testing.T) {
	f := &fakeRT{err: errors.New("blip")}
	b := &breaker{rt: f, threshold: 3, cooldown: time.Hour}
	b.RoundTrip(req())
	b.RoundTrip(req())
	f.err, f.status = nil, 200
	b.RoundTrip(req())
	f.err = errors.New("blip")
	b.RoundTrip(req())
	b.RoundTrip(req())
	if _, err := b.RoundTrip(req()); errors.Is(err, errCircuitOpen) {
		t.Fatal("two failures after a success must not open a threshold-3 breaker")
	}
}

func TestCancellationIsNotAServerFailure(t *testing.T) {
	f := &fakeRT{err: context.Canceled}
	b := &breaker{rt: f, threshold: 2, cooldown: time.Hour}
	for i := 0; i < 10; i++ {
		b.RoundTrip(req())
	}
	if trips, _ := b.stats(); trips != 0 {
		t.Fatal("the go command abandoning lookups must not trip the breaker")
	}
}

func TestServerErrorsCountButClientErrorsDoNot(t *testing.T) {
	f := &fakeRT{status: 503}
	b := &breaker{rt: f, threshold: 2, cooldown: time.Hour}
	b.RoundTrip(req())
	b.RoundTrip(req())
	if trips, _ := b.stats(); trips != 1 {
		t.Fatalf("5xx must count: trips=%d", trips)
	}
	g := &fakeRT{status: 404}
	c := &breaker{rt: g, threshold: 2, cooldown: time.Hour}
	for i := 0; i < 10; i++ {
		c.RoundTrip(req())
	}
	if trips, _ := c.stats(); trips != 0 {
		t.Fatal("a cache miss (404) is normal and must not trip the breaker")
	}
}

func TestQuietLogDropsPerRequestErrorsButCountsThem(t *testing.T) {
	var out strings.Builder
	q := &quietLog{w: &out}
	q.Write([]byte("error PUT /a/b: circuit open\n"))
	q.Write([]byte("error GET /a: boom\n"))
	q.Write([]byte("something important\n"))
	if got := out.String(); got != "something important\n" {
		t.Fatalf("only non-routine lines should pass through, got %q", got)
	}
	if q.dropped.Load() != 2 {
		t.Fatalf("dropped=%d, want 2", q.dropped.Load())
	}
}
