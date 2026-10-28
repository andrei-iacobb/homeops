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
