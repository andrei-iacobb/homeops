#!/usr/bin/env python3
"""
Solis Agile Charge Optimizer

Reads Octopus Agile rates from Home Assistant and programs the Solis inverter
to charge the battery only during cheap periods (<=15p/kWh).

Uses up to 3 charge slots. If no rates are <=15p, all slots are cleared.
Safety check: verifies no expensive rates fall within any charge window.
"""

import os
import sys
from datetime import datetime

import requests

# === CONFIG ===
HASS_URL = os.environ["HASS_URL"]
HASS_TOKEN = os.environ["HASS_TOKEN"]

MAX_RATE = 0.15  # £0.15/kWh = 15p inc VAT
MAX_SLOTS = 3
CHARGE_CURRENT = 50  # Amps (max for this inverter)

NEXT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_next_day_rates"
CURRENT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_current_day_rates"

HEADERS = {
    "Authorization": f"Bearer {HASS_TOKEN}",
    "Content-Type": "application/json",
}


def hass_get(entity_id):
    """Get entity state from HASS."""
    resp = requests.get(f"{HASS_URL}/api/states/{entity_id}", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def hass_service(domain, service, data):
    """Call a HASS service."""
    resp = requests.post(
        f"{HASS_URL}/api/services/{domain}/{service}",
        headers=HEADERS,
        json=data,
        timeout=10,
    )
    resp.raise_for_status()
    return resp


def get_rates():
    """Fetch rates, preferring next-day. Falls back to current-day."""
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


def find_cheap_windows(rates):
    """
    Find consecutive windows where rate <= MAX_RATE.
    Returns up to MAX_SLOTS windows sorted by cheapest average rate.
    """
    cheap = sorted(
        [r for r in rates if r["value_inc_vat"] <= MAX_RATE],
        key=lambda r: r["start"],
    )

    if not cheap:
        return []

    # Group consecutive 30-min slots into windows
    windows = []
    cs = cheap[0]["start"]
    ce = cheap[0]["end"]
    slot_rates = [cheap[0]["value_inc_vat"]]

    for r in cheap[1:]:
        r_start = datetime.fromisoformat(r["start"])
        c_end = datetime.fromisoformat(ce)

        if r_start == c_end:
            # Consecutive — extend window
            ce = r["end"]
            slot_rates.append(r["value_inc_vat"])
        else:
            # Gap — save current window, start new one
            windows.append({
                "start": cs,
                "end": ce,
                "avg_rate": sum(slot_rates) / len(slot_rates),
                "slots": len(slot_rates),
            })
            cs = r["start"]
            ce = r["end"]
            slot_rates = [r["value_inc_vat"]]

    # Don't forget last window
    windows.append({
        "start": cs,
        "end": ce,
        "avg_rate": sum(slot_rates) / len(slot_rates),
        "slots": len(slot_rates),
    })

    # If more windows than slots, pick the cheapest by average rate
    if len(windows) > MAX_SLOTS:
        windows.sort(key=lambda w: w["avg_rate"])
        windows = windows[:MAX_SLOTS]

    # Sort by start time for clean scheduling
    windows.sort(key=lambda w: w["start"])

    return windows


def safety_check(windows, rates):
    """
    Verify NO expensive rate (>MAX_RATE) falls within any charge window.
    Returns True if safe, False if overlap detected.
    """
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


def apply_schedule(windows):
    """Set Solis charge time slots via HASS. Clear unused slots."""
    for i in range(MAX_SLOTS):
        slot = i + 1
        if i < len(windows):
            w = windows[i]
            start_t = datetime.fromisoformat(w["start"]).strftime("%H:%M:%S")
            end_t = datetime.fromisoformat(w["end"]).strftime("%H:%M:%S")

            hass_service("time", "set_value", {
                "entity_id": f"time.solis_timed_charge_start_{slot}",
                "time": start_t,
            })
            hass_service("time", "set_value", {
                "entity_id": f"time.solis_timed_charge_end_{slot}",
                "time": end_t,
            })
            hass_service("number", "set_value", {
                "entity_id": f"number.solis_timed_charge_current_{slot}",
                "value": CHARGE_CURRENT,
            })
            avg = w["avg_rate"] * 100
            duration = w["slots"] * 30
            print(f"  Slot {slot}: {start_t[:5]} - {end_t[:5]}  "
                  f"({duration}min, avg {avg:.1f}p/kWh)")
        else:
            # Clear unused slot
            hass_service("time", "set_value", {
                "entity_id": f"time.solis_timed_charge_start_{slot}",
                "time": "00:00:00",
            })
            hass_service("time", "set_value", {
                "entity_id": f"time.solis_timed_charge_end_{slot}",
                "time": "00:00:00",
            })
            hass_service("number", "set_value", {
                "entity_id": f"number.solis_timed_charge_current_{slot}",
                "value": 0,
            })
            print(f"  Slot {slot}: disabled")


def push_to_inverter():
    """Press the update button to send schedule to the inverter."""
    hass_service("button", "press", {
        "entity_id": "button.solis_update_timed_charge_discharge",
    })


def main():
    print(f"Solis Agile Charge Optimizer — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Max rate threshold: {MAX_RATE*100:.0f}p/kWh")
    print()

    # 1. Get rates
    rates = get_rates()
    if not rates:
        print("ERROR: No rate data available")
        sys.exit(1)

    cheap_count = sum(1 for r in rates if r["value_inc_vat"] <= MAX_RATE)
    expensive_count = len(rates) - cheap_count
    print(f"  {cheap_count} cheap slots (<={MAX_RATE*100:.0f}p), "
          f"{expensive_count} expensive slots (>{MAX_RATE*100:.0f}p)")
    print(f"  Rate range: {min(r['value_inc_vat'] for r in rates)*100:.1f}p "
          f"- {max(r['value_inc_vat'] for r in rates)*100:.1f}p")
    print()

    # 2. Find cheap windows
    windows = find_cheap_windows(rates)

    if not windows:
        print("No cheap rates found — clearing all charge slots")
        apply_schedule([])
        push_to_inverter()
        print("Done — no charging scheduled")
        return

    print(f"Found {len(windows)} charge window(s):")
    for w in windows:
        duration = w["slots"] * 30
        s = datetime.fromisoformat(w["start"]).strftime("%H:%M")
        e = datetime.fromisoformat(w["end"]).strftime("%H:%M")
        print(f"  {s} - {e}  ({duration}min, avg {w['avg_rate']*100:.1f}p/kWh)")
    print()

    # 3. Safety check — absolutely no expensive rates during charging
    print("Running safety check...")
    if not safety_check(windows, rates):
        print("ABORTING — expensive rate detected in charge window!")
        print("Clearing all charge slots for safety")
        apply_schedule([])
        push_to_inverter()
        sys.exit(1)
    print("  PASSED — no expensive rates overlap charge windows")
    print()

    # 4. Apply schedule
    print("Setting charge schedule:")
    apply_schedule(windows)
    print()

    # 5. Push to inverter
    print("Pushing schedule to inverter...")
    push_to_inverter()
    print("Done!")


if __name__ == "__main__":
    main()
