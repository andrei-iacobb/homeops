#!/usr/bin/env python3
"""
Solis Agile Charge / Discharge Optimizer.

Reads Octopus Agile rates, Octoplus Free Electricity (Power-ups) and Octoplus
Saving Sessions from Home Assistant and programs the Solis inverter:

  - Charge during cheap Agile slots (<= MAX_RATE) that fall in the overnight
    grid-charge window only. Daytime cheap slots are skipped on purpose: the
    solar array charges the battery for free then, so grid-charging would
    waste money (in summer Agile is cheap at midday precisely because of solar).
  - Force-charge during Free Electricity windows (Power-ups), no rate check,
    any time of day (Octopus pays you to import).
  - Discharge during Saving Sessions to dodge grid usage
  - All times are emitted in UTC because the inverter clock runs UTC+0 on
    SolisCloud (no DST). The HACS solis integration ships HH,MM straight to
    the inverter with no TZ conversion, so feeding it BST would charge an
    hour late.

Up to 3 charge slots and 3 discharge slots. Unused slots are cleared.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# === CONFIG ===
HASS_URL = os.environ["HASS_URL"]
HASS_TOKEN = os.environ["HASS_TOKEN"]

MAX_RATE = float(os.environ.get("MAX_RATE", "0.15"))   # £/kWh inc VAT cap (15p)
MAX_SLOTS = 3
CHARGE_CURRENT = 50        # Amps - max for this inverter
DISCHARGE_CURRENT = 50     # Amps

# Grid-charge during cheap (<= MAX_RATE) slots. Default is 24h (start == end
# => no time restriction), so every sub-15p slot is taken regardless of time
# of day. The window is an optional knob: set CHARGE_WINDOW_START/END (UK
# local "HH:MM") to restrict grid-charging to e.g. overnight ("23:00"/"07:00")
# if you ever want to stop importing during daylight when solar could fill the
# battery for free. Window may wrap past midnight (start > end => overnight).
CHARGE_WINDOW_START = os.environ.get("CHARGE_WINDOW_START", "00:00")
CHARGE_WINDOW_END = os.environ.get("CHARGE_WINDOW_END", "00:00")

# Max acceptable drift between inverter reported UTC time and real UTC.
# Anything beyond this almost certainly means the SolisCloud station was
# flipped to a local-time TZ (e.g. Europe/London with DST), in which case
# slot writes would be offset and we MUST refuse to push.
MAX_CLOCK_DRIFT_SECONDS = 30 * 60

# Octopus Energy HACS entities.
NEXT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_next_day_rates"
CURRENT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_current_day_rates"

# Inverter timestamp sensor - reports the inverter's own clock as a Unix
# epoch in UTC. Used to detect SolisCloud TZ misconfiguration.
INVERTER_TIMESTAMP_ENTITY = (
    "sensor.solis_inverter_1031030229080043_solis_timestamp_measurements_received"
)

# Octoplus entities (may be absent if not enrolled - script tolerates this).
OCTOPLUS_SAVING_EVENT = "event.octopus_energy_a_a2279b81_octoplus_saving_session_events"
OCTOPLUS_FREE_EVENT_CANDIDATES = [
    "event.octopus_energy_a_a2279b81_octoplus_free_electricity_session_events",
    "event.octopus_energy_a_a2279b81_octoplus_free_electricity_sessions",
    "binary_sensor.octopus_energy_a_a2279b81_octoplus_free_electricity_session",
    "binary_sensor.octopus_energy_a_a2279b81_octoplus_free_electricity_sessions",
]

HEADERS = {
    "Authorization": f"Bearer {HASS_TOKEN}",
    "Content-Type": "application/json",
    # Cloudflare in front of hass.iacob.co.uk 403s the default Python-urllib
    # User-Agent, so set an explicit one.
    "User-Agent": "solis-charge/1.0",
}


# ---------- HASS plumbing ----------

def _request(method, url, payload=None, timeout=10):
    """Minimal stdlib HTTP. Avoids a runtime `pip install` that can fail the
    whole job when PyPI is slow/unreachable. Raises urllib.error.HTTPError on
    non-2xx (callers that need 404-tolerance catch it)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return json.loads(body) if body else None


def hass_get(entity_id):
    result = _request("GET", f"{HASS_URL}/api/states/{entity_id}")
    if result is None:
        raise RuntimeError(f"empty response for {entity_id}")
    return result


