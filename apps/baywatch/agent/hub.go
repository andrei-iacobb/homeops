package main

import "sync"

// hub is a tiny fan-out for Server-Sent Events. The reconcile loop publishes
// pre-framed SSE messages; each connected /v1/stream client gets its own
// buffered channel. Slow clients drop messages rather than blocking the loop
// (state is idempotent and re-synced on the next snapshot/poll).
type hub struct {
	mu   sync.Mutex
	subs map[chan []byte]struct{}
}

func newHub() *hub {
	return &hub{subs: make(map[chan []byte]struct{})}
}

func (h *hub) sub() chan []byte {
	ch := make(chan []byte, 64)
	h.mu.Lock()
	h.subs[ch] = struct{}{}
	h.mu.Unlock()
	return ch
}

func (h *hub) unsub(ch chan []byte) {
	h.mu.Lock()
	if _, ok := h.subs[ch]; ok {
		delete(h.subs, ch)
		close(ch)
	}
	h.mu.Unlock()
}

func (h *hub) pub(msg []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for ch := range h.subs {
		select {
		case ch <- msg:
		default: // drop for slow consumers
		}
	}
}
