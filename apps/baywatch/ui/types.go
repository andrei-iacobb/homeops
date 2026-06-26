package main

import "time"

// These mirror the bw-agent wire contract (apps/baywatch/agent/types.go). The
// agent serves them; the UI consumes and re-aggregates them. Keep in sync.

type Slot struct {
	Slot        int        `json:"slot"`
	Comp        string     `json:"comp"`
	EnclosureID string     `json:"enclosure_id"`
	Present     bool       `json:"present"`
	State       string     `json:"state"`
	Fault       bool       `json:"fault"`
	Locate      bool       `json:"locate"`
	LocateUntil *time.Time `json:"locate_until"`
	Dev         string     `json:"dev"`
	Model       string     `json:"model"`
	Serial      string     `json:"serial"`
	Size        string     `json:"size"`
	TempC       int        `json:"temp_c"`
	Smart       string     `json:"smart"`
	Zfs         string     `json:"zfs"`
	Pool        string     `json:"pool"`
	Reason      string     `json:"reason"`
}

type Enclosure struct {
	ID        string `json:"id"`
	LogicalID string `json:"logical_id"`
	Label     string `json:"label"`
	Bays      int    `json:"bays"`
	Slots     []Slot `json:"slots"`
}

// Snapshot is what an agent returns from /v1/enclosures and pushes as its first
// SSE event.
type Snapshot struct {
	Host       string      `json:"host"`
	Controller string      `json:"controller"`
	TS         time.Time   `json:"ts"`
	Enclosures []Enclosure `json:"enclosures"`
}

// SlotEvent is an incremental change pushed by an agent on /v1/stream.
type SlotEvent struct {
	Host        string `json:"host"`
	EnclosureID string `json:"enclosure_id"`
	Slot        Slot   `json:"slot"`
}

// --- UI aggregate types (what the browser consumes) ---

// HostState is one host's view in the merged fleet, plus liveness metadata the
// agent snapshot does not carry.
type HostState struct {
	Host       string      `json:"host"`
	Controller string      `json:"controller"`
	Online     bool        `json:"online"`
	LastSeen   time.Time   `json:"last_seen"`
	Enclosures []Enclosure `json:"enclosures"`
	// summary counts for the header
	Drives  int `json:"drives"`
	Faults  int `json:"faults"`
	Locates int `json:"locates"`
	Empty   int `json:"empty"`
}

// Fleet is the full merged view returned by GET /api/fleet and pushed as the
// first browser SSE event.
type Fleet struct {
	TS    time.Time   `json:"ts"`
	Hosts []HostState `json:"hosts"`
}

// HostEvent is pushed to the browser when a host's liveness/snapshot changes.
type HostEvent struct {
	Host HostState `json:"host"`
}

// LocateRequest is the browser -> UI body for POST /api/locate, and the UI ->
// agent body (minus Host) for the agent's POST /v1/locate.
type LocateRequest struct {
	Host        string `json:"host"`
	EnclosureID string `json:"enclosure_id"`
	Slot        int    `json:"slot"`
	Seconds     int    `json:"seconds"`
}