def hass_get_optional(entity_id):
    try:
        return hass_get(entity_id)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def hass_service(domain, service, data, timeout=10):
    return _request(
        "POST", f"{HASS_URL}/api/services/{domain}/{service}",
        payload=data, timeout=timeout,
    )


# ---------- Storage mode guard ----------

# Timed charge/discharge slots are DEAD LETTERS unless the inverter's energy
# storage control switch is in a mode with timed charge enabled. Discovered
# 2026-07-23: the register had silently flipped to an unmapped value (HASS
# select showed "unknown") for 10+ days - slots were programmed perfectly,
# battery ignored them, 567 kWh bought from grid that month. Guard every run.
STORAGE_MODE_ENTITY = "select.solis_energy_storage_control_switch"
REQUIRED_STORAGE_MODE = os.environ.get("REQUIRED_STORAGE_MODE", "Self-Use")


def ensure_storage_mode():
    """Verify the inverter is in the required storage mode; fix it if not.

    The solis_modbus select only reflects a write after its next poll cycle
    (~60s observed), so poll for confirmation. Exit non-zero if the mode
    cannot be confirmed - the job failure makes the problem visible instead
    of silently programming slots the inverter will ignore.
    """
    state = hass_get_optional(STORAGE_MODE_ENTITY)
    if state is None:
        print(f"ERROR: {STORAGE_MODE_ENTITY} does not exist in HASS - "
              f"solis integration missing/renamed. Cannot verify storage "
              f"mode; slots may be ignored. Investigate.")
        sys.exit(4)
    mode = state.get("state")
    if mode == REQUIRED_STORAGE_MODE:
        print(f"Storage mode check: {mode} - OK")
        return
    if mode == "unavailable":
        print(f"ERROR: {STORAGE_MODE_ENTITY} is 'unavailable' - solis "
              f"integration cannot reach the inverter. Not writing. "
              f"Investigate.")
        sys.exit(4)
    options = state.get("attributes", {}).get("options", [])
    if options and REQUIRED_STORAGE_MODE not in options:
        print(f"ERROR: required mode '{REQUIRED_STORAGE_MODE}' is not one "
              f"of the select's options {options}. Config mistake.")
        sys.exit(4)

    print(f"WARNING: storage mode is '{mode}', expected "
          f"'{REQUIRED_STORAGE_MODE}'. Timed charge slots are ignored in "
          f"this mode. Attempting to fix...")
    hass_service("select", "select_option", {
        "entity_id": STORAGE_MODE_ENTITY,
        "option": REQUIRED_STORAGE_MODE,
    }, timeout=30)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(15)
        try:
            state = hass_get_optional(STORAGE_MODE_ENTITY)
        except Exception as e:  # transient HA blip - keep polling
            print(f"  poll error (transient, retrying): {e}")
            continue
        mode = state.get("state") if state else None
        if mode == REQUIRED_STORAGE_MODE:
            print(f"Storage mode fixed: now '{mode}'.")
            return
    print(f"ERROR: storage mode still '{mode}' after write + 180s. "
          f"Inverter will NOT act on charge slots. Investigate.")
    sys.exit(4)


# ---------- Octopus data ----------

def get_rates():
    """Fetch rates. Prefer next-day, fall back to current-day."""
    try:
        data = hass_get(NEXT_DAY_RATES)
        rates = data["attributes"].get("rates", [])
        if rates:
            print(f"Using next-day rates ({len(rates)} slots)")
            return rates
    except Exception as e:
        print(f"Next-day rates unavailable: {e}")

    data = hass_get(CURRENT_DAY_RATES)
    rates = data["attributes"].get("rates", [])
    print(f"Using current-day rates ({len(rates)} slots)")
    return rates


def get_free_electricity_sessions():
    """Return list of {'start','end'} for Octoplus Power-ups in the next 24h."""
    sessions = []
    for eid in OCTOPLUS_FREE_EVENT_CANDIDATES:
        state = hass_get_optional(eid)
        if not state:
            continue
        attrs = state.get("attributes", {})
        # The integration uses different attribute names across versions.
        for key in ("available_events", "events", "joined_events", "next_events"):
            for ev in attrs.get(key, []) or []:
                if not isinstance(ev, dict):
                    continue
                s, e = ev.get("start"), ev.get("end")
                if s and e:
                    sessions.append({"start": s, "end": e})
        # Single-event style.
        if attrs.get("current_event_start") and attrs.get("current_event_end"):
            sessions.append({
                "start": attrs["current_event_start"],
                "end": attrs["current_event_end"],
            })
        if attrs.get("next_event_start") and attrs.get("next_event_end"):
            sessions.append({
                "start": attrs["next_event_start"],
                "end": attrs["next_event_end"],
            })
    return _future_sessions(sessions)


