package main

import (
	"bufio"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
)

// driveHealth is the evaluated health + identity of one block device.
type driveHealth struct {
	Zfs      string // ONLINE|DEGRADED|FAULTED|UNAVAIL|OFFLINE|REMOVED or "" if not in a pool
	Pool     string // pool name ("" if not a member)
	ZfsErr   bool   // nonzero read/write/cksum error counters
	SmartStr string // PASSED|FAILED|OK|"" (overall SMART health)
	SmartOK  bool   // true if SMART is not failing (or unknown)
	TempC    int    // temperature in C (0 if unknown)
	Model    string // drive model
	Serial   string // drive serial
	Size     string // human size, e.g. 1.6T
}

// bad reports whether this drive should light its amber fault LED.
func (h driveHealth) bad() (bool, string) {
	if h.Zfs != "" && h.Zfs != "ONLINE" {
		return true, "zfs:" + h.Zfs
	}
	if h.ZfsErr {
		return true, "zfs:errors"
	}
	if !h.SmartOK {
		return true, "smart-fail"
	}
	return false, ""
}

// healthCache holds the latest SMART results (refreshed on a slow timer) and
// recomputes fast ZFS state on demand. SMART is expensive (spins the bus, ~100ms
// to seconds per drive); ZFS state is cheap. So the reconcile loop gets fresh
// ZFS every cycle but reuses cached SMART/temp.
type healthCache struct {
	mu    sync.RWMutex
	smart map[string]smartResult // dev -> last SMART read
}

type smartResult struct {
	str    string
	ok     bool
	tempC  int
	model  string
	serial string
	size   string
}

func newHealthCache() *healthCache {
	return &healthCache{smart: map[string]smartResult{}}
}

// snapshot returns the merged ZFS+SMART health for every dev seen this cycle.
func (hc *healthCache) snapshot(devs []string) map[string]driveHealth {
	zfs := zpoolState()
	hc.mu.RLock()
	defer hc.mu.RUnlock()
	out := make(map[string]driveHealth, len(devs))
	for _, dev := range devs {
		if dev == "" {
			continue
		}
		dh := driveHealth{SmartOK: true} // default optimistic until SMART says otherwise
		if z, ok := zfs[dev]; ok {
			dh.Zfs = z.state
			dh.Pool = z.pool
			dh.ZfsErr = z.errs
		}
		if s, ok := hc.smart[dev]; ok {
			dh.SmartStr = s.str
			dh.SmartOK = s.ok
			dh.TempC = s.tempC
			dh.Model = s.model
			dh.Serial = s.serial
			dh.Size = s.size
		}
		out[dev] = dh
	}
	return out
}

// refreshSmart re-reads SMART health + temperature for each dev. Runs on the
// slow timer in its own goroutine.
func (hc *healthCache) refreshSmart(devs []string) {
	fresh := make(map[string]smartResult, len(devs))
	for _, dev := range devs {
		if dev == "" {
			continue
		}
		fresh[dev] = readSmart(dev)
	}
	hc.mu.Lock()
	hc.smart = fresh
	hc.mu.Unlock()
}

