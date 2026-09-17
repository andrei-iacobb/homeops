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
  - Slot times are written in the inverter's OWN clock. The inverter takes
    bare HH:MM and compares it to its internal time, which Andrei keeps on UK
    local time (BST in summer) even though the SolisCloud station is labelled
    UTC+0. Every run reads that clock live (cid 56), derives its offset from
    UTC and converts the planned windows with it - so the script follows
    whatever the inverter is set to, including the clocks changing. Writing
    UTC on the assumption the station label was the truth fired every slot
    an hour early through summer 2026 (2026-09-17: charged 08:30-09:30 BST at
    27p/21p and skipped the 4.9p/6.7p hour at 15:00).

Up to 3 charge slots and 3 discharge slots, written straight to the\nSolisCloud open API (window, current and the per-slot on/off switch).\nUnused slots are cleared and switched off.
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

# The inverter's own clock, read live from SolisCloud every run (cid 56 =
# Modbus system time, "YYYY-MM-DD HH:MM:SS" in whatever zone the inverter is
# set to). Its distance from real UTC is snapped to the nearest whole hour
# to get the zone offset (a UK inverter is on GMT or BST, never a half-hour
# zone - snapping finer would let a 12 min drift pass as "UTC+01:15"); what
# is left is genuine drift, measurable up to +-30 min. Above the WARN level
# the run carries on (a slot boundary lands a few minutes into the adjacent
# rate - cheaper than losing the day); above the ABORT level the snap itself
# is ambiguous, so the schedule is cleared and the run exits until the clock
# is corrected. (Observed 2026-09-17: BST, +4 min fast.)
INVERTER_CLOCK_CID = "56"
INVERTER_CLOCK_SNAP_SECONDS = 3600
INVERTER_CLOCK_WARN_DRIFT_SECONDS = 10 * 60
INVERTER_CLOCK_ABORT_DRIFT_SECONDS = 25 * 60
# Set by verify_inverter_clock(); every slot time is written as UTC + this.
INVERTER_UTC_OFFSET = None

# Age of the newest inverter reading beyond which the run warns that the
# SolisCloud feed is stale. Warning only: slot writes go through the
# SolisCloud control API and are idempotent, so a stale feed is no reason to
# leave a spent schedule on the inverter.
STALE_FEED_WARN_SECONDS = 3 * 3600

# How far back to look for a genuine inverter reading in HASS. Only used to
# judge telemetry freshness (warnings, and gating the charge-outcome check).
CLOCK_HISTORY_WINDOW_SECONDS = 24 * 3600

# Charge verification. SolisCloud's control API can accept a slot write,
# read it back as stored, and still never deliver it to the inverter: on
# 2026-09-12 atRead reported 08:00-15:00 for slot 1 all morning while the
# battery sat at 19% with no grid import, and grid charging only began after
# the commit button was pressed again (SolisCloud 5-min series shows 0 W to
# the battery until 08:47 UTC and 2.7 kW from 09:22 UTC, the re-press was at
# 09:02). So neither "HASS state matches" nor "cloud read-back matches" is
# proof. The only proof is the inverter behaving: inside a programmed charge
# window the battery must be taking roughly the timed-charge current. If it
# is not, re-commit the slots.
# 50 A x ~52 V is ~2.6 kW; 2.7 kW observed. Below this inside a window the
# timed charge is not running (PV-only charging on a bright day can also
# exceed it, in which case the battery is filling anyway and nothing is lost).
CHARGE_VERIFY_MIN_WATTS = float(os.environ.get("CHARGE_VERIFY_MIN_WATTS", "2000"))
# Charge current tapers near full; do not treat that as a failed slot.
CHARGE_VERIFY_FULL_SOC = float(os.environ.get("CHARGE_VERIFY_FULL_SOC", "95"))
# Give the inverter this long after a window opens before judging it, and
# only judge on a reading no older than this (the feed stalls for hours).
CHARGE_VERIFY_SETTLE_SECONDS = 10 * 60
CHARGE_VERIFY_MAX_AGE_SECONDS = 20 * 60
# Never re-commit more often than this. Bounds the SolisCloud write rate if
# the inverter genuinely refuses to charge (BMS limit, fault) to two an hour
# for the length of the window, instead of one per run.
CHARGE_VERIFY_RECOMMIT_COOLDOWN_SECONDS = 30 * 60

# UTC hour whose run re-commits every slot to the inverter even when nothing
# changed in HASS. Set to -1 to disable. Sits just after the plan rolls over
# at UK midnight (see get_rates) so the re-commit lands on a fresh schedule
# rather than a spent one. In BST rollover is the 23:05 UTC run, so this fires
# an hour behind it - the guarantee is only ever "once per 24h", not "on the
# rollover run itself".
FORCE_PUSH_HOUR = int(os.environ.get("FORCE_PUSH_HOUR", "0"))

# Octopus Energy HACS entities.
NEXT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_next_day_rates"
CURRENT_DAY_RATES = "event.octopus_energy_electricity_23j0212061_1012934633517_current_day_rates"

INVERTER_SN = os.environ.get("INVERTER_SN", "1031030229080043")

# HASS sensor carrying SolisCloud's receipt timestamp for the newest inverter
# reading (server-side epoch, NOT the inverter's clock). Only used to judge
# how fresh the HASS telemetry is.
INVERTER_TIMESTAMP_ENTITY = (
    f"sensor.solis_inverter_{INVERTER_SN}_solis_timestamp_measurements_received"
)
# Live battery telemetry used by verify_charging (see the knobs above).
BATTERY_POWER_ENTITY = (
    f"sensor.solis_inverter_{INVERTER_SN}_solis_battery_power")      # W, +ve = charging
