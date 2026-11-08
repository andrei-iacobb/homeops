package main

import "testing"

func comp(enc string, elem, slot int, dev string) rawComp {
	status := "OK"
	if dev == "" {
		status = "not installed"
	}
	return rawComp{encName: enc, logicalID: enc, elem: elem, slot: slot, dev: dev, status: status}
}

// DL380 topology as read on 2026-11-08: empty elements carry kernel
// placeholder slots that collide with populated bays.
func TestAssignEmptySlotsDL380(t *testing.T) {
	knownSlot = map[string]int{}
	comps := []rawComp{
		comp("1:0:6:0", 0, 1, "sdc"), comp("1:0:6:0", 1, 2, "sdd"), comp("1:0:6:0", 2, 3, "sde"),
		comp("1:0:6:0", 3, 3, ""), comp("1:0:6:0", 4, 4, ""), comp("1:0:6:0", 5, 5, ""),
		comp("1:0:6:0", 6, 6, "sdf"), comp("1:0:6:0", 7, 5, "sdg"),
		comp("2:0:7:0", 0, 1, "sdh"), comp("2:0:7:0", 1, 2, "sdi"), comp("2:0:7:0", 2, 3, "sdj"),
		comp("2:0:7:0", 3, 4, "sdk"), comp("2:0:7:0", 4, 8, "sdl"), comp("2:0:7:0", 5, 7, "sdm"),
		comp("2:0:7:0", 6, 6, ""), comp("2:0:7:0", 7, 7, ""),
		comp("3:0:6:0", 0, 0, ""), comp("3:0:6:0", 1, 1, ""), comp("3:0:6:0", 2, 2, ""),
		comp("3:0:6:0", 3, 4, "sdn"), comp("3:0:6:0", 4, 8, "sdo"), comp("3:0:6:0", 5, 7, "sdp"),
		comp("3:0:6:0", 6, 6, "sdq"), comp("3:0:6:0", 7, 5, "sdr"),
		comp("0:0:3:0", 0, 3, "sda"), comp("0:0:3:0", 1, 4, "sdb"),
	}
	assignEmptySlots(comps)

	want := map[string]map[int]int{ // enc -> elem -> slot
		"1:0:6:0": {0: 1, 1: 2, 2: 3, 3: 4, 4: 8, 5: 7, 6: 6, 7: 5},
		"2:0:7:0": {0: 1, 1: 2, 2: 3, 3: 4, 4: 8, 5: 7, 6: 6, 7: 5},
		"3:0:6:0": {0: 1, 1: 2, 2: 3, 3: 4, 4: 8, 5: 7, 6: 6, 7: 5},
		"0:0:3:0": {0: 3, 1: 4},
	}
	seen := map[string]map[int]bool{}
	for _, c := range comps {
		if got := c.slot; got != want[c.encName][c.elem] {
			t.Errorf("%s elem %d: slot %d, want %d", c.encName, c.elem, got, want[c.encName][c.elem])
		}
		if seen[c.encName] == nil {
			seen[c.encName] = map[int]bool{}
		}
		if seen[c.encName][c.slot] {
			t.Errorf("%s: duplicate slot %d", c.encName, c.slot)
		}
		seen[c.encName][c.slot] = true
	}
}

// With nothing to learn from, empty bays take the lowest free numbers and
// populated bays keep theirs.
func TestAssignEmptySlotsFallback(t *testing.T) {
	knownSlot = map[string]int{}
	comps := []rawComp{
		comp("x", 0, 2, "sda"), comp("x", 1, 2, ""), comp("x", 2, 2, ""),
	}
	assignEmptySlots(comps)
	got := []int{comps[0].slot, comps[1].slot, comps[2].slot}
	if got[0] != 2 || got[1] != 1 || got[2] != 3 {
		t.Fatalf("slots %v, want [2 1 3]", got)
	}
}

// A pulled drive's bay keeps its number via knownSlot (rear cage has no donor).
func TestAssignEmptySlotsRemembersPulledBay(t *testing.T) {
	knownSlot = map[string]int{}
	rear := []rawComp{comp("0:0:3:0", 0, 3, "sda"), comp("0:0:3:0", 1, 4, "sdb")}
	for i := range rear {
		rear[i].logicalID = "rear"
	}
	assignEmptySlots(rear)
	pulled := []rawComp{comp("0:0:3:0", 0, 3, ""), comp("0:0:3:0", 1, 4, "sdb")}
	for i := range pulled {
		pulled[i].logicalID = "rear"
	}
	assignEmptySlots(pulled)
	if pulled[0].slot != 3 {
		t.Fatalf("pulled rear bay got slot %d, want 3", pulled[0].slot)
	}
}
