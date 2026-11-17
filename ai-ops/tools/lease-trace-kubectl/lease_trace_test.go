package exec

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptrace"
	"strings"
	"testing"
	"time"
)

func TestLeaseTracePendingStagesAndSecretExclusion(t *testing.T) {
	for _, phase := range []string{"authentication", "dns", "tcp", "tls", "response_wait"} {
		t.Run(phase, func(t *testing.T) {
			events := []leaseStageEvent{}
			tr := &leaseTrace{id: strings.Repeat("a", 32), started: time.Now(), emit: func(e leaseStageEvent) bool { events = append(events, e); return true }}
			h := tr.hooks()
			switch phase {
			case "authentication":
				tr.stage(phase, "start", nil)
			case "dns":
				h.DNSStart(httptrace.DNSStartInfo{Host: "SECRET-HOST"})
			case "tcp":
				h.ConnectStart("tcp", "SECRET-ADDRESS")
			case "tls":
				h.TLSHandshakeStart()
			case "response_wait":
				h.WroteRequest(httptrace.WroteRequestInfo{})
			}
			if len(events) == 0 || events[len(events)-1].Phase != phase || events[len(events)-1].State != "start" {
				t.Fatal("pending phase lost")
			}
			tr.finish(phase, errors.New("SECRET-TOKEN"))
			h.DNSDone(httptrace.DNSDoneInfo{Err: errors.New("SECRET-DNS")})
			h.TLSHandshakeDone(tls.ConnectionState{ServerName: "SECRET-SERVER"}, errors.New("SECRET-TLS"))
			raw, _ := json.Marshal(events)
			if strings.Contains(string(raw), "SECRET") {
				t.Fatal("secret leaked")
			}
		})
	}
}

func TestLeaseTraceScopeAndDeadline(t *testing.T) {
	req, _ := http.NewRequest("GET", "https://localhost/apis/coordination.k8s.io/v1/namespaces/ai-ops-lab/leases/b6-scenario-lock", nil)
	if newLeaseTrace(req) != nil {
		t.Fatal("trace without opt in")
	}
	req.Header.Set("X-C04-Lease-Trace", "1")
	req.Header.Set("Audit-ID", strings.Repeat("a", 32))
	tr := newLeaseTrace(req)
	if tr == nil {
		t.Fatal("trace missing")
	}
	ctx, cancel := context.WithTimeout(req.Context(), 3*time.Second)
	defer cancel()
	req = req.WithContext(ctx)
	wrapped := tr.withContext(req)
	before, _ := req.Context().Deadline()
	after, _ := wrapped.Context().Deadline()
	if before != after || wrapped.Method != req.Method || wrapped.URL != req.URL {
		t.Fatal("request behavior changed")
	}
	req.Header.Set("Audit-ID", "SECRET\nforged")
	if newLeaseTrace(req) != nil {
		t.Fatal("untrusted id")
	}
	req.Header.Set("Audit-ID", strings.Repeat("a", 32))
	req.Method = "DELETE"
	if newLeaseTrace(req) != nil {
		t.Fatal("unsupported method")
	}
}

func TestLeaseTraceOverflowDoesNotBlock(t *testing.T) {
	tr := &leaseTrace{id: strings.Repeat("a", 32), started: time.Now(), emit: func(leaseStageEvent) bool { return false }}
	start := time.Now()
	for i := 0; i < 1000; i++ {
		tr.stage("dns", "start", nil)
	}
	if tr.dropped.Load() != 1000 || time.Since(start) > time.Second {
		t.Fatal("diagnostic backpressure")
	}
	var last leaseStageEvent
	tr.emit = func(e leaseStageEvent) bool { last = e; return true }
	tr.stage("tls", "start", nil)
	if last.Dropped != 1000 || last.Sequence != 1001 {
		t.Fatal("loss not visible")
	}
}

func TestLeaseTraceNilIsIdentity(t *testing.T) {
	var tr *leaseTrace
	req, _ := http.NewRequest("GET", "http://localhost", nil)
	if tr.withContext(req) != req {
		t.Fatal("disabled trace changed request")
	}
	tr.stage("dns", "start", nil)
	tr.finish("dns", nil)
}
