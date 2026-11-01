"""Bound native Alexa sessions to authenticated Home Assistant camera access.

SDP negotiation is adapted from Home Assistant Core (Apache-2.0),
5588f94efe6bde02a4b7cecffcab23fb085c8d55, alexa/handlers.py.
Changes add endpoint/user ownership, resource bounds, and shutdown cleanup.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import logging
from typing import Any
from uuid import UUID

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import camera
from homeassistant.components.alexa.errors import (
    AlexaAlreadyInOperationError,
    AlexaError,
    AlexaInvalidDirectiveError,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from webrtc_models import RTCIceCandidateInit

from .sdp import embed_candidates

NAMESPACE = "Alexa.RTCSessionController"
ANSWER_TIMEOUT = 5.0
CANDIDATE_TIMEOUT = 1.5
SESSION_DURATION = 600
MAX_SESSIONS = 14
MAX_SDP_BYTES = 65536
_LOGGER = logging.getLogger(__name__)


class EndpointUnreachable(AlexaError):
    """Endpoint error not present in HA 2026.9.3's Alexa exception module."""

    error_type = "ENDPOINT_UNREACHABLE"


@dataclass
class Session:
    """A session is reserved before any camera negotiation can yield."""

    entity_id: str
    user_id: str
    camera: camera.Camera
    offer_hash: str
    answer: str | None = None
    answer_future: asyncio.Future[str] | None = None
    task: asyncio.Task[None] | None = None
    expiry: asyncio.TimerHandle | None = None
    closed: bool = False


def session_id(payload: dict[str, Any]) -> str:
    """Reject malformed IDs before allocating camera resources."""
    value = payload.get("sessionId")
    if not isinstance(value, str) or len(value) != 36:
        raise AlexaInvalidDirectiveError("Invalid RTC session identifier")
    try:
        parsed = UUID(value)
    except ValueError as err:
        raise AlexaInvalidDirectiveError("Invalid RTC session identifier") from err
    if parsed.version != 4 or str(parsed) != value.lower():
        raise AlexaInvalidDirectiveError("Invalid RTC session identifier")
    return value.lower()