def get_saving_sessions():
    """Return list of joined Saving Session windows that are still upcoming."""
    state = hass_get_optional(OCTOPLUS_SAVING_EVENT)
    if not state:
        return []
    attrs = state.get("attributes", {})
    sessions = []
    for ev in attrs.get("joined_events", []) or []:
        if isinstance(ev, dict) and ev.get("start") and ev.get("end"):
            sessions.append({"start": ev["start"], "end": ev["end"]})
    return _future_sessions(sessions)


def _future_sessions(sessions):
    """Drop sessions whose end is already in the past. Dedup by (start,end)."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=36)
    seen = set()
    out = []
    for s in sessions:
        try:
            end = datetime.fromisoformat(s["end"])
            start = datetime.fromisoformat(s["start"])
        except ValueError:
            continue
        if end <= now or start >= horizon:
            continue
        key = (s["start"], s["end"])
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---------- Window logic ----------

def _hhmm_to_min(s):
    h, m = (s.split(":") + ["0"])[:2]
    return int(h) * 60 + int(m)


def in_charge_window(iso_str):
    """True if a slot start falls inside the allowed (overnight) charge window.

    The Octopus rate timestamps carry their own UK-local offset (e.g.
    +01:00 in BST), so reading hour/minute straight off the parsed datetime
    gives UK wall-clock with no tzdata dependency. Handles windows that wrap
    past midnight (start > end)."""
    start = _hhmm_to_min(CHARGE_WINDOW_START)
    end = _hhmm_to_min(CHARGE_WINDOW_END)
    if start == end:
        return True  # 24h - charging allowed any time
    dt = datetime.fromisoformat(iso_str)
    m = dt.hour * 60 + dt.minute
    if start < end:
        return start <= m < end
    return m >= start or m < end  # wraps midnight


def find_cheap_windows(rates):
    """Find consecutive cheap slots inside the allowed overnight window.

    Up to MAX_SLOTS windows, prefer cheapest avg. Slots that are cheap but
    fall in daytime/solar hours are intentionally excluded - solar charges
    the battery for free then, so paying to grid-charge would waste money."""
    cheap = sorted(
        [r for r in rates
         if r["value_inc_vat"] <= MAX_RATE and in_charge_window(r["start"])],
        key=lambda r: r["start"],
    )

    if not cheap:
        return []

    windows = []
    cs = cheap[0]["start"]
    ce = cheap[0]["end"]
    slot_rates = [cheap[0]["value_inc_vat"]]

    for r in cheap[1:]:
        if datetime.fromisoformat(r["start"]) == datetime.fromisoformat(ce):
            ce = r["end"]
            slot_rates.append(r["value_inc_vat"])
        else:
            windows.append({
                "start": cs, "end": ce,
                "avg_rate": sum(slot_rates) / len(slot_rates),
                "slots": len(slot_rates),
            })
            cs, ce, slot_rates = r["start"], r["end"], [r["value_inc_vat"]]

    windows.append({
        "start": cs, "end": ce,
        "avg_rate": sum(slot_rates) / len(slot_rates),
        "slots": len(slot_rates),
    })

    if len(windows) > MAX_SLOTS:
        windows.sort(key=lambda w: w["avg_rate"])
        windows = windows[:MAX_SLOTS]

    windows.sort(key=lambda w: w["start"])
    return windows


def safety_check(windows, rates):
    """No rate above MAX_RATE may fall inside any charge window."""
    expensive = [r for r in rates if r["value_inc_vat"] > MAX_RATE]
    for w in windows:
        ws = datetime.fromisoformat(w["start"])
        we = datetime.fromisoformat(w["end"])
        for r in expensive:
            rs = datetime.fromisoformat(r["start"])
            re_end = datetime.fromisoformat(r["end"])
            if rs < we and re_end > ws:
                print(f"  DANGER: {r['value_inc_vat']*100:.1f}p rate at "
                      f"{r['start'][11:16]}-{r['end'][11:16]} overlaps "
                      f"charge window {w['start'][11:16]}-{w['end'][11:16]}")
                return False
    return True


def merge_windows(windows):
    """Merge overlapping/adjacent {'start','end'} windows. Returns sorted list."""
    if not windows:
        return []
    parsed = []
    for w in windows:
        try:
            s = datetime.fromisoformat(w["start"])
            e = datetime.fromisoformat(w["end"])
            parsed.append((s, e, w))
        except ValueError:
            continue
    parsed.sort(key=lambda t: t[0])

    merged = [parsed[0]]
    for s, e, w in parsed[1:]:
        ls, le, lw = merged[-1]
        if s <= le:
            new_end = max(le, e)
            tags = [t for t in (lw.get("tag"), w.get("tag")) if t]
            merged[-1] = (
                ls,
                new_end,
                {
                    "start": ls.isoformat(),
                    "end": new_end.isoformat(),
                    "tag": "+".join(dict.fromkeys(tags)) if tags else "",
                },
            )
        else:
            merged.append((s, e, w))
    return [m[2] for m in merged]


# ---------- Sanity checks ----------

def verify_inverter_clock():
    """Abort if the inverter clock has drifted from real UTC.

    The HACS solis integration writes raw HH:MM to the inverter with no TZ
    conversion. We emit times in UTC on the assumption that the SolisCloud
    station is also UTC+0. If someone flips the station to Europe/London
    (which honours DST), every slot would be applied 1h late from late
    March to late October. That mistake cost real money before this check
    existed - hence the hard abort.
    """
    state = hass_get_optional(INVERTER_TIMESTAMP_ENTITY)
    if not state:
        print(f"WARN: inverter timestamp sensor missing ({INVERTER_TIMESTAMP_ENTITY}); "
              f"skipping clock-drift check")
        return
    try:
        inverter_epoch = float(state["state"])
    except (KeyError, TypeError, ValueError):
        print(f"WARN: inverter timestamp sensor unreadable; skipping clock-drift check")
        return

    real_epoch = datetime.now(timezone.utc).timestamp()
    drift = real_epoch - inverter_epoch
    inverter_dt = datetime.fromtimestamp(inverter_epoch, tz=timezone.utc)
    print(f"Inverter clock check: inverter says {inverter_dt:%Y-%m-%d %H:%M:%S} UTC, "
          f"drift {drift:+.0f}s from real UTC.")

    if abs(drift) > MAX_CLOCK_DRIFT_SECONDS:
        print(f"ABORTING: inverter clock drift {drift:+.0f}s exceeds "
              f"{MAX_CLOCK_DRIFT_SECONDS}s. This usually means the SolisCloud "
              f"station TZ was changed away from UTC+0. Fix the station setting "
              f"before charging will resume.")
        sys.exit(2)


def _rate_at(rates, dt_utc):
    """Return rate (£/kWh inc VAT) at a given UTC datetime, or None."""
    for r in rates:
        s = datetime.fromisoformat(r["start"])
        e = datetime.fromisoformat(r["end"])
        if s <= dt_utc < e:
            return r["value_inc_vat"]
    return None


def verify_written_slots_are_cheap(charge_windows, rates):
    """Round-trip check: each slot we're about to write, interpreted as a UTC
    wall-clock on the same day as the window, must correspond to a cheap rate.

    Catches the historical bug class: slot times written in BST but applied
    as UTC, putting the actual charge into expensive rates. If the slot's
    UTC HH:MM doesn't land in a cheap Octopus rate, we abort.

    Free-electricity slots skip this check because their rates are
    irrelevant (Octopus pays you to use power).
    """
    for w in charge_windows:
        if "free" in (w.get("tag") or ""):
            continue
        start_dt = datetime.fromisoformat(w["start"]).astimezone(timezone.utc)
        end_dt = datetime.fromisoformat(w["end"]).astimezone(timezone.utc)

        # Sample the rate just inside start and just inside end.
        probe_start = start_dt
        probe_end = end_dt - timedelta(minutes=1)
        for label, probe in (("start", probe_start), ("end-1min", probe_end)):
            rate = _rate_at(rates, probe)
            if rate is None:
                # No rate data for that instant (e.g. far in past) - skip.
                continue
            if rate > MAX_RATE:
                print(f"ABORTING: slot {w['start']} -> {w['end']} {label}-probe "
                      f"@ {probe:%Y-%m-%d %H:%M UTC} has rate "
                      f"{rate*100:.1f}p > {MAX_RATE*100:.0f}p cap. "
                      f"Refusing to charge during expensive rates.")
                return False
    return True


# ---------- Slot programming ----------

def _utc_hhmm(iso_str):
    """Take an ISO timestamp with offset, convert to UTC, return HH:MM:SS.

    The Solis inverter clock runs in UTC+0 (per SolisCloud station settings).
    The HACS solis integration sends the raw HH:MM from the time entity
    straight to the inverter with no TZ conversion. So we must emit times
    in the inverter's local clock = UTC.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%H:%M:%S")