BATTERY_SOC_ENTITY = (
    f"sensor.solis_inverter_{INVERTER_SN}_solis_remaining_battery_capacity")  # %

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
#
# The soliscloud integration renames these option strings across versions
# (2026-07-27: "Self-Use" became "Self-Use Mode - Allow Grid Charging"), which
# turned a single hardcoded name into a hard failure and a false "battery not
# charging" alert. Accept any name in the list; write back whichever one the
# select actually offers. Comma-separated, most-preferred first.
STORAGE_MODE_ENTITY = "select.solis_energy_storage_control_switch"
ACCEPTED_STORAGE_MODES = [
    m.strip()
    for m in os.environ.get(
        "REQUIRED_STORAGE_MODE",
        "Self-Use Mode - Allow Grid Charging,Self-Use",
    ).split(",")
    if m.strip()
]


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
    if mode in ACCEPTED_STORAGE_MODES:
        print(f"Storage mode check: {mode} - OK")
        return
    if mode == "unavailable":
        print(f"ERROR: {STORAGE_MODE_ENTITY} is 'unavailable' - solis "
              f"integration cannot reach the inverter. Not writing. "
              f"Investigate.")
        sys.exit(4)
    options = state.get("attributes", {}).get("options", [])
    target = next((m for m in ACCEPTED_STORAGE_MODES if m in options), None)
    if options and target is None:
        print(f"ERROR: none of {ACCEPTED_STORAGE_MODES} are options of "
              f"{STORAGE_MODE_ENTITY}; it offers {options}. The integration "
              f"has renamed the modes again - set REQUIRED_STORAGE_MODE.")
        sys.exit(4)
    target = target or ACCEPTED_STORAGE_MODES[0]

    print(f"WARNING: storage mode is '{mode}', expected one of "
          f"{ACCEPTED_STORAGE_MODES}. Timed charge slots are ignored in "
          f"this mode. Setting '{target}'...")
    hass_service("select", "select_option", {
        "entity_id": STORAGE_MODE_ENTITY,
        "option": target,
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
        if mode in ACCEPTED_STORAGE_MODES:
            print(f"Storage mode fixed: now '{mode}'.")
            return
    print(f"ERROR: storage mode still '{mode}' after write + 180s. "
          f"Inverter will NOT act on charge slots. Investigate.")
    sys.exit(4)


# ---------- Octopus data ----------

def _covers_now(rates, now):
    """True if some slot spans `now` - proves this set is the live day."""
    for r in rates:
        start = datetime.fromisoformat(r["start"])
        end = datetime.fromisoformat(r["end"])
        if start <= now < end:
            return True
    return False


def _fetch_rates(entity, label):
    try:
        return hass_get(entity)["attributes"].get("rates", []) or []
    except Exception as e:
        print(f"{label} rates unavailable: {e}")
        return []


def get_rates():
    """Fetch the rate set for the day we are planning.

    The plan rolls over at midnight, not at ~16:00 when Octopus publishes the
    next day. Switching the moment next-day appears would blank any cheap slot
    still to come later today, because apply_schedule() rewrites all three
    slots from scratch on every run. So the rule is simply: use whichever set
    actually contains the current half-hour.
    """
    now = datetime.now(timezone.utc)

    current = _fetch_rates(CURRENT_DAY_RATES, "Current-day")
    if current and _covers_now(current, now):
        print(f"Using current-day rates ({len(current)} slots)")
        return current

    # The Octopus integration rolls its entities over a few minutes after local
    # midnight (observed ~00:07), so the 00:05 run still sees current-day
    # holding yesterday while next-day holds the day we are now in. Both
    # entities refresh in the same tick, so they cannot disagree about which
    # day is which - this can only ever pick today, never a day early.
    nxt = _fetch_rates(NEXT_DAY_RATES, "Next-day")
    if nxt and _covers_now(nxt, now):
        print(f"Using next-day rates ({len(nxt)} slots) - current-day has not "
              f"rolled over yet.")
        return nxt

    # Nothing covers now. Only accept a set that is still ahead of us; writing
    # a day that is already spent would leave the inverter holding stale
    # windows, which is worse than leaving the existing schedule alone.
    if nxt and datetime.fromisoformat(nxt[0]["start"]) > now:
        print(f"No rate set covers now - using upcoming next-day rates "
              f"({len(nxt)} slots).")
        return nxt

    print("No rate set covers now or the future - refusing to reprogram the "
          "inverter from a spent schedule.")
    return []


def get_planning_slots():
    """Slots we are allowed to plan against, in chronological order.

    Everything still to come today, plus tomorrow once today has no cheap slot
    left. Tomorrow is admitted only after today is exhausted, so a cheap slot
    tonight can never be displaced by a cheaper one tomorrow - there are only
    MAX_SLOTS slots and tomorrow would otherwise win on price alone.

    Admitting tomorrow as soon as today is spent also arms it early. Inverter
    slots are bare HH:MM values that recur daily, so writing tomorrow's 00:00
    window during the 23:05 run starts it exactly on midnight instead of
    catching it five minutes late on the following run.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)

    # A slot lives on the inverter as a bare HH:MM that recurs daily, so
    # writing one arms its NEXT occurrence - which is the slot we actually
    # mean only while it starts inside 24h. The cutoff applies to the whole
    # pool, not just the pre-armed tail: when the current-day entity is broken
    # get_rates() falls through to next-day, and its later slots would be
    # 24-32h out and fire TODAY at a rate nobody priced.
    today = [r for r in get_rates()
             if datetime.fromisoformat(r["end"]) > now
             and datetime.fromisoformat(r["start"]) < cutoff]
    if not today:
        return []

    if any(r["value_inc_vat"] <= MAX_RATE and in_charge_window(r["start"])
           for r in today):
        return today

    # Same 24h rule applied to the pre-armed tail: tomorrow 14:00 written at
    # 08:05 today would fire at 14:00 TODAY. Tomorrow 00:00 is always inside
    # 24h, which is exactly the case worth arming ahead of the rollover.
    #
    # The horizon floor is separate: it stops the post-midnight fallback (when
    # get_rates() already returned next-day because the Octopus entities have
    # not rolled over yet) from appending that same set twice.
    horizon = max(datetime.fromisoformat(r["end"]) for r in today)
    ahead = []
    for r in _fetch_rates(NEXT_DAY_RATES, "Next-day"):
        start = datetime.fromisoformat(r["start"])
        if horizon <= start < cutoff:
            ahead.append(r)
    if ahead:
        print(f"  Nothing cheap left today - arming {len(ahead)} slot(s) from "
              f"tomorrow that fall inside the next 24h.")
    return today + ahead


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
    """Find runs of consecutive cheap slots inside the allowed charge window.

    Up to MAX_SLOTS windows, prefer cheapest avg. The caller is responsible
    for passing only slots that have not already ended.

    Sort on the parsed datetime, not the raw ISO string: on the October
    fall-back day the repeated hour yields '01:00+01:00' and '01:00+00:00',
    which sort in the wrong order as text and would split one cheap run into
    two competing windows."""
    cheap = sorted(
        [r for r in rates
         if r["value_inc_vat"] <= MAX_RATE and in_charge_window(r["start"])],
        key=lambda r: datetime.fromisoformat(r["start"]),
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

def _last_genuine_inverter_reading():
    """Newest (epoch, received_at) pair from recorder history where the
    sensor value actually changed.

    Not the live state's last_changed: HA rewrites last_changed to the
    restart time when it restores a sensor, so a restore landing mid-stall
    makes a stale reading look freshly received. A value transition in
    history is a real arrival with a real time, and a restore never produces
    one (same value in, same value out). Returns None if no transition
    exists in the window.
    """
    since = (datetime.now(timezone.utc)
             - timedelta(seconds=CLOCK_HISTORY_WINDOW_SECONDS)).isoformat()
    hist = _request(
        "GET",
        f"{HASS_URL}/api/history/period/{since}"
        f"?filter_entity_id={INVERTER_TIMESTAMP_ENTITY}"
        f"&minimal_response&no_attributes",
    ) or []
    records = hist[0] if hist else []
    # Compare numeric values only: an 'unavailable' blip followed by the same
    # stale value coming back is not an arrival, it is the integration
    # reconnecting to a feed that is still stalled.
    numeric = []
    for r in records:
        try:
            numeric.append((float(r["state"]),
                            datetime.fromisoformat(r["last_changed"])))
        except (KeyError, TypeError, ValueError):
            continue
    for i in range(len(numeric) - 1, 0, -1):
        if numeric[i][0] != numeric[i - 1][0]:
            return numeric[i]
    return None


def verify_inverter_clock():
    """Read the inverter's own clock and derive the offset we must write
    slot times in. Abort if the clock has genuinely drifted.

    The inverter compares bare HH:MM slot times against its internal clock,
    so the only zone that matters is the one that clock is set to - not the
    SolisCloud station label (UTC+0 here) and not the timestamp sensor in
    HASS (a server-side receipt time). cid 56 is the inverter's system time
    as a plain string; its distance from real UTC, snapped to the nearest
    whole hour, is the zone offset, and the remainder is drift.

    A DST change on the inverter shows up here as a new offset on the next
    run, and the schedule is rewritten with it within 15 min.

    The HASS feed staleness check stays as a warning only; it never proved
    anything about the clock.
    """
    global INVERTER_UTC_OFFSET
    raw = None
    try:
        api = SolisCloud()
        api.login()
        # A live read is a 10-30s Modbus round trip; take the midpoint so the
        # drift figure is not biased by the latency.
        t0 = datetime.now(timezone.utc)
        raw, _ = api.read(INVERTER_CLOCK_CID)
        t1 = datetime.now(timezone.utc)
    except SolisError as e:
        print(f"ABORTING: cannot read the inverter clock from SolisCloud ({e}); "
              f"slot writes would not reach it either. Investigate.")
        sys.exit(2)
    now = t0 + (t1 - t0) / 2
    try:
        inverter_dt = datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        print(f"ABORTING: inverter clock (cid {INVERTER_CLOCK_CID}) returned "
              f"{raw!r}, not a timestamp. Clearing the schedule so no stale "
              f"window keeps firing. Investigate.")
        clear_schedule()
        sys.exit(2)

    delta = (inverter_dt - now.replace(tzinfo=None)).total_seconds()
    offset = round(delta / INVERTER_CLOCK_SNAP_SECONDS) * INVERTER_CLOCK_SNAP_SECONDS
    drift = delta - offset
    sign = "+" if offset >= 0 else "-"
    zone = f"UTC{sign}{abs(int(offset)) // 3600:02d}:{(abs(int(offset)) % 3600) // 60:02d}"
    print(f"Inverter clock check: inverter says {inverter_dt:%Y-%m-%d %H:%M:%S}, "
          f"real time {now:%H:%M:%S} UTC -> inverter runs {zone}, "
          f"drift {drift:+.0f}s.")
    if abs(offset) > 14 * 3600 or abs(drift) > INVERTER_CLOCK_ABORT_DRIFT_SECONDS:
        # A schedule written on an earlier run recurs daily as bare HH:MM, so
        # leaving it in place while aborting every 15 min would keep charging
        # at times nobody priced. Clear first (needs no offset), then stop.
        print(f"ABORTING: inverter clock is {offset / 3600:+.1f}h from UTC with "
              f"{drift:+.0f}s left over - not a usable time zone reading. "
              f"Clearing the schedule so no stale window keeps firing; correct "
              f"the inverter time in the SolisCloud app.")
        clear_schedule()
        sys.exit(2)
    if abs(drift) > INVERTER_CLOCK_WARN_DRIFT_SECONDS:
        print(f"WARN: inverter clock drift {drift:+.0f}s - slot boundaries will "
              f"land that far into the neighbouring rate. Correct the inverter "
              f"time in the SolisCloud app.")
    INVERTER_UTC_OFFSET = timedelta(seconds=offset)

    reading = _last_genuine_inverter_reading()
    if reading is None:
        print(f"WARN: no new HASS inverter reading in the last "
              f"{CLOCK_HISTORY_WINDOW_SECONDS // 3600}h - the SolisCloud feed "
              f"into HASS is down. Programming continues (it goes direct); "
              f"the charge-outcome check cannot run until it recovers.")
        return
    _, received = reading
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age = (now - received).total_seconds()
    if age > STALE_FEED_WARN_SECONDS:
        print(f"WARN: newest HASS inverter reading is {age / 3600:.1f}h old - "
              f"the SolisCloud feed has stalled; continuing.")


def _rate_at(rates, dt_utc):
    """Return rate (£/kWh inc VAT) at a given UTC datetime, or None."""
    for r in rates:
        s = datetime.fromisoformat(r["start"])
        e = datetime.fromisoformat(r["end"])
        if s <= dt_utc < e:
            return r["value_inc_vat"]
    return None


def verify_written_slots_are_cheap(charge_windows, rates):
    """Round-trip check: the start and end instant of each window we are
    about to write must both sit in a cheap Octopus rate. Catches merge or
    rounding mistakes before they reach the inverter (zone handling is
    covered separately by verify_inverter_clock).

    Free-electricity slots skip this check because their rates are
    irrelevant (Octopus pays you to use power).
    """
    for w in charge_windows:
        if "free" in (w.get("tag") or ""):
            continue
        start_dt = datetime.fromisoformat(w["start"]).astimezone(timezone.utc)
        end_dt = datetime.fromisoformat(w["end"]).astimezone(timezone.utc)

        # Re-derive the start instant from the very string that will be
        # written (inverter HH:MM on the window's inverter-clock date, minus
        # the offset). A sign error in the offset would pass every check
        # that only looks at ISO instants; this one does not.
        written = _inverter_hhmm(w["start"])
        inv_date = (start_dt + INVERTER_UTC_OFFSET).date()
        hh, mm = (int(x) for x in written.split(":"))
        rederived = (datetime.combine(inv_date, datetime.min.time(), tzinfo=timezone.utc)
                     + timedelta(hours=hh, minutes=mm) - INVERTER_UTC_OFFSET)
        if rederived != start_dt.replace(second=0, microsecond=0):
            print(f"ABORTING: slot string {written} re-derives to "
                  f"{rederived:%Y-%m-%d %H:%M UTC}, not {start_dt:%Y-%m-%d %H:%M UTC} - "
                  f"inverter-clock conversion is wrong.")
            return False

        # Sample the rate just inside start and just inside end.
        probe_start = rederived
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


# ---------- SolisCloud API (direct inverter I/O) ----------

# Slots are written straight to SolisCloud's open API instead of through the
# HACS integration's time/number/button entities. Discovered 2026-09-14 by
# driving the SolisCloud web app: every timed slot has its own on/off switch
# (register 43707, one bit per slot, cids 5916-5927) and a slot whose switch
# is off is ignored no matter what its window says. The integration has no
# entity for those switches at all, so the only slot that ever worked was the
# one Andrei had toggled on by hand in the app. Going direct also removes the
# stage-then-commit-button dance that silently no-op'd in July.
#
# Register map for this model (S5-EH1P4.6K-L, model 3103), taken from the
# web app's own atReadBatch call. 6214-family and 5948-family both address
# the current register (verified: writing 6214 reads back on 5948), the web
# app uses the 62xx ids so we do too.
SOLIS_DOMAIN = os.environ.get("SOLIS_DOMAIN", "https://www.soliscloud.com:13333")
SOLIS_KEY_ID = os.environ["SOLIS_KEY_ID"]
SOLIS_KEY_SECRET = os.environ["SOLIS_KEY_SECRET"].encode("utf-8")
SOLIS_USERNAME = os.environ["SOLIS_USERNAME"]
SOLIS_PASSWORD = os.environ["SOLIS_PASSWORD"]

SLOT_CIDS = {
    "charge": [
        {"switch": "5916", "time": "5946", "current": "6214"},
        {"switch": "5917", "time": "5949", "current": "6225"},
        {"switch": "5918", "time": "5952", "current": "6247"},
    ],
    "discharge": [
        {"switch": "5922", "time": "5964", "current": "6302"},
        {"switch": "5923", "time": "5968", "current": "6313"},
        {"switch": "5924", "time": "5972", "current": "6324"},
    ],
}
# SolisCloud codes that mean "datalogger busy, try again", not "bad request".
SOLIS_RETRY_CODES = {"B0173", "B0600"}
SOLIS_RETRIES = 4
SOLIS_RETRY_SLEEP = 15


class SolisError(RuntimeError):
    pass


class SolisCloud:
    """Minimal signed client for /v2/api/{login,atRead,control}."""

    def __init__(self):
        self._token = None

    def _post_once(self, path, body, with_token=False):
        import base64
        import hashlib
        import hmac
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        content_md5 = base64.b64encode(hashlib.md5(raw).digest()).decode()
        date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        to_sign = "POST\n" + content_md5 + "\napplication/json\n" + date + "\n" + path
        sign = base64.b64encode(
            hmac.new(SOLIS_KEY_SECRET, to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode()
        headers = {
            "Content-MD5": content_md5,
            "Content-Type": "application/json",
            "Date": date,
            "Authorization": f"API {SOLIS_KEY_ID}:{sign}",
        }
        if with_token:
            headers["token"] = self._token
        req = urllib.request.Request(SOLIS_DOMAIN + path, data=raw,
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())

    def _post(self, path, body, with_token=False):
        """Read-type call with plain retries (reads are idempotent)."""
        last = None
        for attempt in range(1, SOLIS_RETRIES + 1):
            try:
                result = self._post_once(path, body, with_token)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"{type(e).__name__}: {e}"
                print(f"    solis {path} attempt {attempt}: {last}")
                time.sleep(SOLIS_RETRY_SLEEP)
                continue
            code = str(result.get("code"))
            if code in SOLIS_RETRY_CODES:
                last = f"{code} {result.get('msg')}"
                print(f"    solis {path} attempt {attempt}: datalogger busy ({last})")
                time.sleep(SOLIS_RETRY_SLEEP)
                continue
            return result
        raise SolisError(f"{path} failed after {SOLIS_RETRIES} attempts: {last}")

    def login(self):
        import hashlib
        result = self._post("/v2/api/login", {
            "username": SOLIS_USERNAME,
            "password": hashlib.md5(SOLIS_PASSWORD.encode("utf-8")).hexdigest(),
        })
        token = result.get("csrfToken")
        if str(result.get("code")) != "0" or not token:
            raise SolisError(f"login failed: {result.get('code')} {result.get('msg')}")
        self._token = token

    def read(self, cid):
        """Live Modbus read. Returns (value, raw_register) as strings. For a
        bit cid (slot switch) value is the bit and raw is the whole register."""
        result = self._post("/v2/api/atRead", {"inverterSn": INVERTER_SN, "cid": cid},
                            with_token=True)
        if str(result.get("code")) != "0":
            raise SolisError(f"atRead {cid}: {result.get('code')} {result.get('msg')}")
        data = result.get("data") or {}
        return str(data.get("msg")), str(data.get("yuanzhi"))

    def write(self, cid, value, raw=None, must_write=False):
        """Write one cid. Bit cids need the register's current raw value
        (the web app calls it yuanzhi) or the API refuses with B0218
        "read first then set". Returns the Modbus echo string.

        A control POST that times out or comes back "datalogger busy" may
        still have been applied, and for a bit cid the raw we hold is then
        stale. So never blindly re-POST: read the cid back first, and
        otherwise retry with the fresh raw. "Already holds the value" counts
        as success only when the caller knew the value differed beforehand
        (a normal diff-driven write). A forced rewrite (must_write) exists
        precisely because the stored value is already right and the inverter
        is not acting on it, so there equality proves nothing and the POST
        is retried until it is acknowledged.
        """
        value = str(value)
        last = None
        for attempt in range(1, SOLIS_RETRIES + 1):
            body = {"inverterSn": INVERTER_SN, "cid": cid, "value": value}
            if raw is not None:
                body["yuanzhi"] = str(raw)
            try:
                result = self._post_once("/v2/api/control", body, with_token=True)
                code = str(result.get("code"))
                if code not in SOLIS_RETRY_CODES:
                    if code != "0":
                        raise SolisError(f"control {cid}={value}: {code} {result.get('msg')}")
                    entry = (result.get("data") or [{}])[0]
                    if str(entry.get("code")) != "0":
                        raise SolisError(f"control {cid}={value}: inverter returned "
                                         f"{entry.get('code')} {entry.get('msg')}")
                    return str(entry.get("recv") or "")
                last = f"{code} {result.get('msg')}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"{type(e).__name__}: {e}"
            print(f"    solis control {cid} attempt {attempt}: {last} - reading back")
            time.sleep(SOLIS_RETRY_SLEEP)
            current, fresh_raw = self.read(cid)
            if _same_value(current, value) and not must_write:
                print(f"    solis control {cid}: already {value} - write had landed")
                return ""
            if raw is not None:
                raw = fresh_raw
        raise SolisError(f"control {cid}={value} failed after {SOLIS_RETRIES} attempts: {last}")


def _same_value(a, b):
    """'7' == '7.0', '08:00-15:00' == '08:00-15:00'."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _echo_register_value(recv):
    """Value from a Modbus fn-06 write echo like '0106AABB000119F7', or None."""
    if len(recv) >= 12 and recv[2:4] == "06":
        try:
            return int(recv[8:12], 16)
        except ValueError:
            return None
    return None


# ---------- Slot programming ----------

def split_at_inverter_midnight(windows):
    """Split any window that would straddle 00:00 on the inverter's clock.

    Slots are bare HH:MM and how this firmware treats start > end (wrap,
    reject, never fire) has never been established, so never write one.
    Each half keeps the original tag; the first half ends at 23:59 inverter
    time. Uses the offset measured this run, so must follow
    verify_inverter_clock().
    """
    if INVERTER_UTC_OFFSET is None:
        raise RuntimeError("inverter clock offset unknown - "
                           "verify_inverter_clock() must run first")
    out = []
    for w in windows:
        start = datetime.fromisoformat(w["start"]).astimezone(timezone.utc)
        end = datetime.fromisoformat(w["end"]).astimezone(timezone.utc)
        inv_start = start + INVERTER_UTC_OFFSET
        inv_end = end + INVERTER_UTC_OFFSET
        # Midnight on the inverter clock after the window starts.
        boundary = (inv_start + timedelta(days=1)).replace(hour=0, minute=0,
                                                          second=0, microsecond=0)
        if inv_end < boundary:
            out.append(w)
            continue
        if inv_end == boundary:
            # Ends exactly at inverter midnight: "23:30-00:00" reads as start >
            # end on the inverter, so write it ending 23:59 instead.
            out.append(dict(w, end=(end - timedelta(minutes=1)).isoformat()))
            continue
        cut = boundary - INVERTER_UTC_OFFSET  # back to a real instant
        first = dict(w, end=(cut - timedelta(minutes=1)).isoformat())
        second = dict(w, start=cut.isoformat())
        print(f"  Window {inv_start:%H:%M}-{inv_end:%H:%M} inverter clock crosses "
              f"its midnight - split into {inv_start:%H:%M}-23:59 and "
              f"00:00-{inv_end:%H:%M}.")
        out.extend([first, second])
    return out


def _inverter_hhmm(iso_str):
    """ISO timestamp with offset -> HH:MM on the inverter's own clock.

    The API takes bare HH:MM and the inverter compares it to its internal
    time, so the value must be in whatever zone that clock is set to:
    UTC plus the offset verify_inverter_clock() measured this run.
    """
    if INVERTER_UTC_OFFSET is None:
        raise RuntimeError("inverter clock offset unknown - "
                           "verify_inverter_clock() must run first")
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc) + INVERTER_UTC_OFFSET).strftime("%H:%M")


