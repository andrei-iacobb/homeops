"""Run with HA 2026.9.3 dependencies; never starts Home Assistant itself.

python -m unittest discover -s apps/homeassistant/alexa/rtc_bridge/tests -v
"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components"))

from homeassistant.components import camera
from homeassistant.components.alexa import capabilities, entities, handlers
from homeassistant.components.alexa.errors import AlexaAlreadyInOperationError, AlexaInvalidDirectiveError
from homeassistant.core import Context, State
from homeassistant.exceptions import HomeAssistantError
from webrtc_models import RTCIceCandidateInit

import alexa_rtc_bridge as bridge
from alexa_rtc_bridge.sdp import embed_candidates, is_ipv4_candidate
from alexa_rtc_bridge.session import EndpointUnreachable, NAMESPACE, SessionManager

CAMERA = "camera.front_door_live_view"
SECOND = "camera.garden_live_view"
OFFER = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 99\r\na=mid:video\r\n"
IPV4 = "candidate:1 1 UDP 1 192.0.2.10 5000 typ host"
IPV6 = "candidate:2 1 UDP 1 2001:db8::1 5000 typ host"


def directive(entity_id=CAMERA, key=None, offer=OFFER):
    return SimpleNamespace(
        entity_id=entity_id,
        payload={"sessionId": key or str(uuid4()), "offer": {"format": "SDP", "value": offer}},
        response=lambda **kwargs: kwargs,
    )


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        user = SimpleNamespace(is_active=True, permissions=MagicMock())
        user.permissions.check_entity.return_value = True
        self.hass = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            auth=SimpleNamespace(async_get_user=AsyncMock(return_value=user)),
            async_create_task=asyncio.create_task,
            data={}, bus=MagicMock(),
        )
        self.camera = SimpleNamespace(
            camera_capabilities=SimpleNamespace(frontend_stream_types={camera.StreamType.WEB_RTC}),
            close_webrtc_session=MagicMock(),
        )
        self.manager = SessionManager(self.hass, frozenset({CAMERA, SECOND}))
        self.context = Context(user_id="owner")
        self.lookup = patch.object(camera, "get_camera_from_entity_id", return_value=self.camera)
        self.lookup.start()
        self.addCleanup(self.lookup.stop)
        self.addCleanup(self.manager.stop)

    async def answer(self, offer, key, receive):
        receive(camera.WebRTCAnswer(OFFER))
        receive(camera.WebRTCCandidate(RTCIceCandidateInit(IPV4, sdp_mid="video")))
        receive(camera.WebRTCCandidate(RTCIceCandidateInit("")))

    async def start(self, request=None):
        self.camera.async_handle_async_webrtc_offer = self.answer
        request = request or directive()
        result = await self.manager.initiate(self.hass, None, request, self.context)
        return request, result

    async def test_complete_answer_and_duplicate_offer_do_not_start_two_sessions(self):
        self.camera.async_handle_async_webrtc_offer = AsyncMock(side_effect=self.answer)
        request = directive()
        one = await self.manager.initiate(self.hass, None, request, self.context)
        two = await self.manager.initiate(self.hass, None, request, self.context)
        self.assertEqual(one, two)
        self.assertIn("a=" + IPV4, one["payload"]["answer"]["value"])
        self.camera.async_handle_async_webrtc_offer.assert_awaited_once()

    async def test_other_user_and_camera_cannot_disconnect_session(self):
        request, _ = await self.start()
        key = request.payload["sessionId"]
        for other, context in [(directive(SECOND, key), self.context), (request, Context(user_id="other"))]:
            with self.assertRaises(AlexaInvalidDirectiveError):
                await self.manager.disconnected(self.hass, None, other, context)
        self.assertIn(key, self.manager.sessions)
        self.camera.close_webrtc_session.assert_not_called()

    async def test_owner_disconnect_closes_retained_camera_after_entity_removal(self):
        request, _ = await self.start()
        key = request.payload["sessionId"]
        self.camera.camera_capabilities.frontend_stream_types.clear()
        self.hass.auth.async_get_user.return_value.permissions.check_entity.return_value = False
        with patch.object(camera, "get_camera_from_entity_id", side_effect=HomeAssistantError("removed")) as lookup:
            response = await self.manager.disconnected(self.hass, None, request, self.context)
            # A duplicate disconnect is harmless and does not close twice.
            await self.manager.disconnected(self.hass, None, request, self.context)
            lookup.assert_not_called()
        self.assertEqual(response["name"], "SessionDisconnected")
        self.assertEqual(response["namespace"], NAMESPACE)
        self.assertEqual(response["payload"], {"sessionId": key})
        self.assertFalse(self.manager.sessions)
        self.camera.close_webrtc_session.assert_called_once_with(key)

    async def test_connected_ack_requires_existing_session_endpoint_and_user(self):
        request, _ = await self.start()
        key = request.payload["sessionId"]
        for other, context in [(directive(SECOND, key), self.context), (request, Context(user_id="other"))]:
            with self.assertRaises(AlexaInvalidDirectiveError):
                await self.manager.connected(self.hass, None, other, context)
        with self.assertRaises(EndpointUnreachable):
            await self.manager.connected(self.hass, None, directive(), self.context)
        response = await self.manager.connected(self.hass, None, request, self.context)
        self.assertEqual(response["name"], "SessionConnected")
        self.assertEqual(response["namespace"], NAMESPACE)
        self.assertEqual(response["payload"], {"sessionId": key})
        self.assertIn(key, self.manager.sessions)
        self.camera.close_webrtc_session.assert_not_called()

    async def test_read_permission_denied_never_starts_camera(self):
        self.hass.auth.async_get_user.return_value.permissions.check_entity.return_value = False
        self.camera.async_handle_async_webrtc_offer = AsyncMock()
        with self.assertRaises(AlexaInvalidDirectiveError):
            await self.manager.initiate(self.hass, None, directive(), self.context)
        self.camera.async_handle_async_webrtc_offer.assert_not_called()

    async def test_unlisted_camera_and_missing_identity_are_denied(self):
        for request, context in [(directive("camera.other"), self.context), (directive(), Context())]:
            with self.assertRaises(AlexaInvalidDirectiveError):
                await self.manager.initiate(self.hass, None, request, context)
        self.assertFalse(self.manager.sessions)

    async def test_non_webrtc_camera_and_malformed_session_do_not_allocate(self):
        self.camera.camera_capabilities.frontend_stream_types = {camera.StreamType.HLS}
        with self.assertRaises(AlexaInvalidDirectiveError):
            await self.manager.initiate(self.hass, None, directive(), self.context)
        self.camera.camera_capabilities.frontend_stream_types = {camera.StreamType.WEB_RTC}
        with self.assertRaises(AlexaInvalidDirectiveError):
            await self.manager.initiate(self.hass, None, directive(key="not-a-session"), self.context)
        self.assertFalse(self.manager.sessions)

    async def test_no_ipv4_is_unreachable_and_closes_ring_session(self):
        async def ipv6(offer, key, receive):
            receive(camera.WebRTCAnswer(OFFER + "a=" + IPV6 + "\r\n"))
            receive(camera.WebRTCCandidate(RTCIceCandidateInit("")))
        self.camera.async_handle_async_webrtc_offer = ipv6
        request = directive()
        with self.assertRaises(EndpointUnreachable):
            await self.manager.initiate(self.hass, None, request, self.context)
        self.assertFalse(self.manager.sessions)
        self.camera.close_webrtc_session.assert_called_once_with(request.payload["sessionId"])

    async def test_deadline_cancels_provider_and_closes_session(self):
        cancelled = asyncio.Event()
        async def hung(offer, key, receive):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.camera.async_handle_async_webrtc_offer = hung
        with patch("alexa_rtc_bridge.session.ANSWER_TIMEOUT", 0.02):
            with self.assertRaises(EndpointUnreachable):
                await self.manager.initiate(self.hass, None, directive(), self.context)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.manager.sessions)
        self.camera.close_webrtc_session.assert_called_once()

    async def test_disconnect_during_negotiation_cannot_leave_late_session(self):
        started = asyncio.Event()
        async def pending(offer, key, receive):
            started.set()
            await asyncio.Event().wait()
        self.camera.async_handle_async_webrtc_offer = pending
        request = directive()
        task = asyncio.create_task(self.manager.initiate(self.hass, None, request, self.context))
        await started.wait()
        await self.manager.disconnected(self.hass, None, request, self.context)
        with self.assertRaises(EndpointUnreachable):
            await task
        self.assertFalse(self.manager.sessions)
        self.camera.close_webrtc_session.assert_called_once()

    async def test_disconnect_after_offer_returns_cancels_pending_answer_promptly(self):
        returned = asyncio.Event()
        async def no_answer(offer, key, receive):
            returned.set()
        self.camera.async_handle_async_webrtc_offer = no_answer
        request = directive()
        task = asyncio.create_task(self.manager.initiate(self.hass, None, request, self.context))
        await returned.wait()
        await asyncio.sleep(0)
        session = self.manager.sessions[request.payload["sessionId"]]
        self.assertTrue(session.task.done())
        self.assertFalse(session.answer_future.done())
        await self.manager.disconnected(self.hass, None, request, self.context)
        with self.assertRaises(EndpointUnreachable):
            await asyncio.wait_for(task, timeout=0.1)
        self.assertTrue(session.answer_future.cancelled())
        self.assertFalse(self.manager.sessions)
        self.camera.close_webrtc_session.assert_called_once_with(request.payload["sessionId"])

    async def test_request_cancellation_closes_session(self):
        started = asyncio.Event()
        async def pending(offer, key, receive):
            started.set()
            await asyncio.Event().wait()
        self.camera.async_handle_async_webrtc_offer = pending
        task = asyncio.create_task(self.manager.initiate(self.hass, None, directive(), self.context))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.manager.sessions)
        self.camera.close_webrtc_session.assert_called_once()

    async def test_expiry_and_shutdown_close_active_sessions(self):
        with patch("alexa_rtc_bridge.session.SESSION_DURATION", 0.01):
            await self.start()
            await asyncio.sleep(0.025)
        self.assertFalse(self.manager.sessions)
        await self.start()
        self.manager.stop()
        self.assertFalse(self.manager.sessions)
        self.assertEqual(self.camera.close_webrtc_session.call_count, 2)

    async def test_capacity_includes_pending_negotiations(self):
        await self.start()
        with patch("alexa_rtc_bridge.session.MAX_SESSIONS", 1):
            with self.assertRaises(AlexaAlreadyInOperationError):
                await self.start()
        self.assertEqual(len(self.manager.sessions), 1)

    async def test_capability_adapter_scopes_webrtc_and_restores_core_hooks(self):
        saved_handlers = handlers.HANDLERS.copy()
        saved_adapter = entities.ENTITY_ADAPTERS[camera.DOMAIN]
        try:
            for key in bridge.RTC_KEYS:
                handlers.HANDLERS.pop(key, None)
            with patch.object(bridge, "__version__", "2026.9.3"):
                self.assertTrue(await bridge.async_setup(self.hass, {bridge.DOMAIN: {"include_entities": [CAMERA]}}))
            adapter = entities.ENTITY_ADAPTERS[camera.DOMAIN]
            config = SimpleNamespace(entity_config={}, locale="en-GB")
            with patch.object(saved_adapter, "interfaces", return_value=[capabilities.AlexaCameraStreamController(State(CAMERA, "idle"))]):
                exposed = list(adapter(self.hass, config, State(CAMERA, "idle")).interfaces())
                other = list(adapter(self.hass, config, State(SECOND, "idle")).interfaces())
            self.assertEqual([i.name() for i in exposed], [NAMESPACE])
            self.assertEqual([i.name() for i in other], ["Alexa.CameraStreamController"])
            self.assertIn("en-GB", exposed[0].supported_locales)
            stop = self.hass.bus.async_listen_once.call_args.args[1]
            stop(None)
            self.assertIs(entities.ENTITY_ADAPTERS[camera.DOMAIN], saved_adapter)
        finally:
            entities.ENTITY_ADAPTERS[camera.DOMAIN] = saved_adapter
            handlers.HANDLERS.clear()
            handlers.HANDLERS.update(saved_handlers)

    async def test_unsupported_version_refuses_to_change_registries(self):
        saved_handlers = handlers.HANDLERS.copy()
        saved_adapter = entities.ENTITY_ADAPTERS[camera.DOMAIN]
        with patch.object(bridge, "__version__", "2026.9.4"):
            self.assertFalse(await bridge.async_setup(self.hass, {bridge.DOMAIN: {"include_entities": [CAMERA]}}))
        self.assertEqual(handlers.HANDLERS, saved_handlers)
        self.assertIs(entities.ENTITY_ADAPTERS[camera.DOMAIN], saved_adapter)

    async def test_registered_state_watcher_releases_removed_and_unavailable_cameras(self):
        saved_handlers = handlers.HANDLERS.copy()
        saved_adapter = entities.ENTITY_ADAPTERS[camera.DOMAIN]
        stop = None
        try:
            for key in bridge.RTC_KEYS:
                handlers.HANDLERS.pop(key, None)
            with patch.object(bridge, "__version__", "2026.9.3"):
                self.assertTrue(await bridge.async_setup(self.hass, {bridge.DOMAIN: {"include_entities": [CAMERA]}}))
            manager = self.hass.data[bridge.DOMAIN]
            stop = self.hass.bus.async_listen_once.call_args.args[1]
            event_type, watcher, event_filter = self.hass.bus.async_listen.call_args.args
            self.assertEqual(event_type, bridge.EVENT_STATE_CHANGED)
            self.camera.async_handle_async_webrtc_offer = self.answer
            for new_state in (None, State(CAMERA, "unavailable")):
                request = directive()
                await manager.initiate(self.hass, None, request, self.context)
                unrelated = {"entity_id": SECOND, "new_state": None}
                self.assertFalse(event_filter(unrelated))
                watcher(SimpleNamespace(data=unrelated))
                self.assertIn(request.payload["sessionId"], manager.sessions)
                available = {"entity_id": CAMERA, "new_state": State(CAMERA, "idle")}
                self.assertFalse(event_filter(available))
                watcher(SimpleNamespace(data=available))
                self.assertIn(request.payload["sessionId"], manager.sessions)
                data = {"entity_id": CAMERA, "new_state": new_state}
                self.assertTrue(event_filter(data))
                watcher(SimpleNamespace(data=data))
                watcher(SimpleNamespace(data=data))
                self.assertFalse(manager.sessions)
            self.assertEqual(self.camera.close_webrtc_session.call_count, 2)
            stop(None)
            stop = None
            self.hass.bus.async_listen.return_value.assert_called_once_with()
        finally:
            if stop:
                stop(None)
            entities.ENTITY_ADAPTERS[camera.DOMAIN] = saved_adapter
            handlers.HANDLERS.clear()
            handlers.HANDLERS.update(saved_handlers)

    def test_candidates_route_by_mid_and_ipv6_never_leaks(self):
        answer = OFFER + "a=" + IPV6 + "\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:audio\r\n"
        result = embed_candidates(answer, [RTCIceCandidateInit(IPV4, sdp_mid="audio")])
        self.assertNotIn(IPV6, result)
        self.assertGreater(result.index("a=" + IPV4), result.index("a=mid:audio"))
        self.assertNotIn("a=" + IPV4, result.split("m=audio")[0])

    def test_non_candidate_attributes_and_double_prefix_are_rejected(self):
        invalid = [
            "a=" + IPV4,
            "a=candidate:" + IPV4,
            "other:1 1 UDP 1 192.0.2.10 5000 typ host",
            IPV4 + "\r\na=sendrecv",
        ]
        self.assertTrue(is_ipv4_candidate(IPV4))
        for value in invalid:
            self.assertFalse(is_ipv4_candidate(value), value)
        result = embed_candidates(OFFER, [RTCIceCandidateInit(value, sdp_mid="video") for value in invalid])
        self.assertNotIn("a=candidate:", result)


if __name__ == "__main__":
    unittest.main()
