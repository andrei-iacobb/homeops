// Command bw-agent is the BayWatch host agent. It runs as root via systemd on a
// bare-metal Proxmox host, owns that host's SES enclosure LEDs, polls ZFS+SMART
// health, and serves a small REST+SSE API for the BayWatch UI.
//
// It is the SINGLE writer of every caddy LED on the host (D2 in the plan): it
// reconciles desired LED state (amber = unhealthy from ZFS/SMART, blue = a
// time-boxed locate request) against the kernel SES sysfs every poll, writing
// only on change. This supersedes the standalone drive-health-leds.sh timer.
package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type config struct {
	Bind          string
	Token         string
	HostLabel     string
	Controller    string
	Poll          time.Duration
	SmartPoll     time.Duration
	LocateDefault int
	LocateMax     int
}

func loadConfig() config {
	host, _ := os.Hostname()
	c := config{
		Bind:          envStr("BW_BIND", ":9099"),
		Token:         envStr("BW_TOKEN", ""),
		HostLabel:     envStr("BW_HOST_LABEL", host),
		Controller:    envStr("BW_CONTROLLER", ""),
		Poll:          time.Duration(envInt("BW_POLL", 3)) * time.Second,
		SmartPoll:     time.Duration(envInt("BW_SMART_POLL", 60)) * time.Second,
		LocateDefault: envInt("BW_LOCATE_DEFAULT", 120),
		LocateMax:     envInt("BW_LOCATE_MAX", 600),
	}
	return c
}

type agent struct {
	cfg    config
	health *healthCache
	hub    *hub

	mu          sync.RWMutex
	cur         *Snapshot            // latest published snapshot
	locateUntil map[string]time.Time // compKey -> expiry

	applied map[string]ledState // compKey -> last LED state written to hardware
	cycle   int                 // reconcile counter (for full-sync / re-assert)

	kick chan struct{} // request an immediate reconcile
}

// ledState is the bi-color/locate state the agent has driven onto a bay.
type ledState struct {
	ok    bool // green - present & healthy
	fault bool // amber - ZFS/SMART fault
	ident bool // blue  - locate
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("bw-agent: ")
	cfg := loadConfig()
	if cfg.Token == "" {
		log.Println("WARNING: BW_TOKEN is empty - API auth is DISABLED (LAN-only). Set BW_TOKEN for production.")
	}

	a := &agent{
		cfg:         cfg,
		health:      newHealthCache(),
		hub:         newHub(),
		locateUntil: map[string]time.Time{},
		applied:     map[string]ledState{},
		kick:        make(chan struct{}, 1),
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go a.smartLoop(ctx)
	go a.reconcileLoop(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/healthz", a.handleHealthz)
	mux.HandleFunc("GET /v1/enclosures", a.auth(a.handleEnclosures))
	mux.HandleFunc("GET /v1/stream", a.auth(a.handleStream))
	mux.HandleFunc("POST /v1/locate", a.auth(a.handleLocate))

	srv := &http.Server{
		Addr:              cfg.Bind,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutCtx)
	}()

	log.Printf("listening on %s as host=%q (poll=%s smart=%s)", cfg.Bind, cfg.HostLabel, cfg.Poll, cfg.SmartPoll)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server: %v", err)
	}
}

// --- reconcile -------------------------------------------------------------

func (a *agent) reconcileLoop(ctx context.Context) {
	// Reconcile immediately (no SMART yet) so the API serves within ~1s, then
	// prime SMART asynchronously and reconcile again to fill in temps/identity.
	// SMART across ~25 drives can take many seconds; never block first publish.
	a.reconcile()
	go func() {
		if comps, err := readEnclosures(); err == nil {
			a.health.refreshSmart(devsOf(comps))
			select {
			case a.kick <- struct{}{}:
			default:
			}
		}
	}()
	t := time.NewTicker(a.cfg.Poll)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			a.reconcile()
		case <-a.kick:
			a.reconcile()
		}
	}
}

func (a *agent) smartLoop(ctx context.Context) {
	t := time.NewTicker(a.cfg.SmartPoll)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if comps, err := readEnclosures(); err == nil {
				a.health.refreshSmart(devsOf(comps))
			}
		}
	}
}