def _norm_time(v):
    """Normalise '05:30:00' style states for comparison ('5:30:00' -> '05:30:00')."""
    if not isinstance(v, str) or ":" not in v:
        return v
    parts = v.split(":")
    return ":".join(p.zfill(2) for p in parts[:3]) if len(parts) >= 2 else v


def _set_slot(kind, slot, start, end, current):
    """kind in {'charge','discharge'}. Writes time and current entities.

    Returns True if any value differed from the current HASS state.
    """
    changed = False
    targets = [
        ("time", f"time.solis_timed_{kind}_start_{slot}", start, _norm_time),
        ("time", f"time.solis_timed_{kind}_end_{slot}", end, _norm_time),
        ("number", f"number.solis_timed_{kind}_current_{slot}", current, lambda x: str(int(float(x)))),
    ]
    for domain, eid, desired, normaliser in targets:
        current_state = hass_get_optional(eid)
        cur_val = current_state.get("state") if current_state else None
        if normaliser(cur_val) != normaliser(desired):
            changed = True
            if domain == "time":
                hass_service("time", "set_value", {"entity_id": eid, "time": desired})
            else:
                hass_service("number", "set_value", {"entity_id": eid, "value": desired})
    return changed


def _clear_slot(kind, slot):
    return _set_slot(kind, slot, "00:00:00", "00:00:00", 0)


