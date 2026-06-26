// Command bw-ui is the BayWatch aggregator. It runs in Kubernetes, keeps a live
// SSE connection to each bw-agent on the LAN, merges them into one fleet view,
// serves an embedded SVG-chassis frontend, fans agent changes out to browsers
// over SSE, and proxies time-boxed locate requests to the owning agent.
//
// It touches no hardware. If it dies, the agents keep driving health LEDs.
package main

import (
	"bytes"
	"context"
	"embed"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

//go:embed static
var staticFS embed.FS

type config struct {
	Bind          string
	Token         string
	Agents        []agentConn
	LocateDefault int
}

func loadConfig() config {
	c := config{
		Bind:          envStr("BW_BIND", ":8080"),
		Token:         envStr("BW_TOKEN", ""),
		Agents:        parseAgents(envStr("BW_AGENTS", "")),
		LocateDefault: envInt("BW_LOCATE_DEFAULT", 120),
	}
	return c
}

// parseAgents reads "label=url,label=url" into agentConn entries.
func parseAgents(s string) []agentConn {
	var out []agentConn
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		label, url, ok := strings.Cut(part, "=")
		if !ok {
			log.Printf("ignoring malformed BW_AGENTS entry %q (want label=url)", part)
			continue
		}
		out = append(out, agentConn{label: strings.TrimSpace(label), url: strings.TrimRight(strings.TrimSpace(url), "/")})
	}
	return out
}

type server struct {
	cfg   config
	fleet *fleet
	hub   *hub
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("bw-ui: ")
	cfg := loadConfig()
	if len(cfg.Agents) == 0 {
		log.Println("WARNING: BW_AGENTS is empty - no host agents configured")
	}
	if cfg.Token == "" {
		log.Println("WARNING: BW_TOKEN empty - calling agents without auth")
	}

	h := newHub()
	s := &server{cfg: cfg, fleet: newFleet(cfg.Token, h), hub: h}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	s.fleet.run(ctx, cfg.Agents)

	sub, _ := fs.Sub(staticFS, "static")
	mux := http.NewServeMux()
	mux.Handle("GET /", http.FileServer(http.FS(sub)))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(`{"ok":true}`)) })
	mux.HandleFunc("GET /api/config", s.handleConfig)
	mux.HandleFunc("GET /api/fleet", s.handleFleet)
	mux.HandleFunc("GET /api/stream", s.handleStream)
	mux.HandleFunc("POST /api/locate", s.handleLocate)

	srv := &http.Server{Addr: cfg.Bind, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		sc, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(sc)
	}()

	log.Printf("listening on %s, %d agent(s)", cfg.Bind, len(cfg.Agents))
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server: %v", err)
	}
}

func (s *server) handleConfig(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]any{"locate_default": s.cfg.LocateDefault})
}

func (s *server) handleFleet(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, s.fleet.snapshot())
}

func (s *server) handleStream(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	ch := s.hub.sub()
	defer s.hub.unsub(ch)

	// Initial full fleet so a fresh browser renders immediately.
	_, _ = w.Write(sseFrame("fleet", s.fleet.snapshot()))
	flusher.Flush()

	ka := time.NewTicker(20 * time.Second)
	defer ka.Stop()
	ctx := r.Context()
	for {
		select {
		case <-ctx.Done():
			return
		case msg := <-ch:
			if _, err := w.Write(msg); err != nil {
				return
			}
			flusher.Flush()
		case <-ka.C:
			if _, err := w.Write([]byte(": keepalive\n\n")); err != nil {
				return
			}
			flusher.Flush()
		}
	}
}

func (s *server) handleLocate(w http.ResponseWriter, r *http.Request) {
	var req LocateRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.Host == "" || req.EnclosureID == "" {
		http.Error(w, "host and enclosure_id required", http.StatusBadRequest)
		return
	}
	url, ok := s.fleet.urlForHost(req.Host)
	if !ok {
		http.Error(w, "unknown or offline host", http.StatusNotFound)
		return
	}
	body, _ := json.Marshal(map[string]any{
		"enclosure_id": req.EnclosureID,
		"slot":         req.Slot,
		"seconds":      req.Seconds,
	})
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	areq, _ := http.NewRequestWithContext(ctx, http.MethodPost, url+"/v1/locate", bytes.NewReader(body))
	areq.Header.Set("Content-Type", "application/json")
	if s.cfg.Token != "" {
		areq.Header.Set("Authorization", "Bearer "+s.cfg.Token)
	}
	resp, err := http.DefaultClient.Do(areq)
	if err != nil {
		http.Error(w, "agent unreachable", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, io.LimitReader(resp.Body, 1<<16))
}

// --- helpers ---

func sseFrame(event string, v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		return []byte("event: error\ndata: {}\n\n")
	}
	return []byte("event: " + event + "\ndata: " + string(b) + "\n\n")
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func envStr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