// reconcile reads the hardware, computes desired LED state, writes only the
// diffs, and publishes the resulting snapshot + per-slot change events.
func (a *agent) reconcile() {
	comps, err := readEnclosures()
	if err != nil {
		log.Printf("read enclosures: %v", err)
		return
	}
	healthByDev := a.health.snapshot(devsOf(comps))
	now := time.Now()

	// fullSync (first cycle) writes every bit so any stale LED is corrected;
	// reassert (every ~60s) re-affirms the desired "on" bits to heal drift.
	fullSync := a.cycle == 0
	reassert := a.cycle%20 == 0
	a.cycle++

	// Build enclosure grouping + friendly labels (rear vs numbered front boxes).
	encOrder := []string{}        // enclosure names in stable order
	encComps := map[string][]int{} // encName -> indexes into comps
	encLID := map[string]string{}
	for i, c := range comps {
		if _, ok := encComps[c.encName]; !ok {
			encOrder = append(encOrder, c.encName)
			encLID[c.encName] = c.logicalID
		}
		encComps[c.encName] = append(encComps[c.encName], i)
	}
	sort.Strings(encOrder)
	frontOrder := 0
	encLabel := map[string]string{}
	for _, name := range encOrder {
		bays := len(encComps[name])
		if bays > 2 {
			frontOrder++
			encLabel[name] = cageLabel(bays, frontOrder)
		} else {
			encLabel[name] = cageLabel(bays, 0)
		}
	}

	var encs []Enclosure
	var events []SlotEvent
	prev := a.slotIndex() // previous published slots for diffing

	for _, name := range encOrder {
		idxs := encComps[name]
		enc := Enclosure{
			ID:        name,
			LogicalID: encLID[name],
			Label:     encLabel[name],
			Bays:      len(idxs),
		}
		for _, i := range idxs {
			c := &comps[i]
			key := compKey(c.logicalID, c.slot)

			present := c.dev != "" && !strings.EqualFold(c.status, "not installed")
			dh := healthByDev[c.dev]
			bad := false
			reason := ""
			if present {
				bad, reason = dh.bad()
			}

			// Desired bi-color/locate state for this bay:
			//   green (ok)  = drive present and healthy
			//   amber(fault)= drive present and unhealthy
			//   blue (ident)= a time-boxed locate is active
			// The agent is the single writer; we diff against the last-applied
			// state and only shell out to sg_ses on a real change (plus the
			// full-sync / periodic re-assert), so the backplane is never spammed.
			desiredFault := bad
			desiredOK := present && !bad
			desiredLocate := a.locateActive(key, now)
			want := ledState{ok: desiredOK, fault: desiredFault, ident: desiredLocate}
			a.applyLEDs(c, want, fullSync, reassert, reason)

			state := StateHealthy
			switch {
			case !present:
				state = StateEmpty
			case desiredFault:
				state = StateFault
			}

			slot := Slot{
				Slot:        c.slot,
				Comp:        c.comp,
				EnclosureID: c.logicalID,
				Present:     present,
				State:       state,
				Fault:       desiredFault,
				Locate:      desiredLocate,
				LocateUntil: a.locateExpiry(key, now),
				Dev:         c.dev,
				Model:       dh.Model,
				Serial:      dh.Serial,
				Size:        dh.Size,
				TempC:       dh.TempC,
				Smart:       smartLabel(present, dh),
				Zfs:         zfsLabel(dh),
				Pool:        dh.Pool,
				Reason:      reason,
			}
			enc.Slots = append(enc.Slots, slot)

			if changed(prev[key], slot) {
				events = append(events, SlotEvent{Host: a.cfg.HostLabel, EnclosureID: c.logicalID, Slot: slot})
			}
		}
		sort.Slice(enc.Slots, func(i, j int) bool { return enc.Slots[i].Slot < enc.Slots[j].Slot })
		encs = append(encs, enc)
	}

	snap := &Snapshot{
		Host:       a.cfg.HostLabel,
		Controller: a.cfg.Controller,
		TS:         now,
		Enclosures: encs,
	}
	a.mu.Lock()
	a.cur = snap
	a.mu.Unlock()

	for _, ev := range events {
		a.hub.pub(sseFrame("slot", ev))
	}
}

// applyLEDs drives the green/amber/blue bits for one bay via sg_ses, writing a
// bit only when it changes (plus full-sync on first cycle and a periodic
// re-assert of the "on" bits to heal drift). On a write error it keeps the old
// applied value so the next cycle retries.
func (a *agent) applyLEDs(c *rawComp, want ledState, fullSync, reassert bool, reason string) {
	key := compKey(c.logicalID, c.slot)
	prev := a.applied[key]
	apply := func(field string, was, w bool) bool {
		if w == was && !fullSync && !(reassert && w) {
			return was
		}
		if err := setLED(c.sgDev, c.slot, field, w); err != nil {
			log.Printf("setLED %s slot %d %s=%v: %v", c.encName, c.slot, field, w, err)
			return was // retry next cycle
		}
		if w != was {
			log.Printf("led %s slot %d (%s) %s -> %v %s", c.encName, c.slot, devOr(c.dev), field, w, reason)
		}
		return w
	}
	// Clear green before asserting amber (and vice-versa) by ordering ok first.
	a.applied[key] = ledState{
		ok:    apply(ledOK, prev.ok, want.ok),
		fault: apply(ledFault, prev.fault, want.fault),
		ident: apply(ledIdent, prev.ident, want.ident),
	}
}

// --- locate state ----------------------------------------------------------

func compKey(logicalID string, slot int) string { return logicalID + "#" + strconv.Itoa(slot) }