class SessionManager:
    """Reuse camera signaling in HA; media never passes through this bridge."""

    def __init__(self, hass: HomeAssistant, entity_ids: frozenset[str]) -> None:
        self.hass = hass
        self.entity_ids = entity_ids
        self.sessions: dict[str, Session] = {}
        self.stopping = False

    async def _identity(self, directive: Any, context: Context) -> Any:
        """Authenticate the caller without depending on a current camera entity."""
        entity_id = directive.entity_id
        if self.stopping or entity_id not in self.entity_ids or not context.user_id:
            raise AlexaInvalidDirectiveError("RTC camera access denied")
        user = await self.hass.auth.async_get_user(context.user_id)
        if not user or not user.is_active:
            raise AlexaInvalidDirectiveError("RTC camera access denied")
        return user

    async def _camera(self, directive: Any, context: Context) -> camera.Camera:
        user = await self._identity(directive, context)
        entity_id = directive.entity_id
        if not user.permissions.check_entity(entity_id, POLICY_READ):
            raise AlexaInvalidDirectiveError("RTC camera access denied")
        try:
            entity = camera.get_camera_from_entity_id(self.hass, entity_id)
        except HomeAssistantError as err:
            raise EndpointUnreachable("RTC camera unavailable") from err
        if camera.StreamType.WEB_RTC not in entity.camera_capabilities.frontend_stream_types:
            raise AlexaInvalidDirectiveError("Camera does not support native WebRTC")
        return entity

    def close(self, key: str, session: Session) -> None:
        """Close exactly this session, including a pending Ring signaling task."""
        if session.closed:
            return
        session.closed = True
        if self.sessions.get(key) is session:
            self.sessions.pop(key)
        if session.expiry:
            session.expiry.cancel()
        if session.task and not session.task.done():
            session.task.cancel()
        if session.answer_future and not session.answer_future.done():
            session.answer_future.cancel()
        try:
            session.camera.close_webrtc_session(key)
        except Exception as err:
            # Continue cleaning other sessions without logging Ring payloads or SDP.
            _LOGGER.error("RTC camera cleanup failed (%s)", type(err).__name__)

    def stop(self, _event: Any = None) -> None:
        """Do not leave Ring's unbounded keepalive running after HA stops."""
        self.stopping = True
        for key, session in list(self.sessions.items()):
            self.close(key, session)

    async def initiate(self, hass: HomeAssistant, config: Any, directive: Any, context: Context) -> Any:
        """Handle an Alexa offer within the Lambda's existing request window."""
        entity = await self._camera(directive, context)
        key = session_id(directive.payload)
        offer = directive.payload.get("offer")
        if (
            not isinstance(offer, dict) or offer.get("format") != "SDP"
            or not isinstance(value := offer.get("value"), str)
            or not value.startswith("v=0") or "\nm=" not in value
            or len(value.encode()) > MAX_SDP_BYTES
        ):
            raise AlexaInvalidDirectiveError("Invalid RTC SDP offer")
        digest = hashlib.sha256(value.encode()).hexdigest()
        if existing := self.sessions.get(key):
            if (existing.entity_id, existing.user_id, existing.offer_hash) != (
                directive.entity_id, context.user_id, digest
            ):
                raise AlexaInvalidDirectiveError("RTC session ownership mismatch")
            if existing.answer is None:
                raise AlexaAlreadyInOperationError("RTC session negotiation in progress")
            return directive.response(
                name="AnswerGeneratedForSession", namespace=NAMESPACE,
                payload={"answer": {"format": "SDP", "value": existing.answer}},
            )
        if self.stopping or len(self.sessions) >= MAX_SESSIONS:
            raise AlexaAlreadyInOperationError("RTC session capacity reached")
        session = Session(directive.entity_id, context.user_id, entity, digest)
        self.sessions[key] = session
        try:
            session.answer = await self._answer(key, session, value)
            if session.closed:
                raise EndpointUnreachable("RTC session disconnected during negotiation")
            session.expiry = self.hass.loop.call_later(SESSION_DURATION, self.close, key, session)
        except BaseException:
            self.close(key, session)
            raise
        return directive.response(
            name="AnswerGeneratedForSession", namespace=NAMESPACE,
            payload={"answer": {"format": "SDP", "value": session.answer}},
        )

    async def _answer(self, key: str, session: Session, offer: str) -> str:
        answer_future: asyncio.Future[str] = self.hass.loop.create_future()
        session.answer_future = answer_future
        candidates: list[RTCIceCandidateInit] = []
        gathering_done = asyncio.Event()
        camera_error = False
        accepting = True

        def receive(message: camera.WebRTCMessage) -> None:
            nonlocal camera_error
            if not accepting or session.closed:
                return
            if isinstance(message, camera.WebRTCAnswer):
                if not answer_future.done():
                    answer_future.set_result(message.answer)
            elif isinstance(message, camera.WebRTCCandidate):
                candidate = message.candidate
                if not candidate.candidate:
                    gathering_done.set()
                elif (
                    len(candidates) < 64
                    and len(candidate.candidate) <= 2048
                    and "\n" not in candidate.candidate
                    and "\r" not in candidate.candidate
                ):
                    if isinstance(candidate, RTCIceCandidateInit):
                        candidates.append(candidate)
                    else:
                        candidates.append(RTCIceCandidateInit(candidate.candidate))
            elif isinstance(message, camera.WebRTCError):
                camera_error = True
                gathering_done.set()
                if not answer_future.done():
                    answer_future.set_result("")

        deadline = self.hass.loop.time() + ANSWER_TIMEOUT
        try:
            async with asyncio.timeout_at(deadline):
                session.task = self.hass.async_create_task(
                    session.camera.async_handle_async_webrtc_offer(offer, key, receive)
                )
                await session.task
                answer = await answer_future
                try:
                    async with asyncio.timeout_at(min(deadline, self.hass.loop.time() + CANDIDATE_TIMEOUT)):
                        await gathering_done.wait()
                except TimeoutError:
                    pass
            if camera_error or session.closed:
                raise EndpointUnreachable("RTC camera negotiation failed")
            if (
                not answer.startswith("v=0")
                or len(answer.encode()) > MAX_SDP_BYTES
                or "\nm=" not in answer
            ):
                raise EndpointUnreachable("RTC camera returned an invalid answer")
            answer = embed_candidates(answer, candidates)
            if len(answer.encode()) > MAX_SDP_BYTES:
                raise EndpointUnreachable("RTC camera answer exceeded size limit")
            if not any(line.startswith("a=candidate:") for line in answer.split("\r\n")):
                raise EndpointUnreachable("RTC camera returned no IPv4 candidates")
            return answer
        except asyncio.CancelledError:
            if session.closed:
                raise EndpointUnreachable("RTC session closed during negotiation") from None
            raise
        except (TimeoutError, HomeAssistantError) as err:
            raise EndpointUnreachable("RTC camera did not answer within deadline") from err
        except Exception as err:
            if isinstance(err, AlexaError):
                raise
            raise EndpointUnreachable("RTC camera negotiation failed") from err
        finally:
            accepting = False
            if not answer_future.done():
                answer_future.cancel()

    async def connected(self, hass: HomeAssistant, config: Any, directive: Any, context: Context) -> Any:
        await self._camera(directive, context)
        key = session_id(directive.payload)
        session = self._owned(key, directive.entity_id, context.user_id)
        if not session or session.answer is None:
            raise EndpointUnreachable("RTC session not established")
        return directive.response(name="SessionConnected", namespace=NAMESPACE, payload={"sessionId": key})

    def _owned(self, key: str, entity_id: str, user_id: str | None) -> Session | None:
        session = self.sessions.get(key)
        if session and (session.entity_id, session.user_id) != (entity_id, user_id):
            raise AlexaInvalidDirectiveError("RTC session ownership mismatch")
        return session

    async def disconnected(self, hass: HomeAssistant, config: Any, directive: Any, context: Context) -> Any:
        # The authenticated owner can release a retained session even if the
        # camera was removed, lost WebRTC support or access was revoked since
        # negotiation. No new media access is granted by closing a session.
        await self._identity(directive, context)
        key = session_id(directive.payload)
        if session := self._owned(key, directive.entity_id, context.user_id):
            self.close(key, session)
        return directive.response(name="SessionDisconnected", namespace=NAMESPACE, payload={"sessionId": key})
