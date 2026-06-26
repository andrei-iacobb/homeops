package main

import "time"

// Wire contract (BayWatch v1). The agent serves these; bw-ui consumes them.
// JSON is the contract between agent and UI - keep field names stable.

// SlotState is the rendered LED/health state of a bay.
const (
	StateHealthy = "healthy" // drive present, ZFS ONLINE + SMART ok -> green (fault off)
	StateFault   = "fault"   // drive present but ZFS not-ONLINE/errors or SMART failing -> amber
	StateEmpty   = "empty"   // no drive in this bay (SES element exists, still locatable)
	StateUnknown = "unknown" // present but health not yet evaluated
)

// Slot is one physical bay, anchored on (EnclosureID logical_id, Slot number).
// The anchor is stable even when the drive is pulled/dead - that is the whole
// point of the locator (D1 in the plan).
type Slot struct {
	Slot        int        `json:"slot"`         // SES slot number (silkscreen bay)
	Comp        string     `json:"comp"`         // sysfs component dir, e.g. ArrayElement0006
	EnclosureID string     `json:"enclosure_id"` // enclosure logical_id, e.g. 0x50014380435ccf80
	Present     bool       `json:"present"`      // a drive is installed
	State       string     `json:"state"`        // one of the State* constants
	Fault       bool       `json:"fault"`        // amber fault LED commanded on
	Locate      bool       `json:"locate"`       // blue locate LED commanded on
	LocateUntil *time.Time `json:"locate_until"` // when the locate auto-clears (nil if off)
	Dev         string     `json:"dev"`          // current block device, e.g. sdk ("" if empty)
	Model       string     `json:"model"`        // drive model
	Serial      string     `json:"serial"`       // drive serial (correlation anchor for health)
	Size        string     `json:"size"`         // human size, e.g. 1.6T
	TempC       int        `json:"temp_c"`       // drive temperature in C (0 if unknown)
	Smart       string     `json:"smart"`        // PASSED|FAILED|OK|- (overall SMART health)
	Zfs         string     `json:"zfs"`          // ONLINE|DEGRADED|FAULTED|... or - (not in a pool)
	Pool        string     `json:"pool"`         // ZFS pool name ("" if not a pool member)
	Reason      string     `json:"reason"`       // why fault (empty when healthy)
}

// Enclosure is one drive cage (an SES enclosure).
type Enclosure struct {
	ID        string `json:"id"`         // sysfs enclosure name, e.g. 2:0:8:0
	LogicalID string `json:"logical_id"` // stable SES logical_id, e.g. 0x50014380435ccf80
	Label     string `json:"label"`      // friendly cage label
	Bays      int    `json:"bays"`       // number of slots in this cage
	Slots     []Slot `json:"slots"`      // ordered by slot number
}

// Snapshot is the full agent view returned by GET /v1/enclosures and pushed as
// the first SSE event.
type Snapshot struct {
	Host       string      `json:"host"`       // host label, e.g. DL380G9
	Controller string      `json:"controller"` // controller summary, e.g. "Smart HBA H240 x3 + H240ar"
	TS         time.Time   `json:"ts"`         // when this snapshot was produced
	Enclosures []Enclosure `json:"enclosures"`
}

// SlotEvent is one incremental SSE change pushed on GET /v1/stream after the
// initial snapshot. The UI applies it by (Host, EnclosureID, Slot.Slot).
type SlotEvent struct {
	Host        string `json:"host"`
	EnclosureID string `json:"enclosure_id"`
	Slot        Slot   `json:"slot"`
}

// LocateRequest is the body of POST /v1/locate. Seconds is clamped to LocateMax;
// 0 means "clear the locate now".
type LocateRequest struct {
	EnclosureID string `json:"enclosure_id"` // logical_id of the cage
	Slot        int    `json:"slot"`         // slot number in that cage
	Seconds     int    `json:"seconds"`      // duration; 0 = clear
}
