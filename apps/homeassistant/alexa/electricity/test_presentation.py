"""Unit tests for presentation.py (speech and APL datasource shaping)."""

from __future__ import annotations

import json
import math
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import presentation
from tariffs import Rate, build_briefing

UTC = timezone.utc
HALF = timedelta(minutes=30)


def rates(start: datetime, prices) -> list[Rate]:
    return [
        Rate(start + index * HALF, start + (index + 1) * HALF, Decimal(str(price)))
        for index, price in enumerate(prices)
    ]


def strict_json(payload) -> str:
    return json.dumps(payload, allow_nan=False)


class GeometryTests(unittest.TestCase):
    def briefing(self, prices, now=None):
        start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        # 10:10 keeps the first slot inside the chart window.
        return build_briefing(rates(start, prices), now or datetime(2026, 11, 1, 10, 10, tzinfo=UTC))

    def test_mixed_sign_shares_sum_to_one_and_scale_to_max(self):
        data = presentation.briefing_datasource(self.briefing([20, 10, -5, 40, 0]))
        self.assertEqual(len(data["bars"]), 5)
        self.assertAlmostEqual(data["upShare"] + data["downShare"], 1.0, places=3)
        self.assertAlmostEqual(data["upShare"], 40 / 45, places=3)
        ups = [bar["up"] for bar in data["bars"]]
        downs = [bar["down"] for bar in data["bars"]]
        self.assertEqual(max(ups), 1.0)
        self.assertEqual(max(downs), 1.0)
        self.assertEqual(data["bars"][0]["up"], 0.5)
        self.assertEqual(data["bars"][2]["up"], 0.0)
        self.assertEqual(data["bars"][2]["down"], 1.0)
        self.assertEqual(data["bars"][3]["down"], 0.0)
        self.assertEqual(data["bars"][4]["up"], 0.0)
        self.assertEqual(data["bars"][4]["down"], 0.0)
        self.assertEqual(data["bars"][4]["price"], "0.0")
        strict_json(data)

    def test_all_negative_uses_only_down_region(self):
        data = presentation.briefing_datasource(self.briefing([-1, -2, -3]))
        self.assertEqual(data["upShare"], 0.0)
        self.assertEqual(data["downShare"], 1.0)
        self.assertEqual([bar["up"] for bar in data["bars"]], [0.0, 0.0, 0.0])
        self.assertEqual(data["bars"][2]["down"], 1.0)
        self.assertEqual(data["bars"][0]["price"], "-1.0")
        strict_json(data)

    def test_all_zero_has_safe_denominator_and_zero_bars(self):
        data = presentation.briefing_datasource(self.briefing([0, 0, 0]))
        self.assertEqual(data["upShare"], 1.0)
        self.assertEqual(data["downShare"], 0.0)
        for bar in data["bars"]:
            self.assertEqual(bar["up"], 0.0)
            self.assertEqual(bar["down"], 0.0)
            self.assertEqual(bar["price"], "0.0")
        self.assertEqual(data["currentHour"]["value"], "0.0")
        strict_json(data)

    def test_no_nan_anywhere(self):
        data = presentation.briefing_datasource(self.briefing([1.5, 2.5]))
        encoded = strict_json(data)
        self.assertNotIn("NaN", encoded)
        self.assertNotIn("Infinity", encoded)
        for bar in data["bars"]:
            self.assertTrue(math.isfinite(bar["up"]) and math.isfinite(bar["down"]))


