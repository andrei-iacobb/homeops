"""Exercise Ring motion conversion using the installed Home Assistant runtime."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


class MotionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # A test instance must not bind HAOS's live Supervisor API socket.
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("SUPERVISOR", "HASSIO"))
        }
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory(prefix="alexa-motion-test-")
        self.hass = HomeAssistant(self.directory.name)
        loader.async_setup(self.hass)
        config = yaml.safe_load(Path(__file__).with_name("motion.yaml").read_text())
        self.sources = [block["triggers"][0]["entity_id"] for block in config["template"]]
        self.targets = ["binary_sensor." + block["binary_sensor"][0]["unique_id"] for block in config["template"]]
        for source in self.sources:
            self.hass.states.async_set(source, (dt_util.utcnow() - timedelta(minutes=10)).isoformat(), {"event_type": "motion"})
        for block in config["template"]:
            block["binary_sensor"][0]["auto_off"] = {"seconds": 1}
        assert await bootstrap.async_from_config_dict(config, self.hass) is not None
        await self.hass.async_start()
        await self.hass.async_block_till_done()

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.directory.cleanup()

    async def test_old_events_fresh_events_and_source_unavailability(self):
        for source, target in zip(self.sources, self.targets, strict=True):
            self.assertEqual(self.hass.states.get(target).state, "off")
            self.hass.states.async_set(source, dt_util.utcnow().isoformat(), {"event_type": "motion"})
            await self.hass.async_block_till_done()
            self.assertEqual(self.hass.states.get(target).state, "on")
        await asyncio.sleep(1.2)
        await self.hass.async_block_till_done()
        for target in self.targets:
            self.assertEqual(self.hass.states.get(target).state, "off")
        self.hass.states.async_set(self.sources[0], "unavailable")
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.targets[0]).state, "unavailable")
        self.hass.states.async_set(self.sources[0], (dt_util.utcnow() - timedelta(minutes=10)).isoformat(), {"event_type": "motion"})
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(self.targets[0]).state, "off")

    async def test_repeated_motion_extends_only_its_own_pulse(self):
        first, second = self.sources[:2]
        first_target, second_target = self.targets[:2]
        self.hass.states.async_set(first, dt_util.utcnow().isoformat(), {"event_type": "motion"})
        self.hass.states.async_set(second, dt_util.utcnow().isoformat(), {"event_type": "motion"})
        await self.hass.async_block_till_done()
        await asyncio.sleep(0.6)
        self.hass.states.async_set(first, dt_util.utcnow().isoformat(), {"event_type": "motion"})
        await self.hass.async_block_till_done()
        await asyncio.sleep(0.6)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(first_target).state, "on")
        self.assertEqual(self.hass.states.get(second_target).state, "off")
        await asyncio.sleep(0.6)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(first_target).state, "off")


if __name__ == "__main__":
    unittest.main()
