"""Alexa custom-skill endpoint: electricity briefing from Home Assistant.

Handler: ``lambda_function.lambda_handler`` at the ZIP root with ``tariffs.py``,
``presentation.py`` and ``apl.json`` as siblings. Python 3.13 standard library
only.

Security boundary
  * ALEXA_SKILL_ID env is required; every applicationId present in the event
    (context.System.application and session.application) must equal it, and at
    least one must be present. Otherwise the invocation is rejected by raising.
  * request.timestamp must be within TIMESTAMP_TOLERANCE_SECONDS of now.
  * The linked Home Assistant token is read from context.System.user.accessToken
    then session.user.accessToken. It is only ever placed in the Authorization
    header of two fixed GETs and never logged, echoed or embedded in output.
  * Destination is the hardcoded HTTPS base plus two fixed entity ids. TLS is
    verified with the system trust store, redirects are refused, bodies are
    capped at MAX_BODY_BYTES, each request has a socket timeout and the pair
    shares one wall-clock budget. No retries.

Deployment contract (see deployment-contract.json): Lambda timeout 8 s,
trigger "Alexa Skills Kit" with skill-id verification, env ALEXA_SKILL_ID.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import presentation
from tariffs import (
    NoCurrentCoverage,
    RateDataError,
    build_briefing,
    merge_rates,
    parse_state_rates,
)

LOGGER = logging.getLogger("alexa.electricity")

HA_BASE_URL = "https://hass.iacob.co.uk"
HA_HOST = "hass.iacob.co.uk"
CURRENT_DAY_ENTITY = "event.octopus_energy_electricity_23j0212061_1012934633517_current_day_rates"
NEXT_DAY_ENTITY = "event.octopus_energy_electricity_23j0212061_1012934633517_next_day_rates"

IO_TIMEOUT_SECONDS = 3.0
TOTAL_BUDGET_SECONDS = 6.0
DEADLINE_MARGIN_SECONDS = 1.5
MAX_BODY_BYTES = 512 * 1024
TIMESTAMP_TOLERANCE_SECONDS = 150
APL_INTERFACE = "Alexa.Presentation.APL"
APL_TOKEN = "electricityTariff"
SKILL_ID_ENV = "ALEXA_SKILL_ID"


class RequestRejected(Exception):
    """The event failed skill-id or freshness verification. Message carries no identifiers."""


class FeedError(Exception):
    """Transport-level failure for one feed. Message is a short safe code."""


@dataclass(frozen=True)
class FeedOutcome:
    kind: str  # ok | auth | missing | error | malformed | timeout
    state: Any = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dig(mapping: Any, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _verify_request(event: Any) -> None:
    expected = os.environ.get(SKILL_ID_ENV, "").strip()
    if not expected:
        raise RequestRejected("skill id not configured")
    candidates = [
        _dig(event, "context", "System", "application", "applicationId"),
        _dig(event, "session", "application", "applicationId"),
    ]
    present = [candidate for candidate in candidates if candidate is not None]
    if not present:
        raise RequestRejected("application id missing")
    for candidate in present:
        if not isinstance(candidate, str):
            raise RequestRejected("application id mismatch")
        try:
            matches = hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
        except (TypeError, UnicodeError):
            matches = False
        if not matches:
            raise RequestRejected("application id mismatch")

    stamp = _dig(event, "request", "timestamp")
    if not isinstance(stamp, str) or not stamp:
        raise RequestRejected("timestamp missing")
    try:
        issued = datetime.fromisoformat(stamp)
    except ValueError as exc:
        raise RequestRejected("timestamp invalid") from exc
    if issued.tzinfo is None or issued.utcoffset() is None:
        raise RequestRejected("timestamp naive")
    age = abs((_utcnow() - issued.astimezone(timezone.utc)).total_seconds())
    if age > TIMESTAMP_TOLERANCE_SECONDS:
        raise RequestRejected("timestamp stale")


def _access_token(event: Any) -> Optional[str]:
    for path in (("context", "System", "user", "accessToken"), ("session", "user", "accessToken")):
        token = _dig(event, *path)
        if isinstance(token, str) and token.strip():
            if any(ord(char) <= 32 or ord(char) >= 127 for char in token):
                return None
            return token
    return None


def _supports_apl(event: Any) -> bool:
    interfaces = _dig(event, "context", "System", "device", "supportedInterfaces")
    return isinstance(interfaces, dict) and APL_INTERFACE in interfaces


def _call_budget(context: Any) -> float:
    budget = TOTAL_BUDGET_SECONDS
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if callable(getter):
        try:
            remaining = float(getter()) / 1000.0
        except Exception:  # noqa: BLE001 - defensive against odd runtimes
            remaining = None
        if remaining is not None:
            budget = min(budget, remaining - DEADLINE_MARGIN_SECONDS)
    return max(0.0, budget)


# --------------------------------------------------------------------------
# HTTPS fetch
# --------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the bearer token never follows a Location header."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = True
_SSL_CONTEXT.verify_mode = ssl.CERT_REQUIRED


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _NoRedirectHandler, urllib.request.HTTPSHandler(context=_SSL_CONTEXT)
    )


def _state_url(entity_id: str) -> str:
    return f"{HA_BASE_URL}/api/states/{urllib.parse.quote(entity_id, safe='')}"


def _perform_get(url: str, token: str, timeout: float, opener=None) -> tuple[int, bytes]:
    """One read-only GET to the fixed Home Assistant host. Returns (status, body)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != HA_HOST:
        raise FeedError("destination refused")
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        },
    )
    opener = opener or _build_opener()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_BYTES + 1)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        headers = exc.headers or {}
        if status == 403 and (
            "text/html" in headers.get("Content-Type", "").lower()
            or headers.get("cf-mitigated", "").lower() == "challenge"
        ):
            status = 502
        body = b""
        exc.close()
    if len(body) > MAX_BODY_BYTES:
        raise FeedError("body too large")
    return status, body


