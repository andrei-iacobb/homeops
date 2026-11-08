package main

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// enclosureRoot is the kernel SCSI Enclosure Services sysfs interface. We read
// topology (slot, status, installed block device, enclosure logical_id) from
// here. LED control, however, goes through sg_ses against the enclosure's
// scsi_generic device, because the GREEN/"OK" state has no sysfs file - only
// fault/locate do. We proved live on the HPE Gen9 H240/P440ar backplanes (HBA
// mode) that sg_ses --set=ok lights GREEN, --set=fault lights AMBER, and
// --set=ident lights BLUE, despite ssacli refusing LED control in HBA mode and
// the common "green is impossible in HBA mode" folklore.
const enclosureRoot = "/sys/class/enclosure"

// SES Array Device Slot control fields we drive (sg_ses acronyms).
const (
	ledOK    = "ok"    // green  - drive present & healthy
	ledFault = "fault" // amber  - ZFS/SMART fault
	ledIdent = "ident" // blue   - locate / identify
)

// rawComp is a discovered SES element (one physical bay) plus the sg device that
// controls its LEDs.
type rawComp struct {
	encName   string // sysfs enclosure dir, e.g. 2:0:8:0
	logicalID string // enclosure logical_id, e.g. 0x50014380435ccf80
	sgDev     string // this enclosure's SES device, e.g. /dev/sg13
	comp      string // basename, e.g. ArrayElement0006
	elem      int    // Array Device Slot element index (sg_ses --index=arr,N)
	slot      int    // bay number; see assignEmptySlots for empty bays
	status    string // OK | not installed | ...
	dev       string // installed block device basename, e.g. sdk ("" if empty)
}

// readEnclosures walks the SES sysfs tree and returns every bay across every
// controller on this host, including the sg device used to drive its LEDs.
func readEnclosures() ([]rawComp, error) {
	encDirs, err := os.ReadDir(enclosureRoot)
	if err != nil {
		return nil, err
	}
	var out []rawComp
	for _, e := range encDirs {
		encName := e.Name()
		encPath := filepath.Join(enclosureRoot, encName)
		logicalID := strings.TrimSpace(readFile(filepath.Join(encPath, "id")))
		sgDev := resolveSgDev(encPath)

		comps, err := os.ReadDir(encPath)
		if err != nil {
			continue
		}
		for _, c := range comps {
			compDir := filepath.Join(encPath, c.Name())
			slotStr := readFile(filepath.Join(compDir, "slot"))
			if slotStr == "" {
				continue // not a bay component (enclosure/power/temp elements)
			}
			slot, err := strconv.Atoi(strings.TrimSpace(slotStr))
			if err != nil {
				continue
			}
			elem, err := strconv.Atoi(strings.TrimPrefix(c.Name(), "ArrayElement"))
			if err != nil {
				continue
			}
			rc := rawComp{
				encName:   encName,
				logicalID: logicalID,
				sgDev:     sgDev,
				comp:      c.Name(),
				elem:      elem,
				slot:      slot,
				status:    strings.TrimSpace(readFile(filepath.Join(compDir, "status"))),
			}
			blockDir := filepath.Join(compDir, "device", "block")
			if entries, err := os.ReadDir(blockDir); err == nil && len(entries) > 0 {
				rc.dev = entries[0].Name()
			}
			out = append(out, rc)
		}
	}
	assignEmptySlots(out)
	sort.Slice(out, func(i, j int) bool {
		if out[i].encName != out[j].encName {
			return out[i].encName < out[j].encName
		}
		return out[i].slot < out[j].slot
	})
	return out, nil
}

// assignEmptySlots gives every empty bay a slot number that is unique within its
// enclosure. The H240 backplanes only report a device slot number for populated
// elements; for empty ones the kernel's sysfs "slot" is a placeholder that can
// equal a populated bay's number (seen on DL380: empty element 5 and the drive in
// element 7 both read slot 5). Slot is the bay's identity in keys, the UI and the
// locate API, so a collision made the agent flip that drive's LED every cycle.
//
// An empty element takes the bay number a same-size enclosure reports for the
// same element index (the cages share one element->bay wiring), falling back to
// the lowest unused number from 1. knownSlot remembers every bay number seen on a
// populated element (keyed by logical_id + element), so a bay keeps its number
// after its drive is pulled, including in the rear cage that has no same-size
// donor. readEnclosures runs from the reconcile and SMART goroutines, hence the lock.
var (
	knownMu   sync.Mutex
	knownSlot = map[string]int{}
)

