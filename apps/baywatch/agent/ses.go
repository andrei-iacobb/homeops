package main

import (
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// enclosureRoot is the kernel SCSI Enclosure Services sysfs interface. Each
// enclosure exposes per-bay component dirs with fault/locate/status/slot files
// and a `device/block/<sdX>` symlink to the installed drive. We proved live on
// the HPE Gen9 H240/P440ar backplanes that writing these files physically drives
// the caddy LEDs even in HBA mode (where ssacli refuses).
const enclosureRoot = "/sys/class/enclosure"

// rawComp is a discovered SES element (one physical bay) plus its live sysfs state.
type rawComp struct {
	encName   string // sysfs enclosure dir, e.g. 2:0:8:0
	logicalID string // enclosure logical_id, e.g. 0x50014380435ccf80
	compDir   string // absolute path to the component dir
	comp      string // basename, e.g. ArrayElement0006
	slot      int    // SES slot number
	status    string // OK | not installed | ...
	fault     bool   // current fault sysfs bit
	locate    bool   // current locate sysfs bit
	dev       string // installed block device basename, e.g. sdk ("" if empty)
}

// readEnclosures walks the SES sysfs tree and returns every bay across every
// controller on this host. Reads are cheap (pure sysfs), so the reconcile loop
// can call this every cycle.
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

		comps, err := os.ReadDir(encPath)
		if err != nil {
			continue
		}
		for _, c := range comps {
			compDir := filepath.Join(encPath, c.Name())
			slotStr := readFile(filepath.Join(compDir, "slot"))
			if slotStr == "" {
				continue // not a bay component (e.g. enclosure/power/temp elements)
			}
			slot, err := strconv.Atoi(strings.TrimSpace(slotStr))
			if err != nil {
				continue
			}
			rc := rawComp{
				encName:   encName,
				logicalID: logicalID,
				compDir:   compDir,
				comp:      c.Name(),
				slot:      slot,
				status:    strings.TrimSpace(readFile(filepath.Join(compDir, "status"))),
				fault:     sysfsBitOn(readFile(filepath.Join(compDir, "fault"))),
				locate:    sysfsBitOn(readFile(filepath.Join(compDir, "locate"))),
			}
			// device/block/<sdX> -> installed drive
			blockDir := filepath.Join(compDir, "device", "block")
			if entries, err := os.ReadDir(blockDir); err == nil && len(entries) > 0 {
				rc.dev = entries[0].Name()
			}
			out = append(out, rc)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].encName != out[j].encName {
			return out[i].encName < out[j].encName
		}
		return out[i].slot < out[j].slot
	})
	return out, nil
}

// setFault commands the amber fault LED. We only ever write on a real change
// (the caller diffs), so this never spams the backplane.
func setFault(compDir string, on bool) error {
	return writeBit(filepath.Join(compDir, "fault"), on)
}

// setLocate commands the blue locate/identify LED.
func setLocate(compDir string, on bool) error {
	return writeBit(filepath.Join(compDir, "locate"), on)
}

func writeBit(path string, on bool) error {
	v := "0"
	if on {
		v = "1"
	}
	return os.WriteFile(path, []byte(v), 0)
}

// sysfsBitOn normalizes the enclosure LED files. The kernel enclosure driver
// reads a "fault" of 1 back as 6 (it ORs in the FAULT_SENSED/REQSTD bits), and
// trailing newlines are common - so treat any nonzero as on.
func sysfsBitOn(s string) bool {
	s = strings.TrimSpace(s)
	return s != "" && s != "0"
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
// the front 8-bay cages 1..N in stable logical_id order. order is the cage index
// among same-size cages.
func cageLabel(bays, order int) string {
	switch {
	case bays <= 2:
		return "Rear cage"
	default:
		return "Front box " + strconv.Itoa(order)
	}
}