def _norm_hhmm(v):
    """'5:30:00' / '05:30' style HA time states -> 'HH:MM', else None."""
    if not isinstance(v, str) or ":" not in v:
        return None
    parts = v.split(":")
    try:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    except (ValueError, IndexError):
        return None


def _hass_cached_slot(kind, slot):
    """(window, current) as the HACS integration last saw them on the cloud,
    or (None, None) when unknown. Cheap; the live atRead costs 10-30s per cid
    on the datalogger, and the switch bits are the only thing the integration
    cannot tell us."""
    start = hass_get_optional(f"time.solis_timed_{kind}_start_{slot}")
    end = hass_get_optional(f"time.solis_timed_{kind}_end_{slot}")
    cur = hass_get_optional(f"number.solis_timed_{kind}_current_{slot}")
    s = _norm_hhmm(start.get("state")) if start else None
    e = _norm_hhmm(end.get("state")) if end else None
    window = f"{s}-{e}" if s and e else None
    try:
        current = int(float(cur.get("state"))) if cur else None
    except (TypeError, ValueError):
        current = None
    return window, current


ALL_SLOTS = {(kind, slot)
             for kind in ("charge", "discharge")
             for slot in range(1, MAX_SLOTS + 1)}

DISABLED_WINDOW = "00:00-00:00"


