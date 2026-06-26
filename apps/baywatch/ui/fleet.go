package main

import (
	"bufio"
	"context"
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"
)

// agentConn is one configured bw-agent endpoint.
type agentConn struct {
	label string // configured display label (fallback before first snapshot)
	url   string // base URL, e.g. http://192.168.1.150:9099
}

// fleet holds the merged, live view of every host and streams changes to the
// browser hub. Each agent gets a goroutine that keeps an SSE connection open,
// applies snapshot+slot events into the model, and republishes to browsers.
type fleet struct {
	token  string
	hub    *hub
	client *http.Client

	mu        sync.RWMutex
	hosts     map[string]*HostState // keyed by reported host name
	hostURL   map[string]string     // reported host -> agent base URL (for locate)
	labelHost map[string]string     // configured label -> reported host
}

func newFleet(token string, h *hub) *fleet {
	return &fleet{
		token:     token,
		hub:       h,
		client:    &http.Client{Timeout: 0}, // SSE is long-lived; per-request timeouts set below
		hosts:     map[string]*HostState{},
		hostURL:   map[string]string{},
		labelHost: map[string]string{},
	}
}

func (f *fleet) run(ctx context.Context, agents []agentConn) {
	for _, a := range agents {
		go f.connectLoop(ctx, a)
	}
}

// connectLoop keeps one agent's SSE stream connected, reconnecting with backoff.
func (f *fleet) connectLoop(ctx context.Context, a agentConn) {
	backoff := time.Second
	for {
		if ctx.Err() != nil {
			return
		}
		err := f.stream(ctx, a)
		if ctx.Err() != nil {
			return
		}
		f.markOffline(a)
		if err != nil {
			log.Printf("agent %s (%s) disconnected: %v", a.label, a.url, err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		if backoff < 15*time.Second {
			backoff *= 2
		}
	}
}

func (f *fleet) stream(ctx context.Context, a agentConn) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.url+"/v1/stream", nil)
	if err != nil {
		return err
	}
	if f.token != "" {
		req.Header.Set("Authorization", "Bearer "+f.token)
	}
	resp, err := f.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return &httpError{resp.StatusCode}
	}

	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	var event, data string
	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == "":
			if data != "" {
				f.dispatch(a, event, data)
			}
			event, data = "", ""
		case strings.HasPrefix(line, ":"):
			// comment / keepalive
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(line[len("event:"):])
		case strings.HasPrefix(line, "data:"):
			if data != "" {
				data += "\n" // SSE spec: multiple data: lines join with newline
			}
			data += strings.TrimSpace(line[len("data:"):])
		}
	}
	return sc.Err()
}

func (f *fleet) dispatch(a agentConn, event, data string) {
	switch event {
	case "snapshot":
		var snap Snapshot
		if err := json.Unmarshal([]byte(data), &snap); err != nil {
			log.Printf("bad snapshot from %s: %v", a.label, err)
			return
		}
		f.applySnapshot(a, snap)
	case "slot":
		var ev SlotEvent
		if err := json.Unmarshal([]byte(data), &ev); err != nil {
			return
		}
		f.applySlot(ev)
	}
}

func (f *fleet) applySnapshot(a agentConn, snap Snapshot) {
	hs := &HostState{
		Host:       snap.Host,
		Controller: snap.Controller,
		Online:     true,
		LastSeen:   time.Now(),
		Enclosures: snap.Enclosures,
	}
	hs.recount()
	f.mu.Lock()
	f.hosts[snap.Host] = hs
	f.hostURL[snap.Host] = a.url
	f.labelHost[a.label] = snap.Host
	f.mu.Unlock()
	f.hub.pub(sseFrame("host", HostEvent{Host: *hs}))
}

func (f *fleet) applySlot(ev SlotEvent) {
	f.mu.Lock()
	hs := f.hosts[ev.Host]
	if hs == nil {
		f.mu.Unlock()
		return
	}
	hs.LastSeen = time.Now()
	hs.Online = true
	for ei := range hs.Enclosures {
		if hs.Enclosures[ei].LogicalID != ev.EnclosureID {
			continue
		}
		for si := range hs.Enclosures[ei].Slots {
			if hs.Enclosures[ei].Slots[si].Slot == ev.Slot.Slot {
				hs.Enclosures[ei].Slots[si] = ev.Slot
			}
		}
	}
	hs.recount()
	f.mu.Unlock()
	f.hub.pub(sseFrame("slot", ev))
}

func (f *fleet) markOffline(a agentConn) {
	f.mu.Lock()
	host := f.labelHost[a.label]
	hs := f.hosts[host]
	if hs != nil {
		hs.Online = false
	}
	var snapshot *HostState
	if hs != nil {
		c := *hs
		snapshot = &c
	}
	f.mu.Unlock()
	if snapshot != nil {
		f.hub.pub(sseFrame("host", HostEvent{Host: *snapshot}))
	}
}

// snapshot returns a deep-ish copy of the merged fleet for GET /api/fleet.
func (f *fleet) snapshot() Fleet {
	f.mu.RLock()
	defer f.mu.RUnlock()
	out := Fleet{TS: time.Now()}
	for _, hs := range f.hosts {
		out.Hosts = append(out.Hosts, *hs)
	}
	// stable order by host name
	for i := 0; i < len(out.Hosts); i++ {
		for j := i + 1; j < len(out.Hosts); j++ {
			if out.Hosts[j].Host < out.Hosts[i].Host {
				out.Hosts[i], out.Hosts[j] = out.Hosts[j], out.Hosts[i]
			}
		}
	}
	return out
}

// urlForHost returns the agent base URL serving a given reported host.
func (f *fleet) urlForHost(host string) (string, bool) {
	f.mu.RLock()
	defer f.mu.RUnlock()
	u, ok := f.hostURL[host]
	return u, ok
}

func (hs *HostState) recount() {
	hs.Drives, hs.Faults, hs.Locates, hs.Empty = 0, 0, 0, 0
	for _, e := range hs.Enclosures {
		for _, s := range e.Slots {
			if s.Present {
				hs.Drives++
			} else {
				hs.Empty++
			}
			if s.Fault {
				hs.Faults++
			}
			if s.Locate {
				hs.Locates++
			}
		}
	}
}

type httpError struct{ code int }

func (e *httpError) Error() string { return "agent returned HTTP " + http.StatusText(e.code) }
