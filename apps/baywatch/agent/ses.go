package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
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
	slot      int    // SES slot number (and sg_ses --dev-slot-num)
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
			rc := rawComp{
				encName:   encName,
				logicalID: logicalID,
				sgDev:     sgDev,
				comp:      c.Name(),
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
	sort.Slice(out, func(i, j int) bool {
		if out[i].encName != out[j].encName {
			return out[i].encName < out[j].encName
		}
		return out[i].slot < out[j].slot
	})
	return out, nil
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
func setLED(sgDev string, slot int, field string, on bool) error {
	if sgDev == "" {
		return nil
	}
	flag := "--clear=" + field
	if on {
		flag = "--set=" + field
	}
	return exec.Command("sg_ses", "--dev-slot-num="+strconv.Itoa(slot), flag, sgDev).Run()
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