def _desired_slots(charge_windows, discharge_windows):
    out = {}
    for kind, windows, amps in (("charge", charge_windows, CHARGE_CURRENT),
                                ("discharge", discharge_windows, DISCHARGE_CURRENT)):
        for i in range(MAX_SLOTS):
            slot = i + 1
            if i < len(windows):
                w = windows[i]
                out[(kind, slot)] = {
                    "enabled": True,
                    "window": f"{_inverter_hhmm(w['start'])}-{_inverter_hhmm(w['end'])}",
                    "current": amps,
                    "tag": w.get("tag", ""),
                }
            else:
                out[(kind, slot)] = {"enabled": False, "window": DISABLED_WINDOW,
                                     "current": 0, "tag": ""}
    return out


def apply_schedule(charge_windows, discharge_windows, force=False):
    """Program up to MAX_SLOTS charge + MAX_SLOTS discharge slots on the
    inverter and return the number of registers written.

    Per slot: current, window and the on/off switch. Writes go out in three
    global phases - every switch-off first, then every window/current, then
    every switch-on - so a failure part-way can never leave a stale slot
    live next to a new one.

    What is compared against what: switch bits are always read live (the
    integration cannot see them). For slots we want ENABLED the window and
    current are read live too - those are the registers that make the
    inverter pull from the grid, and the HACS cache has lied before. For
    slots we want disabled the cache is good enough: with the switch off
    the window is inert, and the daily force pass tidies it anyway.
    force=True rewrites everything (the daily re-commit, every abort path,
    and the charge-outcome re-commit).

    Any read or write the cloud or the inverter rejects exits non-zero: a
    slot half-programmed is the silent failure this function exists to make
    loud.
    """
    desired = _desired_slots(charge_windows, discharge_windows)
    try:
        return _apply(desired, force)
    except SolisError as e:
        print(f"ERROR: SolisCloud: {e}")
        print("ERROR: inverter programming failed part-way; slots may be "
              "inconsistent. The next run retries the whole schedule.")
        sys.exit(5)


