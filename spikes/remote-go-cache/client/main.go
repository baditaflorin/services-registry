// ci-gocache is a GOCACHEPROG client for CI that FAILS OPEN: if the shared cache
// server is down, slow, black-holed or returns errors, every lookup is just a cache
// miss and the build carries on. It is a thin layer over bradfitz/go-tool-cache's
// HTTPClient (BestEffortHTTP) adding what that package's own command lacks:
// short network timeouts and a circuit breaker.
package main

import (
	"bytes"
	"context"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

	"github.com/bradfitz/go-tool-cache/cacheproc"
	"github.com/bradfitz/go-tool-cache/cachers"
)

// quietLog drops the library's per-request "error PUT/GET" lines (thousands per build when
// the server is down) and counts them, so CI logs stay readable. Everything else passes.
type quietLog struct {
	w       io.Writer
	dropped atomic.Int64
}

func (q *quietLog) Write(p []byte) (int, error) {
	if bytes.Contains(p, []byte("error PUT")) || bytes.Contains(p, []byte("error GET")) {
		q.dropped.Add(1)
		return len(p), nil
	}
	return q.w.Write(p)
}

func main() {
	var (
		dir       = flag.String("cache-dir", "", "local cache directory (first layer); default under the user cache dir")
		server    = flag.String("cache-server", "", "shared cache server, scheme and authority only; empty = local disk only")
		token     = flag.String("access-token", "", "optional bearer token for the server")
		quiet     = flag.Bool("quiet", false, "do not print the one-line summary at the end")
		dialTO    = flag.Duration("dial-timeout", 500*time.Millisecond, "TCP connect timeout to the server")
		reqTO     = flag.Duration("request-timeout", 3*time.Second, "per-request timeout")
		threshold = flag.Int("breaker-failures", 3, "consecutive failures that open the circuit")
		cooldown  = flag.Duration("breaker-cooldown", 30*time.Second, "how long the circuit stays open")
		maxPuts   = flag.Int("max-async-puts", 8, "concurrent background uploads")
		putTO     = flag.Duration("put-timeout", 30*time.Second, "timeout of one background upload")
	)
	flag.Parse()
	ql := &quietLog{w: os.Stderr}
	log.SetOutput(ql)

	if *dir == "" {
		d, err := os.UserCacheDir()
		if err != nil {
			log.Fatal(err)
		}
		*dir = filepath.Join(d, "ci-gocache")
	}
	if err := os.MkdirAll(*dir, 0o755); err != nil {
		log.Fatal(err)
	}

	dc := &cachers.DiskCache{Dir: *dir}
	p := &cacheproc.Process{Get: dc.Get, Put: dc.Put}
	var br *breaker
	var hc *cachers.HTTPClient

	summary := func() error {
		if !*quiet {
			tripped, skipped := 0, 0
			if br != nil {
				tripped, skipped = br.stats()
			}
			log.Printf("ci-gocache: %d gets (%d hits, %d misses, %d errors); %d puts; breaker trips=%d skipped=%d; %d routine error lines suppressed",
				p.Gets.Load(), p.GetHits.Load(), p.GetMisses.Load(), p.GetErrors.Load(), p.Puts.Load(), tripped, skipped, ql.dropped.Load())
		}
		return nil
	}
	p.Close = summary

	if *server != "" {
		br = &breaker{
			rt: &http.Transport{
				DialContext:           (&net.Dialer{Timeout: *dialTO}).DialContext,
				ResponseHeaderTimeout: *reqTO,
				MaxIdleConnsPerHost:   64,
				IdleConnTimeout:       60 * time.Second,
			},
			threshold: *threshold,
			cooldown:  *cooldown,
		}
		hc = &cachers.HTTPClient{
			BaseURL:               *server,
			Disk:                  dc,
			AccessToken:           *token,
			BestEffortHTTP:        true,
			AsyncPutMaxConcurrent: *maxPuts,
			AsyncPutTimeout:       func(int64) time.Duration { return *putTO },
			HTTPClient:            &http.Client{Transport: br, Timeout: *reqTO},
		}
		p.Get = hc.Get
		p.Put = hc.Put
		p.Close = func() error {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			hc.Shutdown(ctx)
			return summary()
		}
	}

	if err := p.Run(); err != nil {
		log.Fatal(err)
	}
}
