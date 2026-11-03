// oo-relay is a generic read-only relay for the fleet's OpenObserve
// `docker_logs` stream. OpenObserve OSS has no read-only role, so the root
// password stays on VM 630 and this service is the only thing that holds it.
// Callers get exactly one operation: fetch log lines of one container in a
// bounded time range. They cannot send SQL, pick an organization or stream, or
// write anything. See README.md and docs/operations/openobserve-log-relay.md.
package main

import (
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	listenDefault  = ":8080"
	openObserveURL = "https://openobserve.0own.com"
	maxTokens      = 8
)

func main() {
	tokenPath := os.Getenv("OO_RELAY_TOKEN_FILE")
	passwordPath := os.Getenv("OPENOBSERVE_PASSWORD_FILE")
	if tokenPath == "" || passwordPath == "" {
		log.Fatal("OO_RELAY_TOKEN_FILE and OPENOBSERVE_PASSWORD_FILE are required")
	}
	password, err := readSecret(passwordPath)
	if err != nil {
		log.Fatalf("read OpenObserve password: %v", err)
	}
	tokens, err := readTokens(tokenPath, string(password))
	if err != nil {
		log.Fatalf("read relay tokens: %v", err)
	}
	user := os.Getenv("OPENOBSERVE_USER")
	if user == "" {
		user = "admin@0docker.com"
	}
	listen := os.Getenv("OO_RELAY_LISTEN")
	if listen == "" {
		listen = listenDefault
	}

	r := newRelay(tokens, openObserveURL, user, string(password))
	server := &http.Server{
		Addr:              listen,
		Handler:           r.routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      45 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    8 << 10,
	}
	log.Printf("OpenObserve read-only log relay listening on %s (%d token(s))", listen, len(tokens))
	log.Fatal(server.ListenAndServe())
}

func readSecret(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return []byte(strings.TrimRight(string(b), "\r\n")), nil
}

// readTokens reads one to maxTokens bearer tokens, one per line, each 32 random
// bytes as 64 hex characters. More than one token lets a rotation overlap.
func readTokens(path, openObservePassword string) ([][]byte, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var tokens [][]byte
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if len(line) != 64 {
			return nil, fmt.Errorf("each token must be 32 random bytes as 64 hex characters")
		}
		if _, err := hex.DecodeString(line); err != nil {
			return nil, fmt.Errorf("each token must be 32 random bytes as 64 hex characters")
		}
		if subtle.ConstantTimeCompare([]byte(line), []byte(openObservePassword)) == 1 {
			return nil, fmt.Errorf("a relay token must never equal the OpenObserve password")
		}
		tokens = append(tokens, []byte(line))
	}
	if len(tokens) == 0 || len(tokens) > maxTokens {
		return nil, fmt.Errorf("expected 1 to %d tokens, found %d", maxTokens, len(tokens))
	}
	return tokens, nil
}