def _apply(desired, force):
    api = SolisCloud()
    api.login()

    # Current state. One live read per switch (the value is the bit and the
    # read also returns the whole switch register, needed as yuanzhi).
    switch_on = {}
    raw = None
    for key in sorted(desired):
        kind, slot = key
        value, raw = api.read(SLOT_CIDS[kind][slot - 1]["switch"])
        switch_on[key] = value == "1"
    live = {}
    for key in sorted(desired):
        kind, slot = key
        if desired[key]["enabled"]:
            cids = SLOT_CIDS[kind][slot - 1]
            window, _ = api.read(cids["time"])
            current, _ = api.read(cids["current"])
            live[key] = (window, current)
        else:
            live[key] = _hass_cached_slot(kind, slot)

    offs, values, ons = [], [], []  # (label, cid, value)
    for key in sorted(desired):
        kind, slot = key
        d = desired[key]
        cids = SLOT_CIDS[kind][slot - 1]
        window, current = live[key]
        want_current = str(d["current"])
        need_current = force or current is None or not _same_value(current, want_current)
        need_window = force or window != d["window"]
        need_switch = force or switch_on[key] != d["enabled"]
        extra = f"  ({d['tag']})" if d["tag"] else ""
        state = (f"{d['window']} inverter clock @ {d['current']}A, switch on" if d["enabled"]
                 else "disabled")
        print(f"  {kind.capitalize()} slot {slot}: {state}{extra}")
        if need_switch:
            (ons if d["enabled"] else offs).append(
                (f"{kind} {slot} switch", cids["switch"], "1" if d["enabled"] else "0"))
        if need_current:
            values.append((f"{kind} {slot} current", cids["current"], want_current))
        if need_window:
            values.append((f"{kind} {slot} window", cids["time"], d["window"]))

    writes = offs + values + ons
    if not writes:
        print("  Inverter already matches - nothing to write.")
        return 0

    print(f"  Writing {len(writes)} register(s) to the inverter "
          f"({len(offs)} off, {len(values)} values, {len(ons)} on)...")
    for label, cid, value in writes:
        is_switch = cid in {c["switch"] for kinds in SLOT_CIDS.values() for c in kinds}
        if is_switch:
            recv = api.write(cid, value, raw=raw, must_write=force)
            new_raw = _echo_register_value(recv)
            if new_raw is None:
                _, raw = api.read(cid)  # echo unparseable - refetch
            else:
                raw = str(new_raw)
        else:
            recv = api.write(cid, value, must_write=force)
        print(f"    wrote {label} = {value}  (echo {recv or '-'})")

    # Read back what matters: every switch bit, plus window and current of
    # every enabled slot.
    bad = []
    for key in sorted(desired):
        kind, slot = key
        cids = SLOT_CIDS[kind][slot - 1]
        d = desired[key]
        value, _ = api.read(cids["switch"])
        if (value == "1") != d["enabled"]:
            bad.append(f"{kind} {slot} switch reads {value}")
        if d["enabled"]:
            window, _ = api.read(cids["time"])
            current, _ = api.read(cids["current"])
            if window != d["window"]:
                bad.append(f"{kind} {slot} window reads {window}, want {d['window']}")
            if not _same_value(current, d["current"]):
                bad.append(f"{kind} {slot} current reads {current}, want {d['current']}")
    if bad:
        print(f"ERROR: read-back mismatch after write: {'; '.join(bad)}")
        sys.exit(5)
    print("  Read-back OK.")
    return len(writes)


