FROM golang:1.27.3-alpine@sha256:f2c872bcee949b7be9c490efd02b48a8a2fbf947cdf533f1c58525ec9d8170fd AS builder

ARG VCS_REF=unknown
WORKDIR /src
COPY go.mod VERSION ./
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w -X main.relayCommit=${VCS_REF}" -o /out/oo-relay .

FROM scratch
# scratch has no CA bundle; the relay calls https://openobserve.0own.com and must verify it.
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=builder /out/oo-relay /oo-relay
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/oo-relay"]
