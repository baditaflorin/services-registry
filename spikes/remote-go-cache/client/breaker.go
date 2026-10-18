package main

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"time"
)

var errCircuitOpen = errors.New("ci-gocache: circuit open, skipping remote cache")

// breaker stops calling a failing server for a cool-down, so a dead or
// black-holed server costs microseconds per lookup instead of a timeout each.
// Context cancellation (the go command abandoning a lookup) is not a server failure.
type breaker struct {
	rt        http.RoundTripper
	threshold int
	cooldown  time.Duration
	// slow, when positive, makes a response that takes longer than this to START
	// (time to response headers, so large downloads are not penalised) count as a
	// failure. A shared cache over a slow link is worse than no cache: measured, each
	// lookup costs about one round trip, so an 80 ms link made a 48 s build take 80 s.
	slow time.Duration
	now  func() time.Time // injectable for tests; time.Now when nil

	mu        sync.Mutex
	fails     int
	openUntil time.Time
	trips     int
	skipped   int
}

func (b *breaker) clock() time.Time {
	if b.now != nil {
		return b.now()
	}
	return time.Now()
}

func (b *breaker) RoundTrip(r *http.Request) (*http.Response, error) {
	b.mu.Lock()
	if b.clock().Before(b.openUntil) {
		b.skipped++
		b.mu.Unlock()
		return nil, errCircuitOpen
	}
	b.mu.Unlock()

	started := b.clock()
	resp, err := b.rt.RoundTrip(r)
	took := b.clock().Sub(started)

	b.mu.Lock()
	defer b.mu.Unlock()
	switch {
	case err != nil && !errors.Is(err, context.Canceled):
		b.fails++
	case err == nil && resp.StatusCode >= 500:
		b.fails++
	case err == nil && b.slow > 0 && took > b.slow:
		b.fails++
	case err == nil:
		b.fails = 0
	}
	if b.fails >= b.threshold {
		b.openUntil = b.clock().Add(b.cooldown)
		b.fails = 0
		b.trips++
	}
	return resp, err
}

func (b *breaker) stats() (trips, skipped int) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.trips, b.skipped
}