func elemKey(logicalID string, elem int) string {
	return logicalID + "#e" + strconv.Itoa(elem)
}

func assignEmptySlots(comps []rawComp) {
	knownMu.Lock()
	defer knownMu.Unlock()
	isEmpty := func(c rawComp) bool {
		return c.dev == "" || strings.EqualFold(c.status, "not installed")
	}
	size := map[string]int{}
	for _, c := range comps {
		size[c.encName]++
	}
	// learned[bays][elem] = bay numbers reported by populated elements
	learned := map[int]map[int][]int{}
	for _, c := range comps {
		if isEmpty(c) {
			continue
		}
		n := size[c.encName]
		if learned[n] == nil {
			learned[n] = map[int][]int{}
		}
		learned[n][c.elem] = append(learned[n][c.elem], c.slot)
		knownSlot[elemKey(c.logicalID, c.elem)] = c.slot
	}
	used := map[string]map[int]bool{}
	for _, c := range comps {
		if !isEmpty(c) {
			if used[c.encName] == nil {
				used[c.encName] = map[int]bool{}
			}
			used[c.encName][c.slot] = true
		}
	}
	idx := make([]int, 0, len(comps))
	for i, c := range comps {
		if isEmpty(c) {
			idx = append(idx, i)
		}
	}
	sort.Slice(idx, func(a, b int) bool {
		ca, cb := comps[idx[a]], comps[idx[b]]
		if ca.encName != cb.encName {
			return ca.encName < cb.encName
		}
		return ca.elem < cb.elem
	})
	// Pass 1: this bay's own remembered number, then numbers learned from
	// same-size cages. Pass 2: lowest free number for the rest.
	done := map[int]bool{}
	for _, i := range idx {
		c := &comps[i]
		if used[c.encName] == nil {
			used[c.encName] = map[int]bool{}
		}
		candidates := learned[size[c.encName]][c.elem]
		if s, ok := knownSlot[elemKey(c.logicalID, c.elem)]; ok {
			candidates = append([]int{s}, candidates...)
		}
		for _, s := range candidates {
			if !used[c.encName][s] {
				c.slot = s
				used[c.encName][s] = true
				done[i] = true
				break
			}
		}
	}
	for _, i := range idx {
		if done[i] {
			continue
		}
		c := &comps[i]
		s := 1
		for used[c.encName][s] {
			s++
		}
		c.slot = s
		used[c.encName][s] = true
	}
}

// resolveSgDev finds the scsi_generic device for an enclosure, e.g.
// /sys/class/enclosure/1:0:9:0/device/scsi_generic/sg13 -> /dev/sg13.
func resolveSgDev(encPath string) string {
	dir := filepath.Join(encPath, "device", "scsi_generic")
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) == 0 {
		return ""
	}
	return "/dev/" + entries[0].Name()
}

// setLED drives one bi-color/locate state on one bay via sg_ses. field is one of
// ledOK / ledFault / ledIdent. Callers diff against last-applied state so this
// only runs on a real change (plus a periodic re-assert), never per-cycle spam.
func setLED(sgDev string, elem int, field string, on bool) error {
	if sgDev == "" {
		return errors.New("enclosure has no sg device")
	}
	flag := "--clear=" + field
	if on {
		flag = "--set=" + field
	}
	// Address by element index, not --dev-slot-num: empty elements have no
	// device slot number, so slot-number addressing cannot reach them and can
	// hit the wrong bay.
	return exec.Command("sg_ses", "--index=arr,"+strconv.Itoa(elem), flag, sgDev).Run()
}

func readFile(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(b)
}

// cageLabel derives a friendly label for a cage. The HPE silkscreen box number
// is not exposed over SES, so we label rear 2-bay cages explicitly and number
// the front 8-bay cages 1..N in stable order. order is the cage index among
// same-size cages.
func cageLabel(bays, order int) string {
	switch {
	case bays <= 2:
		return "Rear cage"
	default:
		return "Front box " + strconv.Itoa(order)
	}
}
