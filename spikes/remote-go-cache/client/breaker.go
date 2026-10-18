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
	now       func() time.Time // injectable for tests; time.Now when nil

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

	resp, err := b.rt.RoundTrip(r)

	b.mu.Lock()
	defer b.mu.Unlock()
	switch {
	case err != nil && !errors.Is(err, context.Canceled):
		b.fails++
	case err == nil && resp.StatusCode >= 500:
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
