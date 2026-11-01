"""Scoped Alexa WebRTC backport for the installed HA 2026.9.3 instance.

Capability adapted from Home Assistant Core (Apache-2.0), commit
5588f94efe6bde02a4b7cecffcab23fb085c8d55, alexa/capabilities.py.
Only the explicit camera allowlist changes discovery. Authentication, exposure
filters and response formatting remain owned by the core Alexa integration.
"""

from collections.abc import Iterable
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import camera
from homeassistant.components.alexa import capabilities, entities, handlers
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    __version__,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .session import NAMESPACE, SessionManager

DOMAIN = "alexa_rtc_bridge"
SUPPORTED_VERSION = "2026.9.3"
_LOGGER = logging.getLogger(__name__)
RTC_KEYS = tuple((NAMESPACE, name) for name in (
    "InitiateSessionWithOffer", "SessionConnected", "SessionDisconnected"
))


def _camera_ids(value: Any) -> list[str]:
    ids = cv.entity_ids(value)
    if not 1 <= len(ids) <= 7 or len(set(ids)) != len(ids):
        raise vol.Invalid("Specify one to seven distinct camera entities")
    if any(not entity_id.startswith("camera.") for entity_id in ids):
        raise vol.Invalid("Only camera entities are supported")
    return ids


CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({vol.Required("include_entities"): _camera_ids}),
}, extra=vol.ALLOW_EXTRA)


class AlexaRTCSessionController(capabilities.AlexaCapability):
    """Advertise real camera WebRTC support, with half-duplex audio only."""

    supported_locales = {
        "ar-SA", "de-DE", "en-AU", "en-CA", "en-GB", "en-US", "es-ES",
        "es-US", "fr-CA", "fr-FR", "hi-IN", "it-IT", "ja-JP", "nl-NL", "pt-BR",
    }

    def name(self) -> str:
        return NAMESPACE

    def configuration(self) -> dict[str, bool]:
        return {"isFullDuplexAudioSupported": False}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register guarded backport hooks after the core Alexa dependency loads."""
    if DOMAIN in hass.data:
        return True
    if hasattr(capabilities, "AlexaRTCSessionController") and all(
        key in handlers.HANDLERS for key in RTC_KEYS
    ):
        _LOGGER.info("Core Alexa already supports WebRTC; backport not installed")
        return True
    if __version__ != SUPPORTED_VERSION:
        _LOGGER.error("Alexa RTC backport supports HA %s only; found %s", SUPPORTED_VERSION, __version__)
        return False
    if any(key in handlers.HANDLERS for key in RTC_KEYS):
        _LOGGER.error("Another RTC handler is present; refusing partial override")
        return False
    original_adapter = entities.ENTITY_ADAPTERS.get(camera.DOMAIN)
    if original_adapter is not entities.CameraCapabilities:
        _LOGGER.error("Camera Alexa adapter is already overridden; refusing replacement")
        return False
    entity_ids = frozenset(config[DOMAIN]["include_entities"])
    manager = SessionManager(hass, entity_ids)

    class ScopedCameraCapabilities(original_adapter):
        def interfaces(self) -> Iterable[capabilities.AlexaCapability]:
            if self.entity_id not in entity_ids:
                yield from super().interfaces()
                return
            try:
                entity = camera.get_camera_from_entity_id(self.hass, self.entity_id)
            except HomeAssistantError:
                # Entity removal during discovery must not advertise a dead RTC capability.
                yield from super().interfaces()
                return
            if camera.StreamType.WEB_RTC not in entity.camera_capabilities.frontend_stream_types:
                yield from super().interfaces()
                return
            yield AlexaRTCSessionController(self.entity)
            for interface in super().interfaces():
                if not isinstance(interface, capabilities.AlexaCameraStreamController):
                    yield interface

    installed = dict(zip(RTC_KEYS, (manager.initiate, manager.connected, manager.disconnected), strict=True))
    entities.ENTITY_ADAPTERS[camera.DOMAIN] = ScopedCameraCapabilities
    handlers.HANDLERS.update(installed)
    hass.data[DOMAIN] = manager

    @callback
    def camera_unavailable(data: dict[str, Any]) -> bool:
        state = data.get("new_state")
        return data.get("entity_id") in entity_ids and (
            state is None or state.state == STATE_UNAVAILABLE
        )

    @callback
    def camera_state_changed(event: Any) -> None:
        # Core Alexa rejects removed entities before dispatching disconnects.
        # Release retained sessions from state events instead of waiting for expiry.
        if camera_unavailable(event.data):
            entity_id = event.data["entity_id"]
            for key, session in list(manager.sessions.items()):
                if session.entity_id == entity_id:
                    manager.close(key, session)

    unsubscribe_state = hass.bus.async_listen(
        EVENT_STATE_CHANGED, camera_state_changed, camera_unavailable
    )

    @callback
    def stop(event: Any) -> None:
        try:
            manager.stop(event)
        finally:
            unsubscribe_state()
            if entities.ENTITY_ADAPTERS.get(camera.DOMAIN) is ScopedCameraCapabilities:
                entities.ENTITY_ADAPTERS[camera.DOMAIN] = original_adapter
            for key, handler in installed.items():
                if handlers.HANDLERS.get(key) == handler:
                    handlers.HANDLERS.pop(key)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return True
