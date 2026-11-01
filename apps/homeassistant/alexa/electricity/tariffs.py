"""Pure time and tariff maths for the Alexa electricity briefing.

Everything in this module is deterministic and network free. It turns the two
Home Assistant Octopus rate feeds (today / tomorrow) into validated half-hour
slots in UTC, then derives the current and next full clock hour averages, the
six hour chart window, and Europe/London labels with BST/GMT abbreviations.

Rules enforced here:
  * every rate has timezone-aware start/end, exactly 30 minutes apart
  * prices are finite JSON numbers (bool rejected) converted GBP -> pence once
  * identical duplicates (same bounds, same price) collapse; conflicting
    duplicates and any overlap are rejected as malformed
  * missing coverage is never treated as zero; an hour average only exists
    when the full 60 minute window is covered without gaps
  * nothing covering "now" means the briefing is unavailable, regardless of
    how much future data exists
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional

SLOT_DURATION = timedelta(minutes=30)
HOUR = timedelta(hours=1)
CHART_HORIZON = timedelta(hours=6)
PENCE_PER_POUND = Decimal(100)
UTC = timezone.utc


class RateDataError(ValueError):
    """Rate data is malformed or self-contradictory. The message is a short safe code."""


class NoCurrentCoverage(Exception):
    """No validated rate slot covers the current instant."""


@dataclass(frozen=True)
class Rate:
    start: datetime
    end: datetime
    pence: Decimal
    is_capped: bool = False

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass(frozen=True)
class HourWindow:
    start: datetime
    end: datetime
    average: Optional[Decimal]


@dataclass(frozen=True)
class Briefing:
    now: datetime
    current_hour: HourWindow
    next_hour: HourWindow
    current_slot: Rate
    chart: tuple[Rate, ...]
    cheapest: Rate
    peak: Rate
    truncated: bool

    @property
    def coverage_start(self) -> datetime:
        return self.chart[0].start

    @property
    def coverage_end(self) -> datetime:
        return self.chart[-1].end


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def pounds_to_pence(gbp: Decimal) -> Decimal:
    """The single place GBP/kWh becomes p/kWh."""
    return gbp * PENCE_PER_POUND


def _parse_instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RateDataError(f"rate_{field}_missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RateDataError(f"rate_{field}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RateDataError(f"rate_{field}_naive")
    return parsed.astimezone(UTC)


def _parse_price(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RateDataError("rate_price_type")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RateDataError("rate_price_not_finite")
        gbp = Decimal(repr(value))
    else:
        gbp = Decimal(value)
    return pounds_to_pence(gbp)


def parse_rate(entry: Any) -> Rate:
    if not isinstance(entry, dict):
        raise RateDataError("rate_not_object")
    start = _parse_instant(entry.get("start"), "start")
    end = _parse_instant(entry.get("end"), "end")
    if end - start != SLOT_DURATION:
        raise RateDataError("rate_duration")
    pence = _parse_price(entry.get("value_inc_vat"))
    capped = entry.get("is_capped", False)
    if not isinstance(capped, bool):
        raise RateDataError("rate_capped_type")
    return Rate(start=start, end=end, pence=pence, is_capped=capped)


def parse_state_rates(state: Any) -> list[Rate]:
    """Extract rates from a Home Assistant /api/states/{id} JSON object.

    A missing ``attributes`` or ``rates`` key means the feed is unpublished and
    yields an empty list. A present but non-list ``rates`` or any malformed
    entry raises RateDataError so coverage is never silently lost.
    """
    if not isinstance(state, dict):
        raise RateDataError("state_not_object")
    attributes = state.get("attributes")
    if attributes is None:
        return []
    if not isinstance(attributes, dict):
        raise RateDataError("attributes_not_object")
    rates = attributes.get("rates")
    if rates is None:
        return []
    if not isinstance(rates, list):
        raise RateDataError("rates_not_list")
    return [parse_rate(entry) for entry in rates]


def merge_rates(*feeds: Iterable[Rate]) -> list[Rate]:
    """Combine feeds chronologically, collapsing identical slots and rejecting conflicts."""
    combined = sorted(
        (rate for feed in feeds for rate in feed),
        key=lambda rate: (rate.start, rate.end, rate.pence),
    )
    merged: list[Rate] = []
    for rate in combined:
        if merged:
            previous = merged[-1]
            if rate.start == previous.start and rate.end == previous.end:
                if rate.pence == previous.pence:
                    continue
                raise RateDataError("rate_conflicting_duplicate")
            if rate.start < previous.end:
                raise RateDataError("rate_overlap")
        merged.append(rate)
    return merged


# --------------------------------------------------------------------------
# Time windows
# --------------------------------------------------------------------------


def to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("naive datetime")
    return moment.astimezone(UTC)


def floor_hour(moment: datetime) -> datetime:
    return to_utc(moment).replace(minute=0, second=0, microsecond=0)


def slot_covering(rates: Iterable[Rate], moment: datetime) -> Optional[Rate]:
    moment = to_utc(moment)
    for rate in rates:
        if rate.start <= moment < rate.end:
            return rate
    return None


def window_average(rates: Iterable[Rate], start: datetime, end: datetime) -> Optional[Decimal]:
    """Duration-weighted mean over [start, end), or None unless fully covered.

    Rates are assumed non-overlapping (see merge_rates). Partial slot overlap at
    the window edges is weighted by the overlapping seconds only.
    """
    start = to_utc(start)
    end = to_utc(end)
    if end <= start:
        raise ValueError("empty window")
    total_seconds = int((end - start).total_seconds())
    covered_seconds = 0
    weighted = Decimal(0)
    for rate in rates:
        low = max(rate.start, start)
        high = min(rate.end, end)
        if high <= low:
            continue
        seconds = int((high - low).total_seconds())
        covered_seconds += seconds
        weighted += rate.pence * seconds
    if covered_seconds != total_seconds:
        return None
    return weighted / Decimal(total_seconds)


def chart_slots(
    rates: Iterable[Rate], now: datetime, horizon: timedelta = CHART_HORIZON
) -> tuple[list[Rate], bool]:
    """Contiguous slots intersecting [now, now+horizon), stopping at the first gap.

    Returns the slots and whether the chart was truncated before the horizon.
    """
    now = to_utc(now)
    limit = now + horizon
    chosen: list[Rate] = []
    for rate in sorted(rates, key=lambda rate: rate.start):
        if rate.end <= now or rate.start >= limit:
            continue
        if chosen and rate.start != chosen[-1].end:
            break
        chosen.append(rate)
    truncated = not chosen or chosen[-1].end < limit
    return chosen, truncated


def build_briefing(rates: Iterable[Rate], now: datetime) -> Briefing:
    now = to_utc(now)
    ordered = sorted(rates, key=lambda rate: rate.start)
    current_slot = slot_covering(ordered, now)
    if current_slot is None:
        raise NoCurrentCoverage()
    hour_start = floor_hour(now)
    current_hour = HourWindow(
        hour_start, hour_start + HOUR, window_average(ordered, hour_start, hour_start + HOUR)
    )
    next_hour = HourWindow(
        hour_start + HOUR,
        hour_start + 2 * HOUR,
        window_average(ordered, hour_start + HOUR, hour_start + 2 * HOUR),
    )
    chart, truncated = chart_slots(ordered, now)
    cheapest = min(chart, key=lambda rate: (rate.pence, rate.start))
    peak = max(chart, key=lambda rate: (rate.pence, -rate.start.timestamp()))
    return Briefing(
        now=now,
        current_hour=current_hour,
        next_hour=next_hour,
        current_slot=current_slot,
        chart=tuple(chart),
        cheapest=cheapest,
        peak=peak,
        truncated=truncated,
    )


# --------------------------------------------------------------------------
# Europe/London labelling
# --------------------------------------------------------------------------

BST = timedelta(hours=1)
GMT = timedelta(0)


def _last_sunday_0100_utc(year: int, month: int) -> datetime:
    last_day = datetime(year, month, 31, 1, 0, tzinfo=UTC)
    return last_day - timedelta(days=(last_day.weekday() + 1) % 7)


def uk_rule_offset(moment: datetime) -> tuple[timedelta, str]:
    """UK summer time by rule: 01:00 UTC last Sunday of March to last Sunday of October."""
    moment = to_utc(moment)
    begins = _last_sunday_0100_utc(moment.year, 3)
    ends = _last_sunday_0100_utc(moment.year, 10)
    if begins <= moment < ends:
        return BST, "BST"
    return GMT, "GMT"


def _london_zoneinfo():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/London")
    except Exception:  # noqa: BLE001 - tzdata may be absent on the runtime image
        return None


_LONDON = _london_zoneinfo()


def london(moment: datetime) -> datetime:
    """Aware datetime in Europe/London whose tzname() is BST or GMT."""
    moment = to_utc(moment)
    if _LONDON is not None:
        local = moment.astimezone(_LONDON)
        if local.tzname() in ("BST", "GMT"):
            return local
    offset, name = uk_rule_offset(moment)
    return moment.astimezone(timezone(offset, name))


def clock(moment: datetime) -> str:
    return london(moment).strftime("%H:%M")


def zone_abbrev(moment: datetime) -> str:
    return london(moment).tzname() or ""


def clock_zone(moment: datetime) -> str:
    local = london(moment)
    return f"{local.strftime('%H:%M')} {local.tzname()}"


def is_london_tomorrow(moment: datetime, relative_to: datetime) -> bool:
    return london(moment).date() > london(relative_to).date()


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def format_pence(value: Decimal, places: int = 1) -> str:
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    return format(rounded, "f")