class DatasourceTests(unittest.TestCase):
    def test_bar_labels_mark_current_slot_and_first_zone(self):
        start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        data = presentation.briefing_datasource(build_briefing(rates(start, range(1, 20)), now))
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["barCount"], 13)
        self.assertEqual(len(data["bars"]), 13)
        self.assertEqual([bar["current"] for bar in data["bars"]].count(True), 1)
        self.assertTrue(data["bars"][0]["current"])
        self.assertEqual(data["bars"][0]["time"], "10:30")
        self.assertEqual(data["bars"][0]["zone"], "GMT")
        self.assertEqual([bar["zone"] for bar in data["bars"][1:]], [""] * 12)
        self.assertEqual(data["currentHour"]["label"], "This hour 10:00–11:00")
        self.assertEqual(data["nextHour"]["label"], "Next hour 11:00–12:00")
        self.assertEqual(data["asOf"], "Now 10:45 GMT")
        self.assertIn("p/kWh", data["title"])
        self.assertIn("among available rates", data["summary"])
        self.assertIn("Rates shown 10:30–17:00 GMT", data["coverage"])
        self.assertNotIn("not published", data["coverage"])

    def test_zone_label_repeats_at_dst_fall_back(self):
        start = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
        now = datetime(2026, 10, 25, 0, 10, tzinfo=UTC)  # 01:10 BST
        data = presentation.briefing_datasource(build_briefing(rates(start, [10] * 20), now))
        zones = [(bar["time"], bar["zone"]) for bar in data["bars"]]
        self.assertEqual(zones[0], ("01:00", "BST"))
        self.assertEqual(zones[1], ("01:30", ""))
        self.assertEqual(zones[2], ("01:00", "GMT"))
        self.assertEqual(zones[3], ("01:30", ""))

    def test_zone_label_repeats_at_dst_spring_forward(self):
        start = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
        now = datetime(2026, 3, 29, 0, 10, tzinfo=UTC)  # 00:10 GMT
        data = presentation.briefing_datasource(build_briefing(rates(start, [10] * 20), now))
        zones = [(bar["time"], bar["zone"]) for bar in data["bars"]]
        self.assertEqual(zones[0], ("00:00", "GMT"))
        self.assertEqual(zones[1], ("00:30", ""))
        self.assertEqual(zones[2], ("02:00", "BST"))

    def test_unavailable_hours_and_truncation_are_explicit(self):
        start = datetime(2026, 11, 1, 10, 30, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        data = presentation.briefing_datasource(build_briefing(rates(start, [20, 30, 40]), now))
        self.assertFalse(data["currentHour"]["available"])
        self.assertEqual(data["currentHour"]["unit"], "incomplete rates")
        self.assertTrue(data["nextHour"]["available"])
        self.assertEqual(data["nextHour"]["value"], "35.0")
        self.assertIn("later rates unavailable", data["coverage"])
        self.assertIn("Rates shown 10:30–12:00 GMT", data["coverage"])

    def test_message_datasource_is_readable(self):
        data = presentation.message_datasource(presentation.MESSAGES["unavailable"])
        self.assertEqual(data["state"], "message")
        self.assertEqual(data["headline"], "Rates unavailable")
        self.assertTrue(data["detail"])
        self.assertEqual(data["barCount"], 1)
        strict_json(data)


class SpeechTests(unittest.TestCase):
    def test_full_briefing_speech(self):
        start = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        prices = [1, 2, 10, 20, 30, 40, 5, 60, 70, 80, 90, 95, 99, 99, 99, 99]
        speech = presentation.briefing_speech(build_briefing(rates(start, prices), now))
        self.assertIn("between 10:00 and 11:00 averages 15.0 pence per kilowatt hour", speech)
        self.assertIn("from 11:00 to 12:00, averages 35.0 pence", speech)
        self.assertIn("current half hour rate is 20.0 pence", speech)
        self.assertIn("cheapest half hour starts at 12:00 at 5.0 pence", speech)
        self.assertIn("dearest at 15:00 at 99.0 pence", speech)
        self.assertNotIn("published", speech)
        self.assertNotIn("<", speech)

    def test_incomplete_hours_and_truncation_are_spoken(self):
        start = datetime(2026, 11, 1, 10, 30, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
        speech = presentation.briefing_speech(build_briefing(rates(start, [20, 30]), now))
        self.assertIn("don't have complete rates for the hour between 10:00 and 11:00", speech)
        self.assertIn("from 11:00 to 12:00, isn't fully available yet", speech)
        self.assertIn("up to 11:30 GMT", speech)
        self.assertIn("aren't available yet", speech)

    def test_clock_change_intervals_and_extrema_are_unambiguous(self):
        for date, first, end, cheapest in [
            ((2026, 10, 25), "01:00 BST", "01:00 GMT", "01:00 GMT"),
            ((2026, 3, 29), "00:00 GMT", "02:00 BST", "02:00 BST"),
        ]:
            with self.subTest(date=date):
                start = datetime(*date, 0, 0, tzinfo=UTC)
                briefing = build_briefing(rates(start, [10, 20, 5, 50] + [20] * 16), start + timedelta(minutes=40))
                speech = presentation.briefing_speech(briefing)
                data = presentation.briefing_datasource(briefing)
                self.assertIn(f"between {first} and {end} averages 15.0", speech)
                self.assertIn(f"cheapest half hour starts at {cheapest}", speech)
                self.assertEqual(data["currentHour"]["label"], f"This hour {first}–{end}")
                self.assertIn(f"Cheapest {cheapest}", data["summary"])
                self.assertIn("GMT", data["nowSlot"])
                self.assertIn("BST", data["nowSlot"])

    def test_negative_price_spoken_as_minus(self):
        start = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 10, 10, tzinfo=UTC)
        speech = presentation.briefing_speech(build_briefing(rates(start, [-3, -1, 4, 4]), now))
        self.assertIn("averages minus 2.0 pence", speech)
        self.assertIn("cheapest half hour starts at 10:00 at minus 3.0 pence", speech)

    def test_tomorrow_marker_across_midnight(self):
        start = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
        now = datetime(2026, 11, 1, 23, 40, tzinfo=UTC)
        speech = presentation.briefing_speech(build_briefing(rates(start, [10, 10, 10, 10]), now))
        self.assertIn("between 23:00 and 00:00 tomorrow", speech)
        self.assertIn("from 00:00 tomorrow to 01:00 tomorrow", speech)


if __name__ == "__main__":
    unittest.main()