def apply_schedule(charge_windows, discharge_windows):
    """Program up to MAX_SLOTS charge + MAX_SLOTS discharge slots.

    Returns True if any HASS entity state changed (used to decide whether to
    re-push the schedule to the inverter).
    """
    changed = False
    for i in range(MAX_SLOTS):
        slot = i + 1
        if i < len(charge_windows):
            w = charge_windows[i]
            start_t = _utc_hhmm(w["start"])
            end_t = _utc_hhmm(w["end"])
            changed |= _set_slot("charge", slot, start_t, end_t, CHARGE_CURRENT)
            tag = w.get("tag", "")
            extra = f"  ({tag})" if tag else ""
            print(f"  Charge slot {slot}: {start_t[:5]} - {end_t[:5]} UTC{extra}")
        else:
            changed |= _clear_slot("charge", slot)
            print(f"  Charge slot {slot}: disabled")

    for i in range(MAX_SLOTS):
        slot = i + 1
        if i < len(discharge_windows):
            w = discharge_windows[i]
            start_t = _utc_hhmm(w["start"])
            end_t = _utc_hhmm(w["end"])
            changed |= _set_slot("discharge", slot, start_t, end_t, DISCHARGE_CURRENT)
            tag = w.get("tag", "")
            extra = f"  ({tag})" if tag else ""
            print(f"  Discharge slot {slot}: {start_t[:5]} - {end_t[:5]} UTC{extra}")
        else:
            changed |= _clear_slot("discharge", slot)
            print(f"  Discharge slot {slot}: disabled")
    return changed


def push_to_inverter():
    # The HACS solis integration's button press synchronously calls the
    # SolisCloud control API, which can take 20-40 seconds. Give it a
    # generous timeout so we don't bail half-way.
    hass_service(
        "button",
        "press",
        {"entity_id": "button.solis_update_timed_charge_discharge"},
        timeout=90,
    )


# ---------- Main ----------