func readSmart(dev string) smartResult {
	res := smartResult{ok: true}
	if out, err := exec.Command("lsblk", "-dno", "SIZE", "/dev/"+dev).Output(); err == nil {
		res.size = strings.TrimSpace(string(out))
	}
	out, _ := exec.Command("smartctl", "-i", "-H", "-A", "/dev/"+dev).CombinedOutput()
	sc := bufio.NewScanner(strings.NewReader(string(out)))
	for sc.Scan() {
		line := sc.Text()
		l := strings.ToLower(line)
		switch {
		case res.model == "" && (strings.HasPrefix(l, "device model") || strings.HasPrefix(l, "product:") || strings.HasPrefix(l, "model number")):
			res.model = afterColon(line)
		case res.serial == "" && (strings.HasPrefix(l, "serial number") || strings.HasPrefix(l, "serial number:")):
			res.serial = afterColon(line)
		case strings.Contains(l, "overall-health") || strings.Contains(l, "smart health status"):
			// "SMART overall-health self-assessment test result: PASSED"
			// "SMART Health Status: OK"
			if i := strings.LastIndex(line, ":"); i >= 0 {
				res.str = strings.TrimSpace(line[i+1:])
			}
			res.ok = !strings.Contains(l, "fail")
		case strings.Contains(l, "current drive temperature"):
			// SAS: "Current Drive Temperature: 34 C"
			res.tempC = firstInt(line)
		case strings.HasPrefix(strings.TrimSpace(line), "194") && strings.Contains(l, "temperature"):
			// SATA attr 194 Temperature_Celsius ... <raw value at end>
			if v := lastInt(line); v > 0 && v < 120 {
				res.tempC = v
			}
		case res.tempC == 0 && strings.HasPrefix(strings.TrimSpace(line), "190") && strings.Contains(l, "temperature"):
			if v := lastInt(line); v > 0 && v < 120 {
				res.tempC = v
			}
		}
	}
	return res
}

type zfsLeaf struct {
	pool  string
	state string
	errs  bool
}

// zpoolState parses `zpool status -P` and maps each leaf vdev to its base block
// device, pool, state, and whether it has any error counters. Drives not in a
// pool (e.g. passthrough to a VM) simply do not appear.
func zpoolState() map[string]zfsLeaf {
	out := map[string]zfsLeaf{}
	b, err := exec.Command("zpool", "status", "-P").Output()
	if err != nil {
		return out
	}
	var pool string
	sc := bufio.NewScanner(strings.NewReader(string(b)))
	for sc.Scan() {
		line := sc.Text()
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "pool:") {
			pool = strings.TrimSpace(strings.TrimPrefix(trimmed, "pool:"))
			continue
		}
		if !strings.HasPrefix(trimmed, "/dev/") {
			continue
		}
		f := strings.Fields(trimmed)
		if len(f) < 2 {
			continue
		}
		base := baseDev(f[0])
		if base == "" {
			continue
		}
		leaf := zfsLeaf{pool: pool, state: f[1]}
		if len(f) >= 5 {
			r := atoi(f[2])
			w := atoi(f[3])
			c := atoi(f[4])
			leaf.errs = r > 0 || w > 0 || c > 0
		}
		out[base] = leaf
	}
	return out
}

// baseDev resolves a zpool leaf path (often /dev/disk/by-id/... or /dev/sdXN) to
// the base block device name (sdX) via lsblk, falling back to symlink resolution
// + partition-suffix stripping if lsblk fails.
func baseDev(path string) string {
	if out, err := exec.Command("lsblk", "-no", "pkname", path).Output(); err == nil {
		if name := strings.TrimSpace(strings.SplitN(string(out), "\n", 2)[0]); name != "" {
			return name
		}
	}
	// Resolve by-id/by-path symlinks to the real /dev/sdXN before stripping.
	resolved := path
	if r, err := filepath.EvalSymlinks(path); err == nil {
		resolved = r
	}
	name := resolved
	if i := strings.LastIndex(name, "/"); i >= 0 {
		name = name[i+1:]
	}
	// sdb3 -> sdb ; nvme0n1p2 -> nvme0n1
	name = strings.TrimRight(name, "0123456789")
	name = strings.TrimSuffix(name, "p")
	return name
}

func afterColon(s string) string {
	if i := strings.Index(s, ":"); i >= 0 {
		return strings.TrimSpace(s[i+1:])
	}
	return strings.TrimSpace(s)
}

func atoi(s string) int {
	n, _ := strconv.Atoi(strings.TrimSpace(s))
	return n
}

func firstInt(s string) int {
	for _, f := range strings.Fields(s) {
		if n, err := strconv.Atoi(strings.Trim(f, ":")); err == nil {
			return n
		}
	}
	return 0
}

func lastInt(s string) int {
	v := 0
	for _, f := range strings.Fields(s) {
		if n, err := strconv.Atoi(f); err == nil {
			v = n
		}
	}
	return v
}
