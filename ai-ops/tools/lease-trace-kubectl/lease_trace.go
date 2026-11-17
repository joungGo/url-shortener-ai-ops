// Local diagnostic patch for k8s.io/client-go v0.34.1, package exec.
// No addresses, headers, credentials, URLs or error strings enter the event type.
package exec

import (
	"crypto/tls"
	"encoding/json"
	"net/http"
	"net/http/httptrace"
	"os"
	"regexp"
	"sync"
	"sync/atomic"
	"time"
)

const leaseTracePrefix = "C04_LEASE_STAGE_V1 "

var leaseTraceID = regexp.MustCompile(`^[a-f0-9]{32}$`)
var leaseTraceOnce sync.Once
var leaseTraceQueue = make(chan leaseStageEvent, 256)

type leaseStageEvent struct {
	Version       int    `json:"version"`
	RequestID     string `json:"request_id"`
	Sequence      uint64 `json:"sequence"`
	Phase         string `json:"phase"`
	State         string `json:"state"`
	ElapsedMicros int64  `json:"elapsed_us"`
	Dropped       uint64 `json:"dropped"`
	Reused        *bool  `json:"reused,omitempty"`
}

type leaseTrace struct {
	id      string
	started time.Time
	seq     atomic.Uint64
	dropped atomic.Uint64
	emit    func(leaseStageEvent) bool
}

func queueLeaseStage(e leaseStageEvent) bool {
	leaseTraceOnce.Do(func() {
		go func() {
			for event := range leaseTraceQueue {
				encoded, err := json.Marshal(event)
				if err == nil {
					_, _ = os.Stderr.Write(append(append([]byte(leaseTracePrefix), encoded...), '\n'))
				}
			}
		}()
	})
	select {
	case leaseTraceQueue <- e:
		return true
	default:
		return false
	}
}

func newLeaseTrace(req *http.Request) *leaseTrace {
	if req.Header.Get("X-C04-Lease-Trace") != "1" || !leaseTraceID.MatchString(req.Header.Get("Audit-ID")) ||
		(req.Method != "GET" && req.Method != "PUT") ||
		req.URL.Path != "/apis/coordination.k8s.io/v1/namespaces/ai-ops-lab/leases/b6-scenario-lock" {
		return nil
	}
	return &leaseTrace{id: req.Header.Get("Audit-ID"), started: time.Now(), emit: queueLeaseStage}
}

func (t *leaseTrace) stage(phase, state string, reused *bool) {
	if t == nil {
		return
	}
	e := leaseStageEvent{1, t.id, t.seq.Add(1), phase, state, time.Since(t.started).Microseconds(), t.dropped.Load(), reused}
	// Never wait for logging, retry a request, or change its context deadline.
	if !t.emit(e) {
		t.dropped.Add(1)
	}
}

func (t *leaseTrace) finish(phase string, err error) {
	state := "done"
	if err != nil {
		state = "failed"
	}
	t.stage(phase, state, nil)
}

func (t *leaseTrace) hooks() *httptrace.ClientTrace {
	return &httptrace.ClientTrace{
		DNSStart:          func(httptrace.DNSStartInfo) { t.stage("dns", "start", nil) },
		DNSDone:           func(i httptrace.DNSDoneInfo) { t.finish("dns", i.Err) },
		ConnectStart:      func(string, string) { t.stage("tcp", "start", nil) },
		ConnectDone:       func(_, _ string, err error) { t.finish("tcp", err) },
		TLSHandshakeStart: func() { t.stage("tls", "start", nil) },
		TLSHandshakeDone:  func(_ tls.ConnectionState, err error) { t.finish("tls", err) },
		GetConn:           func(string) { t.stage("connection", "start", nil) },
		GotConn:           func(i httptrace.GotConnInfo) { t.stage("connection", "done", &i.Reused) },
		WroteRequest: func(i httptrace.WroteRequestInfo) {
			t.finish("request_write", i.Err)
			if i.Err == nil {
				t.stage("response_wait", "start", nil)
			}
		},
		GotFirstResponseByte: func() { t.stage("response_wait", "done", nil) },
	}
}

func (t *leaseTrace) withContext(req *http.Request) *http.Request {
	if t == nil {
		return req
	}
	return req.WithContext(httptrace.WithClientTrace(req.Context(), t.hooks()))
}