def main():
    now_utc = datetime.now(timezone.utc)
    print(f"Solis Optimizer - run at {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Max Agile rate threshold: {MAX_RATE*100:.0f}p/kWh")
    print(f"Inverter clock TZ: UTC+0 (SolisCloud station setting). "
          f"All slot times below are in UTC.")
    print()

    # 0. Verify the inverter clock matches our UTC assumption before we
    # touch any slot values. If the SolisCloud station was flipped to a
    # DST-aware TZ, slot writes would silently be 1h offset.
    verify_inverter_clock()
    print()

    # 0.5. Verify the storage mode actually honours timed charge slots.
    ensure_storage_mode()
    print()

    # 1. Free electricity (Power-ups) - top priority charge windows.
    free_sessions = get_free_electricity_sessions()
    if free_sessions:
        print(f"Found {len(free_sessions)} Free Electricity session(s):")
        for s in free_sessions:
            print(f"  {s['start']} -> {s['end']}")
    else:
        print("Free Electricity: none upcoming.")

    # 2. Saving sessions - discharge windows.
    saving_sessions = get_saving_sessions()
    if saving_sessions:
        print(f"Found {len(saving_sessions)} joined Saving Session(s):")
        for s in saving_sessions:
            print(f"  {s['start']} -> {s['end']}")
    else:
        print("Saving Sessions: none upcoming.")
    print()

    # 3. Agile rates.
    rates = get_rates()
    if not rates:
        print("ERROR: no rate data available")
        sys.exit(1)

    cheap_count = sum(1 for r in rates if r["value_inc_vat"] <= MAX_RATE)
    eligible_count = sum(
        1 for r in rates
        if r["value_inc_vat"] <= MAX_RATE and in_charge_window(r["start"])
    )
    print(f"  {cheap_count} cheap slots (<={MAX_RATE*100:.0f}p), "
          f"{len(rates) - cheap_count} expensive slots")
    window_desc = ("24h - no time restriction"
                   if CHARGE_WINDOW_START == CHARGE_WINDOW_END
                   else f"{CHARGE_WINDOW_START}-{CHARGE_WINDOW_END} UK local")
    print(f"  Grid-charge window: {window_desc} "
          f"-> {eligible_count} cheap slot(s) eligible to charge")
    print(f"  Rate range: {min(r['value_inc_vat'] for r in rates)*100:.1f}p "
          f"- {max(r['value_inc_vat'] for r in rates)*100:.1f}p")
    print()

    # 4. Build charge windows.
    cheap_windows = find_cheap_windows(rates)
    for w in cheap_windows:
        w["tag"] = f"agile {w['avg_rate']*100:.1f}p"

    if cheap_windows:
        print("Running safety check on cheap windows...")
        if not safety_check(cheap_windows, rates):
            print("ABORTING - expensive rate detected inside a charge window")
            apply_schedule([], [])
            push_to_inverter()
            sys.exit(1)
        print("  PASSED")
        print()

    # Free Electricity always wins - tag and prepend, then merge.
    free_tagged = [{"start": s["start"], "end": s["end"], "tag": "free"} for s in free_sessions]
    all_charge = merge_windows(free_tagged + cheap_windows)
    # If merging produced > MAX_SLOTS, keep free-electricity ones plus cheapest.
    if len(all_charge) > MAX_SLOTS:
        free_first = [w for w in all_charge if "free" in (w.get("tag") or "")]
        rest = sorted(
            [w for w in all_charge if "free" not in (w.get("tag") or "")],
            key=lambda w: w.get("avg_rate", MAX_RATE),
        )
        all_charge = (free_first + rest)[:MAX_SLOTS]
        all_charge.sort(key=lambda w: datetime.fromisoformat(w["start"]))

    # 5. Discharge windows = saving sessions only (for now).
    discharge_windows = [
        {"start": s["start"], "end": s["end"], "tag": "saving session"}
        for s in saving_sessions
    ]
    discharge_windows = merge_windows(discharge_windows)[:MAX_SLOTS]

    # 5.5. Round-trip check: confirm each charge window we're about to write
    # actually lands in cheap rates after our UTC conversion. Catches any
    # future TZ bug class before it hits the inverter.
    if all_charge:
        print("Round-trip rate check on UTC-converted windows...")
        if not verify_written_slots_are_cheap(all_charge, rates):
            print("Clearing all charge slots due to round-trip check failure.")
            apply_schedule([], discharge_windows)
            push_to_inverter()
            sys.exit(3)
        print("  PASSED")
        print()

    # 6. Apply.
    print("Programming inverter:")
    changed = apply_schedule(all_charge, discharge_windows)
    print()

    if not changed:
        print("No slot values changed - skipping cloud push.")
        print("Done")
        return

    print("Pushing schedule to inverter...")
    push_to_inverter()
    print("Done")


if __name__ == "__main__":
    main()
