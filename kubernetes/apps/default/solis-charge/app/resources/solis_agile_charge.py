#!/usr/bin/env python3
"""
Solis Agile Charge / Discharge Optimizer.

Reads Octopus Agile rates, Octoplus Free Electricity (Power-ups) and Octoplus
Saving Sessions from Home Assistant and programs the Solis inverter:

  - Charge during cheap Agile slots (<= MAX_RATE)
  - Force-charge during Free Electricity windows (Power-ups), no rate check
  - Discharge during Saving Sessions to dodge grid usage
  - All times are emitted in UTC because the inverter clock runs UTC+0 on
    SolisCloud (no DST). The HACS solis integration ships HH,MM straight to
    the inverter with no TZ conversion, so feeding it BST would charge an
    hour late.

Up to 3 charge slots and 3 discharge slots. Unused slots are cleared.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# === CONFIG ===
HASS_URL = os.environ["HASS_URL"]
HASS_TOKEN = os.environ["HASS_TOKEN"]

MAX_RATE = 0.15            # 15p/kWh inc VAT
MAX_SLOTS = 3
CHARGE_CURRENT = 50        # Amps - max for this inverter
DISCHARGE_CURRENT = 50     # Amps

# Octopus Energy HACS entities.
NEXT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_next_day_rates"
CURRENT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_current_day_rates"

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
}


# ---------- HASS plumbing ----------

def hass_get(entity_id):
    resp = requests.get(f"{HASS_URL}/api/states/{entity_id}", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def hass_get_optional(entity_id):
    resp = requests.get(f"{HASS_URL}/api/states/{entity_id}", headers=HEADERS, timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def hass_service(domain, service, data, timeout=10):
    resp = requests.post(
        f"{HASS_URL}/api/services/{domain}/{service}",
        headers=HEADERS,
        json=data,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


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

def find_cheap_windows(rates):
    """Find consecutive cheap slots. Up to MAX_SLOTS windows, prefer cheapest avg."""
    cheap = sorted(
        [r for r in rates if r["value_inc_vat"] <= MAX_RATE],
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
    print(f"  {cheap_count} cheap slots (<={MAX_RATE*100:.0f}p), "
          f"{len(rates) - cheap_count} expensive slots")
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
