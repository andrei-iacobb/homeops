"""Speech text and APL datasources for the electricity briefing.

A tariff strip puts the two real hourly averages above the next half-hour
prices so the household can choose when to use electricity.

Everything emitted here is built from validated Decimal prices and UTC
datetimes, never from raw Home Assistant text, so the APL document can bind
labels directly. No message in this module ever carries a token, an entity id,
a response body or an exception string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tariffs import (
    Briefing,
    Rate,
    clock,
    clock_zone,
    format_pence,
    is_london_tomorrow,
    zone_abbrev,
)

TITLE = "Electricity · p/kWh inc. VAT"
UNIT = "p/kWh"
SCOPE_NOTE = "among available rates"


@dataclass(frozen=True)
class Message:
    speech: str
    headline: str
    detail: str


MESSAGES: dict[str, Message] = {
    "link": Message(
        speech=(
            "To read your electricity rates I need access to Home Assistant. "
            "Please open the Alexa app and link your account for this skill, then ask again."
        ),
        headline="Link Home Assistant",
        detail="Open the Alexa app and link your account for this skill to see electricity rates.",
    ),
    "unavailable": Message(
        speech=(
            "I couldn't reach your electricity rates just now. "
            "Please try again in a few minutes."
        ),
        headline="Rates unavailable",
        detail="Home Assistant did not answer in time. Try again in a few minutes.",
    ),
    "deadline": Message(
        speech=(
            "Your electricity rates are taking too long to load, so I've stopped waiting. "
            "Please try again shortly."
        ),
        headline="Rates unavailable",
        detail="Loading took too long. Try again shortly.",
    ),
    "no_coverage": Message(
        speech=(
            "I have no published electricity rate for the current time, "
            "so I can't give you a price right now. Please try again later."
        ),
        headline="No rate for now",
        detail="No published half-hour rate covers the current time.",
    ),
    "malformed": Message(
        speech=(
            "The electricity rate data looks wrong right now, so I won't read it out. "
            "Please try again later."
        ),
        headline="Rate data looks wrong",
        detail="The rate feed failed validation and was not shown.",
    ),
    "help": Message(
        speech=(
            "I read your Octopus electricity rates from Home Assistant. "
            "Ask for the electricity price and I'll tell you the average for this hour and the next, "
            "plus the cheapest and dearest half hours I can see. "
            "If I say you need to link your account, open the Alexa app and link Home Assistant for this skill. "
            "What would you like?"
        ),
        headline="Home energy",
        detail=(
            "Ask for the electricity price to hear this hour and next hour averages, "
            "then the cheapest and dearest half hours coming up."
        ),
    ),
    "goodbye": Message(speech="Okay.", headline="", detail=""),
    "fallback": Message(
        speech=(
            "I can tell you the electricity price. Try asking for the electricity price, "
            "or say help."
        ),
        headline="Home energy",
        detail="Ask for the electricity price.",
    ),
    "unexpected": Message(
        speech="Something went wrong reading your electricity rates. Please try again later.",
        headline="Rates unavailable",
        detail="Something went wrong. Try again later.",
    ),
}


# --------------------------------------------------------------------------
# Speech
# --------------------------------------------------------------------------


def _pence_speech(value: Decimal, with_unit: bool = False) -> str:
    text = format_pence(value)
    if text.startswith("-"):
        text = "minus " + text[1:]
    return f"{text} pence per kilowatt hour" if with_unit else f"{text} pence"


def _clock_speech(moment: datetime, reference: datetime, with_zone: bool = False) -> str:
    label = clock_zone(moment) if with_zone else clock(moment)
    if is_london_tomorrow(moment, reference):
        return f"{label} tomorrow"
    return label


def _needs_zone_labels(briefing: Briefing) -> bool:
    moments = [briefing.current_hour.start, briefing.current_hour.end,
               briefing.next_hour.start, briefing.next_hour.end]
    moments.extend(moment for rate in briefing.chart for moment in (rate.start, rate.end))
    return len({zone_abbrev(moment) for moment in moments}) > 1


def briefing_speech(briefing: Briefing) -> str:
    now = briefing.now
    cur = briefing.current_hour
    nxt = briefing.next_hour
    sentences: list[str] = []
    show_zones = _needs_zone_labels(briefing)

    def label(moment):
        return _clock_speech(moment, now, show_zones)

    cur_range = f"between {label(cur.start)} and {label(cur.end)}"
    if cur.average is not None:
        sentences.append(f"Electricity {cur_range} averages {_pence_speech(cur.average, True)}.")
    else:
        sentences.append(
            f"I don't have complete rates for the hour {cur_range}, so there's no average for this hour."
        )

    nxt_range = f"from {label(nxt.start)} to {label(nxt.end)}"
    if nxt.average is not None:
        unit = cur.average is None
        sentences.append(f"The next hour, {nxt_range}, averages {_pence_speech(nxt.average, unit)}.")
    else:
        sentences.append(f"The next hour, {nxt_range}, isn't fully available yet.")

    sentences.append(f"The current half hour rate is {_pence_speech(briefing.current_slot.pence)}.")

    end_label = label(briefing.coverage_end)
    if not show_zones:
        end_label += f" {zone_abbrev(briefing.coverage_end)}"
    cheapest = briefing.cheapest
    peak = briefing.peak
    if len(briefing.chart) > 1:
        sentences.append(
            f"Among the rates I have up to {end_label}, the cheapest half hour starts at "
            f"{label(cheapest.start)} at {_pence_speech(cheapest.pence)}, "
            f"and the dearest at {label(peak.start)} at {_pence_speech(peak.pence)}."
        )
    else:
        sentences.append(f"That is the only rate I have, up to {end_label}.")
    if briefing.truncated:
        sentences.append("Later rates aren't available yet.")
    return " ".join(sentences)


# --------------------------------------------------------------------------
# APL datasources
# --------------------------------------------------------------------------


def _hour_tile(label: str, average: Decimal | None) -> dict[str, Any]:
    if average is None:
        return {"label": label, "value": "—", "unit": "incomplete rates", "available": False}
    return {"label": label, "value": format_pence(average), "unit": UNIT, "available": True}


def _bar_geometry(chart: tuple[Rate, ...]) -> tuple[float, float, list[float], list[float]]:
    max_pos = max((rate.pence for rate in chart if rate.pence > 0), default=Decimal(0))
    max_neg = max((-rate.pence for rate in chart if rate.pence < 0), default=Decimal(0))
    total = max_pos + max_neg
    if total == 0:
        up_share, down_share = 1.0, 0.0
    else:
        up_share = round(float(max_pos / total), 4)
        down_share = round(1.0 - up_share, 4)
    ups: list[float] = []
    downs: list[float] = []
    for rate in chart:
        ups.append(round(float(rate.pence / max_pos), 4) if rate.pence > 0 else 0.0)
        downs.append(round(float(-rate.pence / max_neg), 4) if rate.pence < 0 else 0.0)
    return up_share, down_share, ups, downs


def briefing_datasource(briefing: Briefing) -> dict[str, Any]:
    now = briefing.now
    cur = briefing.current_hour
    nxt = briefing.next_hour
    up_share, down_share, ups, downs = _bar_geometry(briefing.chart)
    label = clock_zone if _needs_zone_labels(briefing) else clock

    bars: list[dict[str, Any]] = []
    previous_zone = None
    for index, rate in enumerate(briefing.chart):
        zone = zone_abbrev(rate.start)
        bars.append(
            {
                "time": clock(rate.start),
                "zone": zone if zone != previous_zone else "",
                "price": format_pence(rate.pence),
                "up": ups[index],
                "down": downs[index],
                "current": rate is briefing.current_slot,
            }
        )
        previous_zone = zone

    coverage = (
        f"Rates shown {label(briefing.coverage_start)}–{label(briefing.coverage_end)} "
        f"{zone_abbrev(briefing.coverage_end) if label is clock else ''}"
    ).strip()
    if is_london_tomorrow(briefing.coverage_end, now):
        coverage += " (ends tomorrow)"
    if briefing.truncated:
        coverage += " · later rates unavailable"

    summary = (
        f"Cheapest {label(briefing.cheapest.start)} {format_pence(briefing.cheapest.pence)} · "
        f"Peak {label(briefing.peak.start)} {format_pence(briefing.peak.pence)} · {SCOPE_NOTE}"
    )

    return {
        "state": "ok",
        "title": TITLE,
        "asOf": f"Now {clock_zone(now)}",
        "currentHour": _hour_tile(f"This hour {label(cur.start)}–{label(cur.end)}", cur.average),
        "nextHour": _hour_tile(f"Next hour {label(nxt.start)}–{label(nxt.end)}", nxt.average),
        "nowSlot": (
            f"Now {label(briefing.current_slot.start)}–{label(briefing.current_slot.end)} "
            f"{format_pence(briefing.current_slot.pence)} {UNIT}"
        ),
        "bars": bars,
        "barCount": len(bars),
        "upShare": up_share,
        "downShare": down_share,
        "summary": summary,
        "coverage": coverage,
        "headline": "",
        "detail": "",
    }


def message_datasource(message: Message) -> dict[str, Any]:
    return {
        "state": "message",
        "title": TITLE,
        "asOf": "",
        "headline": message.headline,
        "detail": message.detail,
        "bars": [],
        "barCount": 1,
        "upShare": 1.0,
        "downShare": 0.0,
        "summary": "",
        "coverage": "",
    }
