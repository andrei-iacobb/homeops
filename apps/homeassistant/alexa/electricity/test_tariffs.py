"""Unit tests for tariffs.py (pure maths, no network)."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import tariffs
from tariffs import (
    NoCurrentCoverage,
    Rate,
    RateDataError,
    build_briefing,
    chart_slots,
    clock_zone,
    floor_hour,
    format_pence,
    is_london_tomorrow,
    london,
    merge_rates,
    parse_rate,
    parse_state_rates,
    slot_covering,
    uk_rule_offset,
    window_average,
)

UTC = timezone.utc
HALF = timedelta(minutes=30)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def entry(start: datetime, gbp, capped: bool = False, minutes: int = 30) -> dict:
    return {
        "start": iso(start),
        "end": iso(start + timedelta(minutes=minutes)),
        "value_inc_vat": gbp,
        "is_capped": capped,
    }


def entries(start: datetime, prices) -> list[dict]:
    return [entry(start + index * HALF, price) for index, price in enumerate(prices)]


def rate(start: datetime, pence) -> Rate:
    return Rate(start=start, end=start + HALF, pence=Decimal(str(pence)))


def rates(start: datetime, prices) -> list[Rate]:
    return [rate(start + index * HALF, price) for index, price in enumerate(prices)]


T10 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


class ParseRateTests(unittest.TestCase):
    def test_gbp_becomes_pence_exactly_once(self):
        parsed = parse_rate(entry(T10, 0.2461))
        self.assertEqual(parsed.pence, Decimal("24.61"))
        self.assertNotEqual(parsed.pence, Decimal("2461"))

    def test_integer_zero_is_a_valid_price(self):
        parsed = parse_rate(entry(T10, 0))
        self.assertEqual(parsed.pence, Decimal("0"))

    def test_negative_price_is_preserved(self):
        self.assertEqual(parse_rate(entry(T10, -0.0123)).pence, Decimal("-1.23"))

    def test_offset_timestamps_normalise_to_utc(self):
        parsed = parse_rate(
            {
                "start": "2026-07-01T11:00:00+01:00",
                "end": "2026-07-01T11:30:00+01:00",
                "value_inc_vat": 0.1,
            }
        )
        self.assertEqual(parsed.start, datetime(2026, 7, 1, 10, 0, tzinfo=UTC))
        self.assertEqual(parsed.end, datetime(2026, 7, 1, 10, 30, tzinfo=UTC))
        self.assertFalse(parsed.is_capped)

    def test_rejects_non_finite_and_non_numeric_prices(self):
        for bad in (math.nan, math.inf, -math.inf, True, False, "0.2", None, [0.2], {"v": 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(RateDataError):
                    parse_rate(entry(T10, bad))

    def test_rejects_timezone_naive_timestamps(self):
        bad = entry(T10, 0.2)
        bad["start"] = "2026-11-01T10:00:00"
        with self.assertRaises(RateDataError):
            parse_rate(bad)
        bad = entry(T10, 0.2)
        bad["end"] = "2026-11-01T10:30:00"
        with self.assertRaises(RateDataError):
            parse_rate(bad)

    def test_rejects_missing_or_invalid_timestamps(self):
        for field, value in (("start", None), ("end", ""), ("start", "yesterday"), ("end", 5)):
            with self.subTest(field=field, value=value):
                bad = entry(T10, 0.2)
                bad[field] = value
                with self.assertRaises(RateDataError):
                    parse_rate(bad)

    def test_rejects_wrong_or_negative_duration(self):
        with self.assertRaises(RateDataError):
            parse_rate(entry(T10, 0.2, minutes=60))
        with self.assertRaises(RateDataError):
            parse_rate(entry(T10, 0.2, minutes=0))
        with self.assertRaises(RateDataError):
            parse_rate(entry(T10, 0.2, minutes=-30))

    def test_rejects_non_object_entry_and_non_bool_capped(self):
        with self.assertRaises(RateDataError):
            parse_rate(["2026-11-01T10:00:00Z"])
        with self.assertRaises(RateDataError):
            parse_rate(entry(T10, 0.2, capped="yes"))


class ParseStateTests(unittest.TestCase):
    def test_unpublished_feed_shapes_yield_empty(self):
        self.assertEqual(parse_state_rates({"state": "unknown"}), [])
        self.assertEqual(parse_state_rates({"state": "unavailable", "attributes": {}}), [])
        self.assertEqual(parse_state_rates({"attributes": {"rates": None}}), [])

    def test_malformed_containers_fail(self):
        for bad in ("string", None, {"attributes": "x"}, {"attributes": {"rates": {"a": 1}}}):
            with self.subTest(bad=bad):
                with self.assertRaises(RateDataError):
                    parse_state_rates(bad)

    def test_single_malformed_entry_fails_whole_feed(self):
        good = entries(T10, [0.1, 0.2, 0.3])
        good[1]["value_inc_vat"] = math.nan
        with self.assertRaises(RateDataError):
            parse_state_rates({"attributes": {"rates": good}})

    def test_valid_feed_parses_in_order(self):
        parsed = parse_state_rates({"attributes": {"rates": entries(T10, [0.1, 0.2])}})
        self.assertEqual([r.pence for r in parsed], [Decimal("10.0"), Decimal("20.0")])


class MergeTests(unittest.TestCase):
    def test_midnight_rollover_duplicates_collapse(self):
        midnight = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
        today = rates(midnight - 2 * HALF, [10, 11, 12, 13])  # 23:00 .. 01:00
        tomorrow = rates(midnight, [12, 13, 14])  # 00:00 .. 01:30 overlaps two identical slots
        merged = merge_rates(today, tomorrow)
        self.assertEqual(len(merged), 5)
        self.assertEqual([r.pence for r in merged], [Decimal(v) for v in (10, 11, 12, 13, 14)])
        self.assertEqual(merged[0].start, midnight - 2 * HALF)
        self.assertEqual(merged[-1].end, midnight + 3 * HALF)

    def test_conflicting_duplicate_rejected(self):
        with self.assertRaises(RateDataError):
            merge_rates([rate(T10, 10)], [rate(T10, 11)])

    def test_overlap_rejected(self):
        shifted = Rate(T10 + timedelta(minutes=15), T10 + timedelta(minutes=45), Decimal(5))
        with self.assertRaises(RateDataError):
            merge_rates([rate(T10, 10), shifted])

    def test_unsorted_input_becomes_chronological(self):
        merged = merge_rates([rate(T10 + HALF, 2), rate(T10, 1)])
        self.assertEqual([r.pence for r in merged], [Decimal(1), Decimal(2)])


class WindowAverageTests(unittest.TestCase):
    def test_unequal_overlap_weights_by_seconds(self):
        slots = rates(T10, [10, 20, 40])  # 10:00, 10:30, 11:00
        start = T10 + timedelta(minutes=15)
        average = window_average(slots, start, start + timedelta(hours=1))
        # 15 min @10 + 30 min @20 + 15 min @40 = (150 + 600 + 600) / 60
        self.assertEqual(average, Decimal("22.5"))

    def test_full_hour_of_two_slots_is_mean(self):
        self.assertEqual(window_average(rates(T10, [10, 30]), T10, T10 + timedelta(hours=1)), Decimal(20))

    def test_partial_hour_is_unavailable_not_estimated(self):
        self.assertIsNone(window_average(rates(T10, [10]), T10, T10 + timedelta(hours=1)))

    def test_gap_inside_hour_is_unavailable_not_zero(self):
        slots = [rate(T10, 10), rate(T10 + 2 * HALF, 10)]
        self.assertIsNone(window_average(slots, T10, T10 + timedelta(hours=1)))

    def test_all_zero_hour_averages_zero(self):
        self.assertEqual(window_average(rates(T10, [0, 0]), T10, T10 + timedelta(hours=1)), Decimal(0))

    def test_negative_prices_average_correctly(self):
        self.assertEqual(window_average(rates(T10, [-10, 30]), T10, T10 + timedelta(hours=1)), Decimal(10))

    def test_rejects_empty_window(self):
        with self.assertRaises(ValueError):
            window_average([], T10, T10)


class HourBoundaryTests(unittest.TestCase):
    def test_floor_hour_uses_utc(self):
        local = datetime(2026, 7, 1, 11, 45, 12, tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(floor_hour(local), datetime(2026, 7, 1, 10, 0, tzinfo=UTC))

    def test_mid_hour_windows_are_full_clock_hours(self):
        start = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        prices = [1, 2, 10, 20, 30, 40, 50, 60]  # 09:00 .. 13:00
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        briefing = build_briefing(rates(start, prices), now)
        self.assertEqual(briefing.current_hour.start, datetime(2026, 11, 1, 10, 0, tzinfo=UTC))
        self.assertEqual(briefing.current_hour.end, datetime(2026, 11, 1, 11, 0, tzinfo=UTC))
        self.assertEqual(briefing.current_hour.average, Decimal(15))
        self.assertEqual(briefing.next_hour.start, datetime(2026, 11, 1, 11, 0, tzinfo=UTC))
        self.assertEqual(briefing.next_hour.end, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
        self.assertEqual(briefing.next_hour.average, Decimal(35))
        self.assertEqual(briefing.current_slot.pence, Decimal(20))

    def test_next_hour_incomplete_is_unavailable(self):
        start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        briefing = build_briefing(rates(start, [10, 20, 30]), now)  # ends 11:30
        self.assertEqual(briefing.current_hour.average, Decimal(15))
        self.assertIsNone(briefing.next_hour.average)

    def test_current_hour_incomplete_but_current_slot_known(self):
        start = datetime(2026, 11, 1, 10, 30, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        briefing = build_briefing(rates(start, [20, 30, 40]), now)
        self.assertIsNone(briefing.current_hour.average)
        self.assertEqual(briefing.next_hour.average, Decimal(35))
        self.assertEqual(briefing.current_slot.start, start)

    def test_slot_covering_is_half_open(self):
        slots = rates(T10, [1, 2])
        self.assertEqual(slot_covering(slots, T10).pence, Decimal(1))
        self.assertEqual(slot_covering(slots, T10 + HALF).pence, Decimal(2))
        self.assertIsNone(slot_covering(slots, T10 + 2 * HALF))


class ChartTests(unittest.TestCase):
    def test_thirteen_bars_when_mid_slot_with_full_coverage(self):
        start = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        slots = rates(start, range(1, 25))  # 08:00 .. 20:00
        chart, truncated = chart_slots(slots, now)
        self.assertEqual(len(chart), 13)
        self.assertFalse(truncated)
        self.assertEqual(chart[0].start, datetime(2026, 11, 1, 10, 30, tzinfo=UTC))
        self.assertEqual(chart[-1].start, datetime(2026, 11, 1, 16, 30, tzinfo=UTC))
        self.assertEqual(chart[-1].end, datetime(2026, 11, 1, 17, 0, tzinfo=UTC))

    def test_twelve_bars_when_exactly_on_slot_boundary(self):
        start = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 30, tzinfo=UTC)
        chart, truncated = chart_slots(rates(start, range(24)), now)
        self.assertEqual(len(chart), 12)
        self.assertFalse(truncated)

    def test_gap_truncates_chart_and_excludes_later_cheaper_slot(self):
        start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        slots = rates(start, [20, 25, 30, 35])  # to 12:00
        slots += rates(start + timedelta(hours=3), [1, 2])  # 13:00 onward after a gap
        briefing = build_briefing(slots, now)
        self.assertTrue(briefing.truncated)
        self.assertEqual(briefing.coverage_end, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
        self.assertEqual(len(briefing.chart), 3)
        self.assertEqual(briefing.cheapest.pence, Decimal(25))
        self.assertEqual(briefing.peak.pence, Decimal(35))

    def test_unpublished_tomorrow_truncates_at_midnight_bst(self):
        day_start = datetime(2026, 7, 14, 23, 0, tzinfo=UTC)  # Octopus day = midnight BST
        now = datetime(2026, 7, 15, 21, 45, tzinfo=UTC)  # 22:45 BST
        briefing = build_briefing(rates(day_start, [10] * 48), now)
        self.assertTrue(briefing.truncated)
        self.assertEqual(briefing.coverage_end, datetime(2026, 7, 15, 23, 0, tzinfo=UTC))
        self.assertEqual(clock_zone(briefing.coverage_end), "00:00 BST")
        self.assertTrue(is_london_tomorrow(briefing.coverage_end, now))
        self.assertEqual(len(briefing.chart), 3)  # 21:30, 22:00, 22:30 UTC

    def test_no_current_coverage_even_with_future_data(self):
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        future = rates(datetime(2026, 11, 2, 0, 0, tzinfo=UTC), [5] * 48)
        with self.assertRaises(NoCurrentCoverage):
            build_briefing(future, now)

    def test_no_current_coverage_when_only_past_data(self):
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        with self.assertRaises(NoCurrentCoverage):
            build_briefing(rates(T10 - 4 * HALF, [5, 5, 5, 5]), now)

    def test_cheapest_and_peak_ties_pick_earliest(self):
        start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 10, tzinfo=UTC)
        briefing = build_briefing(rates(start, [5, 9, 5, 9]), now)
        self.assertEqual(briefing.cheapest.start, start)
        self.assertEqual(briefing.peak.start, start + HALF)

    def test_single_slot_coverage(self):
        now = datetime(2026, 11, 1, 10, 10, tzinfo=UTC)
        briefing = build_briefing([rate(T10, 7)], now)
        self.assertEqual(len(briefing.chart), 1)
        self.assertTrue(briefing.truncated)
        self.assertIs(briefing.cheapest, briefing.peak)
        self.assertIsNone(briefing.current_hour.average)


class LondonLabelTests(unittest.TestCase):
    def test_spring_forward_labels(self):
        self.assertEqual(clock_zone(datetime(2026, 3, 29, 0, 30, tzinfo=UTC)), "00:30 GMT")
        self.assertEqual(clock_zone(datetime(2026, 3, 29, 1, 0, tzinfo=UTC)), "02:00 BST")
        self.assertEqual(clock_zone(datetime(2026, 3, 29, 1, 30, tzinfo=UTC)), "02:30 BST")

    def test_fall_back_labels_disambiguate_repeated_hour(self):
        self.assertEqual(clock_zone(datetime(2026, 10, 25, 0, 30, tzinfo=UTC)), "01:30 BST")
        self.assertEqual(clock_zone(datetime(2026, 10, 25, 1, 0, tzinfo=UTC)), "01:00 GMT")
        self.assertEqual(clock_zone(datetime(2026, 10, 25, 1, 30, tzinfo=UTC)), "01:30 GMT")

    def test_rule_offset_boundaries(self):
        self.assertEqual(uk_rule_offset(datetime(2026, 3, 29, 0, 59, tzinfo=UTC))[1], "GMT")
        self.assertEqual(uk_rule_offset(datetime(2026, 3, 29, 1, 0, tzinfo=UTC))[1], "BST")
        self.assertEqual(uk_rule_offset(datetime(2026, 10, 25, 0, 59, tzinfo=UTC))[1], "BST")
        self.assertEqual(uk_rule_offset(datetime(2026, 10, 25, 1, 0, tzinfo=UTC))[1], "GMT")
        self.assertEqual(uk_rule_offset(datetime(2027, 3, 28, 1, 0, tzinfo=UTC))[1], "BST")
        self.assertEqual(uk_rule_offset(datetime(2027, 10, 31, 1, 0, tzinfo=UTC))[1], "GMT")

    def test_rule_matches_zoneinfo_when_available(self):
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo("Europe/London")
        except Exception:  # noqa: BLE001
            self.skipTest("tzdata not available")
        moment = datetime(2026, 1, 1, 0, 15, tzinfo=UTC)
        end = datetime(2027, 1, 1, tzinfo=UTC)
        while moment < end:
            expected = moment.astimezone(zone)
            offset, name = uk_rule_offset(moment)
            self.assertEqual(offset, expected.utcoffset(), moment)
            self.assertEqual(name, expected.tzname(), moment)
            moment += timedelta(hours=1)

    def test_london_falls_back_to_rule_without_zoneinfo(self):
        original = tariffs._LONDON
        tariffs._LONDON = None
        try:
            self.assertEqual(clock_zone(datetime(2026, 7, 1, 12, 0, tzinfo=UTC)), "13:00 BST")
            self.assertEqual(clock_zone(datetime(2026, 12, 1, 12, 0, tzinfo=UTC)), "12:00 GMT")
        finally:
            tariffs._LONDON = original

    def test_midnight_rollover_tomorrow_detection(self):
        now = datetime(2026, 11, 1, 23, 45, tzinfo=UTC)  # 23:45 GMT
        self.assertTrue(is_london_tomorrow(now + HALF, now))
        self.assertFalse(is_london_tomorrow(now, now))
        summer_now = datetime(2026, 7, 1, 22, 45, tzinfo=UTC)  # 23:45 BST
        self.assertTrue(is_london_tomorrow(summer_now + HALF, summer_now))
        self.assertEqual(london(summer_now + HALF).strftime("%H:%M"), "00:15")

    def test_naive_input_rejected(self):
        with self.assertRaises(ValueError):
            london(datetime(2026, 7, 1, 12, 0))


class FormatTests(unittest.TestCase):
    def test_rounding_half_up_one_place(self):
        self.assertEqual(format_pence(Decimal("24.65")), "24.7")
        self.assertEqual(format_pence(Decimal("24.64")), "24.6")
        self.assertEqual(format_pence(Decimal("7")), "7.0")

    def test_negative_and_negative_zero(self):
        self.assertEqual(format_pence(Decimal("-2.34")), "-2.3")
        self.assertEqual(format_pence(Decimal("-0.04")), "0.0")
        self.assertEqual(format_pence(Decimal("0")), "0.0")


if __name__ == "__main__":
    unittest.main()