def clear_schedule():
    """Disable every slot, rewriting all registers regardless of cache."""
    apply_schedule([], [], force=True)


# ---------- Charge outcome check ----------

def _float_state(entity_id):
    """(value, last_updated) for a numeric sensor, or (None, None)."""
    state = hass_get_optional(entity_id)
    try:
        value = float(state.get("state"))
        updated = datetime.fromisoformat(state["last_updated"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None, None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return value, updated


# Re-commit cooldown lives in a HASS state we set ourselves (POST
# /api/states). The job has no storage of its own and HASS is the one place
# every run can see. Not persisted across HASS restarts, which only means
# one extra re-commit - acceptable for a rate limit.
RECOMMIT_MARKER_ENTITY = "sensor.solis_charge_last_recommit"


def _last_recommit():
    state = hass_get_optional(RECOMMIT_MARKER_ENTITY)
    if not state:
        return None
    try:
        t = datetime.fromisoformat(state["state"])
    except (KeyError, TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _mark_recommit(now):
    _request("POST", f"{HASS_URL}/api/states/{RECOMMIT_MARKER_ENTITY}", {
        "state": now.isoformat(),
        "attributes": {"friendly_name": "Solis charge last re-commit"},
    })


def verify_charging(charge_windows, discharge_windows, now, pushed_this_run=False):
    """Inside a programmed charge window, prove the inverter is actually
    grid-charging; rewrite every slot if it is not.

    Neither "cloud read-back matches" nor "HASS matches" is proof (2026-09-12:
    slot stored, read back all morning, battery sat at 19% - the slot switch
    bit was off). The only proof is the inverter behaving: inside a window
    the battery must be taking roughly the timed-charge current.

    Returns True if verified OK or not applicable, False if a re-commit was
    needed (the run still exits 0 - the remedy has been applied and the next
    run re-verifies).
    """
    if pushed_this_run:
        # Any telemetry we have predates the write we just sent; judging the
        # inverter on it would double-write every time a window is programmed
        # while already open. The next run (15 min) verifies.
        print("Charge check: slots were written this run - verifying next run.")
        return True
    active = None
    for w in charge_windows:
        start = datetime.fromisoformat(w["start"]).astimezone(timezone.utc)
        end = datetime.fromisoformat(w["end"]).astimezone(timezone.utc)
        if start + timedelta(seconds=CHARGE_VERIFY_SETTLE_SECONDS) <= now < end:
            active = (start, end, w)
            break
    if active is None:
        return True
    start, end, w = active
    label = f"{start:%H:%M}-{end:%H:%M} UTC ({w.get('tag') or 'charge'})"

    reading = _last_genuine_inverter_reading()
    if reading is None:
        print(f"Charge check: inside window {label} but no inverter reading "
              f"in the last {CLOCK_HISTORY_WINDOW_SECONDS // 3600}h - cannot verify.")
        return True
    _, received = reading
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    age = (now - received).total_seconds()
    if age > CHARGE_VERIFY_MAX_AGE_SECONDS:
        print(f"Charge check: inside window {label} but newest inverter reading "
              f"is {age / 60:.0f} min old - cannot verify until the feed catches up.")
        return True
    if received < start + timedelta(seconds=CHARGE_VERIFY_SETTLE_SECONDS):
        print(f"Charge check: inside window {label} but newest reading predates "
              f"the settle period - waiting for a fresh one.")
        return True

    watts, watts_at = _float_state(BATTERY_POWER_ENTITY)
    soc, soc_at = _float_state(BATTERY_SOC_ENTITY)
    if watts is None or soc is None:
        print(f"Charge check: battery sensors unreadable "
              f"(power={watts}, soc={soc}) - cannot verify.")
        return True
    # The battery sensors must be as current as the timestamp reading: a
    # sensor stuck on an old value while the feed keeps ticking would
    # otherwise condemn the inverter every run for the whole window.
    telemetry_floor = received - timedelta(seconds=CHARGE_VERIFY_MAX_AGE_SECONDS)
    if min(watts_at, soc_at) < telemetry_floor:
        print(f"Charge check: battery telemetry older than the newest inverter "
              f"reading (power {watts_at:%H:%M:%S}, soc {soc_at:%H:%M:%S}, "
              f"reading {received:%H:%M:%S}) - cannot verify.")
        return True
    if soc >= CHARGE_VERIFY_FULL_SOC:
        print(f"Charge check: window {label}, battery {soc:.0f}% - full enough, OK.")
        return True
    if watts >= CHARGE_VERIFY_MIN_WATTS:
        print(f"Charge check: window {label}, battery taking {watts:.0f} W "
              f"at {soc:.0f}% - charging, OK.")
        return True

    print(f"WARNING: window {label} is active, battery at {soc:.0f}% but only "
          f"{watts:+.0f} W ({age / 60:.0f} min old reading). The inverter is not "
          f"running the timed charge.")
    last = _last_recommit()
    if last and (now - last).total_seconds() < CHARGE_VERIFY_RECOMMIT_COOLDOWN_SECONDS:
        print(f"  Last re-commit was {(now - last).total_seconds() / 60:.0f} min ago "
              f"- giving the inverter until the cooldown expires before another.")
        return False
    print("  Rewriting every slot.")
    apply_schedule(charge_windows, discharge_windows, force=True)
    _mark_recommit(now)  # only a delivered rewrite starts the cooldown
    return False


# ---------- Main ----------

def main():
    now_utc = datetime.now(timezone.utc)
    print(f"Solis Optimizer - run at {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Max Agile rate threshold: {MAX_RATE*100:.0f}p/kWh")
    print("Slot times below are in the inverter's own clock (read live each run).")
    print()

    # 0. Read the inverter clock and fix the zone offset every slot time is
    # written in. Nothing is programmed if the clock cannot be read or has
    # drifted.
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
    # Spent slots never reach the picker: the inverter stores a slot as a bare
    # HH:MM with no date, so a window that has already passed would simply
    # re-fire at the same clock time tomorrow at rates nobody checked.
    upcoming = get_planning_slots()
    if not upcoming:
        # Clear rather than just bail. Leaving yesterday's windows programmed
        # means the inverter keeps grid-charging at times nobody has priced;
        # an empty schedule just falls back to self-use, which is safe.
        print("ERROR: no usable rate data - clearing schedule")
        clear_schedule()
        sys.exit(1)

    print(f"  Planning horizon: {upcoming[0]['start'][11:16]} -> "
          f"{upcoming[-1]['end'][11:16]} ({len(upcoming)} slots)")
    cheap_count = sum(1 for r in upcoming if r["value_inc_vat"] <= MAX_RATE)
    eligible_count = sum(
        1 for r in upcoming
        if r["value_inc_vat"] <= MAX_RATE and in_charge_window(r["start"])
    )
    print(f"  {cheap_count} cheap slots (<={MAX_RATE*100:.0f}p), "
          f"{len(upcoming) - cheap_count} expensive slots")
    window_desc = ("24h - no time restriction"
                   if CHARGE_WINDOW_START == CHARGE_WINDOW_END
                   else f"{CHARGE_WINDOW_START}-{CHARGE_WINDOW_END} UK local")
    print(f"  Grid-charge window: {window_desc} "
          f"-> {eligible_count} cheap slot(s) eligible to charge")
    print(f"  Rate range: {min(r['value_inc_vat'] for r in upcoming)*100:.1f}p "
          f"- {max(r['value_inc_vat'] for r in upcoming)*100:.1f}p")
    print()

    # 4. Build charge windows.
    cheap_windows = find_cheap_windows(upcoming)
    for w in cheap_windows:
        w["tag"] = f"agile {w['avg_rate']*100:.1f}p"

    if cheap_windows:
        print("Running safety check on cheap windows...")
        if not safety_check(cheap_windows, upcoming):
            print("ABORTING - expensive rate detected inside a charge window")
            clear_schedule()
            sys.exit(1)
        print("  PASSED")
        print()

    # Free Electricity always wins - tag and prepend, then merge.
    free_tagged = [{"start": s["start"], "end": s["end"], "tag": "free"} for s in free_sessions]
    all_charge = split_at_inverter_midnight(merge_windows(free_tagged + cheap_windows))
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
    discharge_windows = split_at_inverter_midnight(merge_windows(discharge_windows))[:MAX_SLOTS]

    # 5.5. Round-trip check: confirm each charge window we're about to write
    # actually lands in cheap rates. Catches merge/rounding mistakes before
    # they hit the inverter.
    if all_charge:
        print("Round-trip rate check on charge windows...")
        if not verify_written_slots_are_cheap(all_charge, upcoming):
            print("Clearing all charge slots due to round-trip check failure.")
            # Force the commit: if HASS already reads 00:00 the staged clear
            # would push nothing and the inverter would keep charging on the
            # windows this check just rejected.
            apply_schedule([], discharge_windows, force=True)
            sys.exit(3)
        print("  PASSED")
        print()

    # 6. Apply. Re-write everything once a day as insurance, gated on the
    # first run of the hour since the job runs every 15 min.
    force = now_utc.hour == FORCE_PUSH_HOUR and now_utc.minute < 15
    if force:
        print(f"Daily force-push window ({FORCE_PUSH_HOUR:02d}:00 UTC) - "
              f"rewriting every slot regardless of current state.")
    print("Programming inverter:")
    written = apply_schedule(all_charge, discharge_windows, force=force)
    print()

    # 7. Prove it. A write the cloud accepted is not a slot the inverter is
    # running; inside a live window the battery has to be charging.
    verify_charging(all_charge, discharge_windows, datetime.now(timezone.utc),
                    pushed_this_run=written > 0)
    print("Done")


if __name__ == "__main__":
    main()