func (a *agent) locateActive(key string, now time.Time) bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	until, ok := a.locateUntil[key]
	return ok && now.Before(until)
}

// locateExpiry returns the locate deadline as of the given reconcile time, so the
// Slot's Locate and LocateUntil fields stay consistent within a single snapshot.
func (a *agent) locateExpiry(key string, now time.Time) *time.Time {
	a.mu.RLock()
	defer a.mu.RUnlock()
	until, ok := a.locateUntil[key]
	if !ok || now.After(until) {
		return nil
	}
	u := until
	return &u
}

func (a *agent) setLocateState(logicalID string, slot, seconds int) {
	key := compKey(logicalID, slot)
	a.mu.Lock()
	if seconds <= 0 {
		delete(a.locateUntil, key)
	} else {
		if seconds > a.cfg.LocateMax {
			seconds = a.cfg.LocateMax
		}
		a.locateUntil[key] = time.Now().Add(time.Duration(seconds) * time.Second)
	}
	a.mu.Unlock()
	select {
	case a.kick <- struct{}{}:
	default:
	}
}

// slotIndex returns the last published slots keyed by compKey for diffing.
func (a *agent) slotIndex() map[string]Slot {
	a.mu.RLock()
	defer a.mu.RUnlock()
	out := map[string]Slot{}
	if a.cur == nil {
		return out
	}
	for _, e := range a.cur.Enclosures {
		for _, s := range e.Slots {
			out[compKey(s.EnclosureID, s.Slot)] = s
		}
	}
	return out
}

// --- HTTP handlers ---------------------------------------------------------

func (a *agent) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if a.cfg.Token != "" {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if subtle.ConstantTimeCompare([]byte(got), []byte(a.cfg.Token)) != 1 {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
		}
		// No CORS header: only bw-ui talks to the agent (server-side, same LAN);
		// browsers never call the agent directly.
		next(w, r)
	}
}

func (a *agent) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(`{"ok":true}`))
}

func (a *agent) handleEnclosures(w http.ResponseWriter, _ *http.Request) {
	a.mu.RLock()
	snap := a.cur
	a.mu.RUnlock()
	if snap == nil {
		http.Error(w, "warming up", http.StatusServiceUnavailable)
		return
	}
	writeJSON(w, snap)
}

func (a *agent) handleStream(w http.ResponseWriter, r *http.Request) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	ch := a.hub.sub()
	defer a.hub.unsub(ch)

	// Send the current full snapshot first so a fresh client renders immediately.
	a.mu.RLock()
	snap := a.cur
	a.mu.RUnlock()
	if snap != nil {
		_, _ = w.Write(sseFrame("snapshot", snap))
		flusher.Flush()
	}

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

func (a *agent) handleLocate(w http.ResponseWriter, r *http.Request) {
	var req LocateRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&req); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.EnclosureID == "" {
		http.Error(w, "enclosure_id required", http.StatusBadRequest)
		return
	}
	if !a.slotExists(req.EnclosureID, req.Slot) {
		http.Error(w, "no such slot", http.StatusNotFound)
		return
	}
	a.setLocateState(req.EnclosureID, req.Slot, req.Seconds)
	w.Header().Set("Content-Type", "application/json")
	_, _ = fmt.Fprintf(w, `{"ok":true,"enclosure_id":%q,"slot":%d,"seconds":%d}`, req.EnclosureID, req.Slot, req.Seconds)
}

func (a *agent) slotExists(logicalID string, slot int) bool {
	a.mu.RLock()
	defer a.mu.RUnlock()
	if a.cur == nil {
		return false
	}
	for _, e := range a.cur.Enclosures {
		if e.LogicalID != logicalID {
			continue
		}
		for _, s := range e.Slots {
			if s.Slot == slot {
				return true
			}
		}
	}
	return false
}

// --- helpers ---------------------------------------------------------------

func devsOf(comps []rawComp) []string {
	out := make([]string, 0, len(comps))
	for _, c := range comps {
		if c.dev != "" {
			out = append(out, c.dev)
		}
	}
	return out
}

func changed(prev, cur Slot) bool {
	return prev.State != cur.State ||
		prev.Fault != cur.Fault ||
		prev.Locate != cur.Locate ||
		prev.Dev != cur.Dev ||
		prev.TempC != cur.TempC ||
		prev.Zfs != cur.Zfs ||
		prev.Smart != cur.Smart ||
		prev.Reason != cur.Reason ||
		(prev.LocateUntil == nil) != (cur.LocateUntil == nil)
}

func smartLabel(present bool, dh driveHealth) string {
	if !present {
		return "-"
	}
	if dh.SmartStr == "" {
		return "-"
	}
	return dh.SmartStr
}

func zfsLabel(dh driveHealth) string {
	if dh.Zfs == "" {
		return "-"
	}
	return dh.Zfs
}

func devOr(dev string) string {
	if dev == "" {
		return "empty"
	}
	return dev
}

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
