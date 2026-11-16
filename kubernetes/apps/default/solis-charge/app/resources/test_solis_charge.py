"""Offline regressions for stale inverter slots during Home Assistant outages."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error


with patch.dict(os.environ, {key: "test-only" for key in (
    "HASS_URL", "HASS_TOKEN", "SOLIS_KEY_ID", "SOLIS_KEY_SECRET",
    "SOLIS_USERNAME", "SOLIS_PASSWORD",
)}):
    spec = importlib.util.spec_from_file_location(
        "solis", Path(__file__).with_name("solis_agile_charge.py"))
    solis = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(solis)


def slot(start, end, rate):
    return {"start": start, "end": end, "value_inc_vat": rate}


class ProposalTests(unittest.TestCase):
    def test_dp_uses_slightly_dearer_slot_to_stay_within_three_runs(self):
        start = solis.datetime(2026, 11, 17, tzinfo=solis.timezone.utc)
        rates = [0.01, 0.10, 0.02, 0.10, 0.03, 0.10, 0.04]
        pool = [
            slot(
                (start + solis.timedelta(minutes=30 * index)).isoformat(),
                (start + solis.timedelta(minutes=30 * (index + 1))).isoformat(),
                rate,
            )
            for index, rate in enumerate(rates)
        ]

        selected = solis.select_proposal_slots(pool, 4)

        self.assertEqual(
            [item["value_inc_vat"] for item in selected],
            [0.01, 0.10, 0.02, 0.03],
        )
        self.assertLessEqual(len(solis._proposal_windows(selected, 1.3)), 3)

    def test_run_crossing_uk_midnight_counts_as_two_inverter_slots(self):
        # GMT in November, so UK midnight is 00:00 UTC. A 23:00-01:00 run is
        # two inverter slots once split_at_inverter_midnight cuts it.
        start = solis.datetime(2026, 11, 17, 22, tzinfo=solis.timezone.utc)
        rates = [0.30, 0.05, 0.05, 0.05, 0.05, 0.30, 0.06, 0.30, 0.07]
        pool = [
            slot(
                (start + solis.timedelta(minutes=30 * index)).isoformat(),
                (start + solis.timedelta(minutes=30 * (index + 1))).isoformat(),
                rate,
            )
            for index, rate in enumerate(rates)
        ]

        selected = solis.select_proposal_slots(pool, 6)
        windows = solis._proposal_windows(selected, 1.3)

        self.assertLessEqual(len(windows), 3)
        with patch.object(solis, "INVERTER_UTC_OFFSET", solis.timedelta(0)):
            split = solis.split_at_inverter_midnight(
                [{"start": w["start"], "end": w["end"]} for w in windows])
        self.assertEqual(len(split), len(windows))

    def test_dp_orders_the_repeated_dst_hour_by_instant(self):
        pool = [
            slot("2026-10-25T01:00:00+00:00", "2026-10-25T01:30:00+00:00", 0.03),
            slot("2026-10-25T01:30:00+01:00", "2026-10-25T01:00:00+00:00", 0.02),
            slot("2026-10-25T01:00:00+01:00", "2026-10-25T01:30:00+01:00", 0.01),
        ]

        selected = solis.select_proposal_slots(pool, 3)
        windows = solis._proposal_windows(selected, 1.3)

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["start"], "2026-10-25T00:00:00+00:00")
        self.assertEqual(windows[0]["end"], "2026-10-25T01:30:00+00:00")

    def test_dp_returns_the_most_slots_that_fit_when_request_is_impossible(self):
        pool = [
            slot(f"2026-11-17T{hour:02d}:00:00+00:00",
                 f"2026-11-17T{hour:02d}:30:00+00:00", rate)
            for hour, rate in enumerate((0.05, 0.04, 0.03, 0.02, 0.01), start=1)
        ]

        selected = solis.select_proposal_slots(pool, 5)

        self.assertEqual(len(selected), 3)
        self.assertEqual(
            [item["value_inc_vat"] for item in selected],
            [0.03, 0.02, 0.01],
        )

    def test_plan_code_round_trip(self):
        windows = [
            {"start": "2026-11-17T01:00:00+00:00",
             "end": "2026-11-17T03:30:00+00:00"},
            {"start": "2026-11-17T23:30:00+00:00",
             "end": "2026-11-18T01:00:00+00:00"},
        ]

        code = solis.format_plan_code(windows)

        self.assertEqual(code, "20261117T0100Z-0330Z;20261117T2330Z-0100Z")
        self.assertEqual(solis.parse_plan_code(code), windows)

    def test_uk_time_fallback_uses_last_sunday_dst_rule(self):
        with patch.object(solis, "UK_TIMEZONE", None):
            self.assertEqual(
                solis._uk_local_datetime("2026-03-29T00:30:00+00:00").strftime("%H:%M"),
                "00:30",
            )
            self.assertEqual(
                solis._uk_local_datetime("2026-03-29T01:00:00+00:00").strftime("%H:%M"),
                "02:00",
            )
            self.assertEqual(
                solis._uk_local_datetime("2026-10-25T01:00:00+00:00").strftime("%H:%M"),
                "01:00",
            )

    def test_proposal_publishes_costs_comparison_and_soc(self):
        now = solis.datetime(2026, 11, 17, tzinfo=solis.timezone.utc)
        pool = [
            slot("2026-11-17T00:00:00+00:00",
                 "2026-11-17T00:30:00+00:00", 0.01),
            slot("2026-11-17T00:30:00+00:00",
                 "2026-11-17T01:00:00+00:00", 0.20),
            slot("2026-11-17T01:00:00+00:00",
                 "2026-11-17T01:30:00+00:00", 0.10),
        ]
        with patch.object(solis, "PROPOSAL_FLOOR_SOC", 50), patch.object(
                solis, "BATTERY_CAPACITY_KWH", 2.6), patch.object(
                solis, "CHARGE_KW", 2.6), patch.object(
                solis, "_float_state", return_value=(42.3, now)), patch.object(
                solis, "_request") as request, contextlib.redirect_stdout(io.StringIO()):
            code, attributes = solis.publish_charge_proposal(pool, now=now)

        self.assertEqual(code, "20261117T0100Z-0130Z")
        self.assertEqual(attributes["total_kwh"], 1.3)
        self.assertEqual(attributes["est_cost_gbp"], 0.13)
        self.assertEqual(attributes["baseline_rate_p"], 15.0)
        self.assertEqual(attributes["est_saving_gbp"], 0.07)
        self.assertEqual(attributes["soc_now"], 42.3)
        self.assertEqual(attributes["horizon_end"], "2026-11-17T01:30:00+00:00")
        request.assert_called_once_with(
            "POST", f"{solis.HASS_URL}/api/states/{solis.PROPOSAL_ENTITY}", {
                "state": code,
                "attributes": attributes,
            })


class ApprovedPlanTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(patch.object(solis, "INVERTER_UTC_OFFSET", solis.timedelta()))
        self.enterContext(patch.object(solis, "MAX_RATE", 0.15))
        self.enterContext(patch.object(solis, "APPROVED_MAX_RATE", 0.40))
        self.now = solis.datetime(2026, 11, 17, tzinfo=solis.timezone.utc)

    def approved_state(self, start="2026-11-17T01:00:00+00:00",
                       end="2026-11-17T02:00:00+00:00"):
        return {"state": solis.format_plan_code([{"start": start, "end": end}])}

    def test_approved_window_bypasses_normal_max_rate(self):
        rates = [slot("2026-11-17T01:00:00+00:00",
                      "2026-11-17T02:00:00+00:00", 0.30)]
        with patch.object(solis, "hass_get_optional", return_value=self.approved_state()):
            approved = solis.get_approved_windows(rates=rates, now=self.now)

        self.assertEqual(len(approved), 1)
        self.assertTrue(solis.safety_check(approved, rates))
        safe, dropped = solis.filter_safe_windows(approved, rates)
        self.assertEqual(safe, approved)
        self.assertEqual(dropped, [])
        safe, dropped = solis.verify_written_slots_are_cheap(approved, rates)
        self.assertEqual(safe, approved)
        self.assertEqual(dropped, [])

    def test_window_ending_past_cutoff_stays_pending(self):
        state = self.approved_state(
            "2026-11-17T23:00:00+00:00", "2026-11-17T23:50:00+00:00")
        with patch.object(solis, "hass_get_optional", return_value=state), patch.object(
                solis, "hass_service") as service:
            approved = solis.get_approved_windows(rates=[], now=self.now)

        self.assertEqual(approved, [])
        service.assert_not_called()
        self.assertIn("Approved window pending (ends past cutoff)", self.output.getvalue())

    def test_finished_plan_is_cleared(self):
        state = self.approved_state(
            "2026-11-16T01:00:00+00:00", "2026-11-16T02:00:00+00:00")
        with patch.object(solis, "hass_get_optional", return_value=state), patch.object(
                solis, "hass_service") as service:
            approved = solis.get_approved_windows(rates=[], now=self.now)

        self.assertEqual(approved, [])
        service.assert_called_once_with("input_text", "set_value", {
            "entity_id": solis.APPROVED_PLAN_ENTITY,
            "value": "",
        })
        self.assertIn("Approved plan finished, clearing", self.output.getvalue())

    def test_rate_above_approved_cap_is_refused(self):
        rates = [slot("2026-11-17T01:00:00+00:00",
                      "2026-11-17T01:30:00+00:00", 0.41)]
        with patch.object(solis, "hass_get_optional", return_value=self.approved_state()):
            approved = solis.get_approved_windows(rates=rates, now=self.now)

        self.assertEqual(approved, [])
        self.assertIn("REFUSING approved window", self.output.getvalue())

    def test_unknown_rates_do_not_block_an_approved_window(self):
        with patch.object(solis, "hass_get_optional", return_value=self.approved_state()):
            approved = solis.get_approved_windows(rates=[], now=self.now)

        self.assertEqual(len(approved), 1)

    def test_charge_window_priority_is_free_then_approved_then_cheap(self):
        cheap = [
            {"start": "2026-11-17T01:00:00+00:00",
             "end": "2026-11-17T01:30:00+00:00", "tag": "agile 10.0p"},
            {"start": "2026-11-17T02:00:00+00:00",
             "end": "2026-11-17T02:30:00+00:00", "tag": "agile 11.0p"},
        ]
        approved = [{"start": "2026-11-17T03:00:00+00:00",
                     "end": "2026-11-17T03:30:00+00:00", "tag": "approved"}]
        free = [{"start": "2026-11-17T04:00:00+00:00",
                 "end": "2026-11-17T04:30:00+00:00", "tag": "free"}]

        combined = solis.combine_charge_windows(free, approved, cheap)

        self.assertEqual(
            [window["tag"] for window in combined],
            ["agile 10.0p", "approved", "free"],
        )

    def test_proposal_failure_does_not_block_approved_schedule_or_verification(self):
        start = self.now + solis.timedelta(hours=1)
        end = start + solis.timedelta(minutes=30)
        upcoming = [slot(start.isoformat(), end.isoformat(), 0.30)]
        approved = [{"start": start.isoformat(), "end": end.isoformat(),
                     "tag": "approved"}]
        with patch.object(solis, "verify_inverter_clock"), patch.object(
                solis, "ensure_storage_mode"), patch.object(
                solis, "get_free_electricity_sessions", return_value=[]), patch.object(
                solis, "get_saving_sessions", return_value=[]), patch.object(
                solis, "get_planning_slots", return_value=upcoming), patch.object(
                solis, "publish_charge_proposal", side_effect=RuntimeError("HA write failed")), patch.object(
                solis, "get_approved_windows", return_value=approved), patch.object(
                solis, "apply_schedule", return_value=0) as apply, patch.object(
                solis, "verify_charging") as verify:
            solis.run_optimizer()

        programmed = apply.call_args.args[0]
        verified = verify.call_args.args[0]
        self.assertEqual(programmed, approved)
        self.assertEqual(verified, approved)
        self.assertIn("Charge proposal failed (RuntimeError)", self.output.getvalue())


class Inverter:
    def __init__(self):
        self.values = {}
        self.writes = []
        for slots in solis.SLOT_CIDS.values():
            for cids in slots:
                self.values.update({cids["switch"]: "1",
                                    cids["time"]: "01:00-06:00",
                                    cids["current"]: "50"})

    def login(self):
        pass

    def read(self, cid):
        return self.values[cid], "63"

    def write(self, cid, value, **kwargs):
        self.writes.append((cid, value))
        self.values[cid] = value
        return "63"


class ChargeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.inverter = Inverter()
        self.enterContext(patch.object(solis, "SolisCloud", return_value=self.inverter))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def assert_disabled(self):
        for slots in solis.SLOT_CIDS.values():
            for cids in slots:
                self.assertEqual(self.inverter.values[cids["switch"]], "0")

    def test_forced_clear_does_not_require_home_assistant(self):
        with patch.object(solis, "hass_get", side_effect=urllib.error.URLError("HA down")):
            solis.clear_schedule()
        self.assert_disabled()
        self.assertTrue(self.inverter.writes)

    def test_ha_outage_before_price_validation_clears_old_slots(self):
        with patch.object(solis, "verify_inverter_clock"), patch.object(
                solis, "hass_get", side_effect=urllib.error.URLError("HA down")):
            with self.assertRaises(SystemExit) as result:
                solis.main()
        self.assertEqual(result.exception.code, 1)
        self.assert_disabled()

    def test_unavailable_mode_clears_old_slots(self):
        with patch.object(solis, "verify_inverter_clock"), patch.object(
                solis, "hass_get", return_value={"state": "unavailable"}):
            with self.assertRaises(SystemExit) as result:
                solis.main()
        self.assertEqual(result.exception.code, 4)
        self.assert_disabled()

    def test_unreadable_clock_still_attempts_direct_clear(self):
        read = self.inverter.read

        def fail_clock_only(cid):
            if cid == solis.INVERTER_CLOCK_CID:
                raise solis.SolisError("clock unavailable")
            return read(cid)

        with patch.object(self.inverter, "read", side_effect=fail_clock_only):
            with self.assertRaises(SystemExit) as result:
                solis.main()
        self.assertEqual(result.exception.code, 2)
        self.assert_disabled()

    def test_programming_failure_gets_one_best_effort_clear(self):
        write = self.inverter.write
        failed = False

        def fail_first_write(cid, value, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise solis.SolisError("transient write failure")
            return write(cid, value, **kwargs)

        with patch.object(solis, "run_optimizer", side_effect=solis.clear_schedule), patch.object(
                self.inverter, "write", side_effect=fail_first_write):
            with self.assertRaises(SystemExit) as result:
                solis.main()
        self.assertEqual(result.exception.code, 5)
        self.assert_disabled()

    def test_failed_clear_never_reports_success(self):
        with patch.object(solis, "run_optimizer", side_effect=ValueError("bad data")), patch.object(
                self.inverter, "login", side_effect=solis.SolisError("offline")):
            with self.assertRaises(SystemExit) as result:
                solis.main()
        self.assertNotEqual(result.exception.code, 0)
        self.assertEqual(self.inverter.writes, [])

    def test_successful_run_does_not_clear_the_valid_schedule(self):
        with patch.object(solis, "run_optimizer"), patch.object(solis, "clear_schedule") as clear:
            solis.main()
        clear.assert_not_called()

    def test_explicit_success_does_not_clear(self):
        with patch.object(solis, "run_optimizer", side_effect=SystemExit(0)), patch.object(
                solis, "clear_schedule") as clear:
            with self.assertRaises(SystemExit) as result:
                solis.main()
        self.assertEqual(result.exception.code, 0)
        clear.assert_not_called()


if __name__ == "__main__":
    unittest.main()