def _fetch_feed(entity_id: str, token: str) -> FeedOutcome:
    try:
        status, body = _perform_get(_state_url(entity_id), token, IO_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - any transport problem is one safe category
        return FeedOutcome("error")
    if status in (401, 403):
        return FeedOutcome("auth")
    if status == 404:
        return FeedOutcome("missing")
    if status != 200:
        return FeedOutcome("error")
    try:
        state = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return FeedOutcome("malformed")
    return FeedOutcome("ok", state)


def _fetch_both(token: str, budget: float) -> tuple[FeedOutcome, FeedOutcome]:
    """Fetch both feeds concurrently inside one wall-clock budget without blocking shutdown."""
    if budget <= 0:
        return FeedOutcome("timeout"), FeedOutcome("timeout")
    deadline = time.monotonic() + budget
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ha-feed")
    futures = []
    try:
        futures = [
            executor.submit(_fetch_feed, entity, token)
            for entity in (CURRENT_DAY_ENTITY, NEXT_DAY_ENTITY)
        ]
        done, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    outcomes = []
    for future in futures:
        if future in done:
            outcomes.append(future.result())
        else:
            outcomes.append(FeedOutcome("timeout"))
    return outcomes[0], outcomes[1]


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_document() -> dict[str, Any]:
    path = Path(__file__).with_name("apl.json")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _apl_directive(datasource: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Alexa.Presentation.APL.RenderDocument",
        "token": APL_TOKEN,
        "document": _load_document(),
        "datasources": {"tariff": datasource},
    }


def _response(
    speech: str,
    *,
    end_session: bool = True,
    card: Optional[dict[str, Any]] = None,
    reprompt: Optional[str] = None,
    directives: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "outputSpeech": {"type": "PlainText", "text": speech},
        "shouldEndSession": end_session,
    }
    if card:
        body["card"] = card
    if reprompt:
        body["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    if directives:
        body["directives"] = directives
    return {"version": "1.0", "response": body}


def _message_response(
    key: str,
    apl: bool,
    *,
    end_session: bool = True,
    card: Optional[dict[str, Any]] = None,
    reprompt: Optional[str] = None,
) -> dict[str, Any]:
    message = presentation.MESSAGES[key]
    directives = None
    if apl and message.headline:
        directives = [_apl_directive(presentation.message_datasource(message))]
    return _response(
        message.speech,
        end_session=end_session,
        card=card,
        reprompt=reprompt,
        directives=directives,
    )


def _link_response(apl: bool) -> dict[str, Any]:
    return _message_response("link", apl, card={"type": "LinkAccount"})


def _briefing_response(event: Any, context: Any, apl: bool) -> dict[str, Any]:
    token = _access_token(event)
    if token is None:
        LOGGER.info("briefing outcome=no_token")
        return _link_response(apl)

    current, future = _fetch_both(token, _call_budget(context))
    LOGGER.info("briefing feeds current=%s future=%s", current.kind, future.kind)

    if "auth" in (current.kind, future.kind):
        return _link_response(apl)
    if "timeout" in (current.kind, future.kind):
        return _message_response("deadline", apl)
    if current.kind in ("error", "missing"):
        return _message_response("unavailable", apl)
    if "malformed" in (current.kind, future.kind):
        return _message_response("malformed", apl)

    try:
        current_rates = parse_state_rates(current.state)
        future_rates = parse_state_rates(future.state) if future.kind == "ok" else []
        briefing = build_briefing(merge_rates(current_rates, future_rates), _utcnow())
    except RateDataError as exc:
        LOGGER.warning("briefing outcome=malformed code=%s", str(exc))
        return _message_response("malformed", apl)
    except NoCurrentCoverage:
        LOGGER.info("briefing outcome=no_coverage")
        return _message_response("no_coverage", apl)

    directives = [_apl_directive(presentation.briefing_datasource(briefing))] if apl else None
    speech = presentation.briefing_speech(briefing)
    card = {"type": "Simple", "title": "Electricity rates", "content": speech}
    return _response(speech, card=card, directives=directives)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    _verify_request(event)
    request = _dig(event, "request")
    request_type = request.get("type") if isinstance(request, dict) else None
    intent_name = _dig(request, "intent", "name")
    apl = _supports_apl(event)
    LOGGER.info("alexa request type=%s intent=%s apl=%s", request_type, intent_name, apl)

    try:
        if request_type == "LaunchRequest":
            return _briefing_response(event, context, apl)
        if request_type == "IntentRequest":
            if intent_name == "GetTariffIntent":
                return _briefing_response(event, context, apl)
            if intent_name == "AMAZON.HelpIntent":
                return _message_response(
                    "help", apl, end_session=False, reprompt=presentation.MESSAGES["fallback"].speech
                )
            if intent_name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
                return _message_response("goodbye", apl)
            return _message_response(
                "fallback", apl, end_session=False, reprompt=presentation.MESSAGES["fallback"].speech
            )
        if request_type == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}
        return _message_response("fallback", apl)
    except Exception as exc:  # noqa: BLE001 - never crash the voice response
        LOGGER.error("alexa request outcome=unexpected kind=%s", type(exc).__name__)
        try:
            return _message_response("unexpected", apl)
        except Exception:  # noqa: BLE001 - e.g. APL document unreadable; fall back to voice only
            return _message_response("unexpected", False)
