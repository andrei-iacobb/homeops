"""Unit tests for lambda_function.py. All HTTP is mocked at _perform_get or the opener."""

from __future__ import annotations

import io
import json
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

import lambda_function as lf

UTC = timezone.utc
HALF = timedelta(minutes=30)

FAKE_TOKEN = "FAKE-HA-TOKEN-not-a-real-credential-0000"
SKILL_ID = "amzn1.ask.skill.00000000-0000-4000-8000-000000000000"
OTHER_SKILL_ID = "amzn1.ask.skill.ffffffff-ffff-4fff-8fff-ffffffffffff"
NOW = datetime(2026, 11, 1, 10, 45, tzinfo=UTC)
SECRET_BODY = b'{"message":"SECRET-RESPONSE-BODY-MUST-NOT-LEAK"}'

CURRENT_URL = lf._state_url(lf.CURRENT_DAY_ENTITY)
NEXT_URL = lf._state_url(lf.NEXT_DAY_ENTITY)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def rate_entries(start: datetime, prices) -> list[dict]:
    return [
        {
            "start": iso(start + index * HALF),
            "end": iso(start + (index + 1) * HALF),
            "value_inc_vat": price,
            "is_capped": False,
        }
        for index, price in enumerate(prices)
    ]


def state_body(entries, state: str = "2026-11-01T00:00:00+00:00") -> bytes:
    return json.dumps({"state": state, "attributes": {"rates": entries}}).encode("utf-8")


TODAY = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
TOMORROW = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
TODAY_BODY = state_body(rate_entries(TODAY, [0.10 + 0.005 * i for i in range(48)]))
TOMORROW_BODY = state_body(rate_entries(TOMORROW, [0.20] * 48))
EMPTY_BODY = json.dumps({"state": "unknown", "attributes": {}}).encode("utf-8")


def make_event(
    request_type: str = "LaunchRequest",
    intent: str | None = None,
    token: str | None = FAKE_TOKEN,
    system_app: str | None = SKILL_ID,
    session_app: str | None = SKILL_ID,
    apl: bool = False,
    timestamp: str | None = None,
    include_session: bool = True,
) -> dict:
    request: dict = {
        "type": request_type,
        "requestId": "amzn1.echo-api.request.fake",
        "timestamp": timestamp if timestamp is not None else iso(NOW),
        "locale": "en-GB",
    }
    if intent:
        request["intent"] = {"name": intent, "confirmationStatus": "NONE"}
    system: dict = {"user": {"userId": "amzn1.ask.account.fake"}, "device": {"deviceId": "fake"}}
    if system_app is not None:
        system["application"] = {"applicationId": system_app}
    if token is not None:
        system["user"]["accessToken"] = token
    if apl:
        system["device"]["supportedInterfaces"] = {"Alexa.Presentation.APL": {"runtime": {"maxVersion": "2023.3"}}}
    else:
        system["device"]["supportedInterfaces"] = {"AudioPlayer": {}}
    event: dict = {"version": "1.0", "request": request, "context": {"System": system}}
    if include_session:
        session: dict = {"new": True, "sessionId": "amzn1.echo-api.session.fake", "user": {"userId": "amzn1.ask.account.fake"}}
        if session_app is not None:
            session["application"] = {"applicationId": session_app}
        if token is not None:
            session["user"]["accessToken"] = token
        event["session"] = session
    return event


class FakeContext:
    def __init__(self, remaining_ms: int = 8000):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


class FakeHTTP:
    """Replacement for lambda_function._perform_get keyed by URL."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url, token, timeout, opener=None):
        self.calls.append((url, timeout))
        outcome = self.responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome


def speech_of(response: dict) -> str:
    return response["response"]["outputSpeech"]["text"]


def directives_of(response: dict) -> list:
    return response["response"].get("directives", [])


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict("os.environ", {lf.SKILL_ID_ENV: SKILL_ID})
        env.start()
        self.addCleanup(env.stop)
        now = mock.patch.object(lf, "_utcnow", return_value=NOW)
        now.start()
        self.addCleanup(now.stop)

    def run_with_http(self, event, responses, context=None):
        fake = FakeHTTP(responses)
        with mock.patch.object(lf, "_perform_get", fake):
            response = lf.lambda_handler(event, context or FakeContext())
        return response, fake

    def assert_no_leak(self, response, logs=""):
        encoded = json.dumps(response)
        for secret in (FAKE_TOKEN, SECRET_BODY.decode("utf-8"), "SECRET-RESPONSE-BODY"):
            self.assertNotIn(secret, encoded)
            self.assertNotIn(secret, logs)


class VerificationTests(HandlerTestCase):
    def test_rejects_when_env_missing(self):
        with mock.patch.dict("os.environ", {lf.SKILL_ID_ENV: ""}):
            with self.assertRaises(lf.RequestRejected):
                lf.lambda_handler(make_event(), FakeContext())

    def test_rejects_system_application_mismatch(self):
        with self.assertRaises(lf.RequestRejected):
            lf.lambda_handler(make_event(system_app=OTHER_SKILL_ID), FakeContext())

    def test_rejects_session_mismatch_even_when_system_matches(self):
        with self.assertRaises(lf.RequestRejected):
            lf.lambda_handler(make_event(session_app=OTHER_SKILL_ID), FakeContext())

    def test_rejects_when_neither_application_present(self):
        with self.assertRaises(lf.RequestRejected):
            lf.lambda_handler(make_event(system_app=None, session_app=None), FakeContext())

    def test_accepts_when_only_system_application_present(self):
        event = make_event(request_type="IntentRequest", intent="AMAZON.StopIntent", session_app=None)
        response = lf.lambda_handler(event, FakeContext())
        self.assertEqual(speech_of(response), "Okay.")

    def test_accepts_without_session_object(self):
        event = make_event(request_type="IntentRequest", intent="AMAZON.StopIntent", include_session=False)
        self.assertEqual(speech_of(lf.lambda_handler(event, FakeContext())), "Okay.")

    def test_rejects_non_string_application_id(self):
        with self.assertRaises(lf.RequestRejected):
            lf.lambda_handler(make_event(system_app=12345), FakeContext())

    def test_rejects_stale_and_future_timestamps(self):
        stale = iso(NOW - timedelta(seconds=lf.TIMESTAMP_TOLERANCE_SECONDS + 1))
        with self.assertRaises(lf.RequestRejected):
            lf.lambda_handler(make_event(timestamp=stale), FakeContext())
        ahead = iso(NOW + timedelta(seconds=lf.TIMESTAMP_TOLERANCE_SECONDS + 1))
        with self.assertRaises(lf.RequestRejected):
            lf.lambda_handler(make_event(timestamp=ahead), FakeContext())

    def test_accepts_timestamp_inside_tolerance(self):
        fresh = iso(NOW - timedelta(seconds=lf.TIMESTAMP_TOLERANCE_SECONDS - 5))
        event = make_event(request_type="IntentRequest", intent="AMAZON.StopIntent", timestamp=fresh)
        self.assertEqual(speech_of(lf.lambda_handler(event, FakeContext())), "Okay.")

    def test_rejects_missing_naive_or_invalid_timestamp(self):
        for stamp in ("", "2026-11-01T10:45:00", "not-a-time"):
            with self.subTest(stamp=stamp):
                with self.assertRaises(lf.RequestRejected):
                    lf.lambda_handler(make_event(timestamp=stamp), FakeContext())

    def test_rejection_happens_before_any_fetch(self):
        fake = FakeHTTP({})
        with mock.patch.object(lf, "_perform_get", fake):
            with self.assertRaises(lf.RequestRejected):
                lf.lambda_handler(make_event(system_app=OTHER_SKILL_ID), FakeContext())
        self.assertEqual(fake.calls, [])

    def test_rejection_message_carries_no_identifiers(self):
        try:
            lf.lambda_handler(make_event(system_app=OTHER_SKILL_ID), FakeContext())
        except lf.RequestRejected as exc:
            self.assertNotIn(OTHER_SKILL_ID, str(exc))
            self.assertNotIn(SKILL_ID, str(exc))


class TokenTests(HandlerTestCase):
    def test_missing_token_gives_link_account_card_without_fetch(self):
        response, fake = self.run_with_http(make_event(token=None), {})
        self.assertEqual(fake.calls, [])
        self.assertEqual(response["response"]["card"], {"type": "LinkAccount"})
        self.assertIn("link", speech_of(response).lower())
        self.assertTrue(response["response"]["shouldEndSession"])

    def test_blank_token_treated_as_missing(self):
        response, fake = self.run_with_http(make_event(token="   "), {})
        self.assertEqual(fake.calls, [])
        self.assertEqual(response["response"]["card"]["type"], "LinkAccount")

    def test_unsafe_header_tokens_never_reach_http(self):
        for token in ["token\r\nInjected: yes", "token\x00", "token\u00e9", " token"]:
            with self.subTest(token=repr(token)):
                response, fake = self.run_with_http(make_event(token=token), {})
                self.assertEqual(fake.calls, [])
                self.assertEqual(response["response"]["card"]["type"], "LinkAccount")

    def test_session_token_used_when_system_token_absent(self):
        event = make_event()
        del event["context"]["System"]["user"]["accessToken"]
        self.assertEqual(lf._access_token(event), FAKE_TOKEN)

    def test_401_gives_link_account_and_leaks_nothing(self):
        responses = {CURRENT_URL: (401, SECRET_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        with self.assertLogs(lf.LOGGER, level="INFO") as captured:
            response, _ = self.run_with_http(make_event(apl=True), responses)
        self.assertEqual(response["response"]["card"], {"type": "LinkAccount"})
        self.assert_no_leak(response, "\n".join(captured.output))
        datasource = directives_of(response)[0]["datasources"]["tariff"]
        self.assertEqual(datasource["state"], "message")
        self.assertEqual(datasource["headline"], "Link Home Assistant")

    def test_403_on_future_feed_also_links(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (403, SECRET_BODY)}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertEqual(response["response"]["card"]["type"], "LinkAccount")
        self.assert_no_leak(response)


class BriefingTests(HandlerTestCase):
    def test_voice_only_success_has_no_directive(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        response, fake = self.run_with_http(make_event(), responses)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual({url for url, _ in fake.calls}, {CURRENT_URL, NEXT_URL})
        for _, timeout in fake.calls:
            self.assertEqual(timeout, lf.IO_TIMEOUT_SECONDS)
        speech = speech_of(response)
        self.assertIn("between 10:00 and 11:00 averages", speech)
        self.assertIn("from 11:00 to 12:00, averages", speech)
        self.assertNotIn("directives", response["response"])
        self.assertEqual(response["response"]["card"]["type"], "Simple")
        self.assertEqual(response["response"]["outputSpeech"]["type"], "PlainText")
        self.assert_no_leak(response)

    def test_tariff_intent_renders_apl_on_supported_device(self):
        event = make_event(request_type="IntentRequest", intent="GetTariffIntent", apl=True)
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        response, _ = self.run_with_http(event, responses)
        directives = directives_of(response)
        self.assertEqual(len(directives), 1)
        directive = directives[0]
        self.assertEqual(directive["type"], "Alexa.Presentation.APL.RenderDocument")
        self.assertEqual(directive["document"]["type"], "APL")
        self.assertEqual(directive["document"]["version"], "1.8")
        tariff = directive["datasources"]["tariff"]
        self.assertEqual(tariff["state"], "ok")
        self.assertEqual(len(tariff["bars"]), 13)
        self.assertEqual(tariff["bars"][0]["time"], "10:30")
        json.dumps(response, allow_nan=False)
        self.assert_no_leak(response)

    def test_apl_document_references_only_inline_assets(self):
        document = lf._load_document()
        encoded = json.dumps(document)
        self.assertNotIn("http://", encoded)
        self.assertNotIn("https://", encoded)
        self.assertEqual(document["mainTemplate"]["parameters"], ["payload"])
        self.assertNotIn("import", document)

    def test_future_feed_404_proceeds_with_truncated_coverage(self):
        late = datetime(2026, 11, 1, 22, 45, tzinfo=UTC)
        with mock.patch.object(lf, "_utcnow", return_value=late):
            event = make_event(timestamp=iso(late), apl=True)
            responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (404, b"")}
            response, _ = self.run_with_http(event, responses)
        speech = speech_of(response)
        self.assertIn("aren't available yet", speech)
        self.assertIn("between 22:00 and 23:00 averages", speech)
        tariff = directives_of(response)[0]["datasources"]["tariff"]
        self.assertEqual(tariff["state"], "ok")
        self.assertEqual(len(tariff["bars"]), 3)
        self.assertIn("later rates unavailable", tariff["coverage"])

    def test_future_feed_server_error_proceeds(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (500, SECRET_BODY)}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertIn("between 10:00 and 11:00 averages", speech_of(response))
        self.assert_no_leak(response)

    def test_future_feed_transport_error_proceeds(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: OSError("connection refused")}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertIn("between 10:00 and 11:00 averages", speech_of(response))

    def test_future_feed_unknown_state_proceeds(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (200, EMPTY_BODY)}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertIn("between 10:00 and 11:00 averages", speech_of(response))
        self.assertNotIn("published", speech_of(response))  # today covers the 6 h horizon

    def test_current_feed_error_is_unavailable(self):
        responses = {CURRENT_URL: (500, SECRET_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        response, _ = self.run_with_http(make_event(apl=True), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["unavailable"].speech)
        tariff = directives_of(response)[0]["datasources"]["tariff"]
        self.assertEqual(tariff["state"], "message")
        self.assertEqual(tariff["headline"], "Rates unavailable")
        self.assert_no_leak(response)

    def test_current_feed_transport_error_is_unavailable(self):
        responses = {CURRENT_URL: OSError("boom"), NEXT_URL: (200, TOMORROW_BODY)}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["unavailable"].speech)
        self.assertNotIn("boom", json.dumps(response))

    def test_malformed_rate_entry_is_friendly_and_specific(self):
        entries = rate_entries(TODAY, [0.1] * 48)
        entries[21]["value_inc_vat"] = "NaN"
        body = state_body(entries).replace(b'"NaN"', b"NaN")  # python json emits NaN token
        responses = {CURRENT_URL: (200, body), NEXT_URL: (200, TOMORROW_BODY)}
        response, _ = self.run_with_http(make_event(apl=True), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["malformed"].speech)
        self.assertEqual(directives_of(response)[0]["datasources"]["tariff"]["headline"], "Rate data looks wrong")

    def test_conflicting_duplicate_across_feeds_is_malformed(self):
        tomorrow = rate_entries(TODAY + 46 * HALF, [0.99, 0.99])  # overlaps today's last two slots
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (200, state_body(tomorrow))}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["malformed"].speech)

    def test_identical_midnight_duplicates_merge_cleanly(self):
        today = rate_entries(TODAY, [0.1] * 48)
        tomorrow = today[-2:] + rate_entries(TOMORROW, [0.2] * 48)
        responses = {CURRENT_URL: (200, state_body(today)), NEXT_URL: (200, state_body(tomorrow))}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertIn("averages 10.0 pence", speech_of(response))

    def test_invalid_json_body_is_malformed(self):
        responses = {CURRENT_URL: (200, b"<html>not json</html>"), NEXT_URL: (200, TOMORROW_BODY)}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["malformed"].speech)
        self.assertNotIn("html", json.dumps(response))

    def test_no_current_coverage_even_with_future_feed(self):
        responses = {CURRENT_URL: (200, EMPTY_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        response, _ = self.run_with_http(make_event(apl=True), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["no_coverage"].speech)
        self.assertEqual(directives_of(response)[0]["datasources"]["tariff"]["headline"], "No rate for now")

    def test_stale_current_feed_with_fresh_coverage_is_fine(self):
        body = state_body(rate_entries(TODAY, [0.1] * 48), state="2026-10-31T16:00:00+00:00")
        responses = {CURRENT_URL: (200, body), NEXT_URL: (404, b"")}
        response, _ = self.run_with_http(make_event(), responses)
        self.assertIn("averages 10.0 pence", speech_of(response))

    def test_unexpected_exception_gives_friendly_response(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        with mock.patch.object(lf, "build_briefing", side_effect=RuntimeError("secret internal detail")):
            response, _ = self.run_with_http(make_event(apl=True), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["unexpected"].speech)
        self.assertNotIn("secret internal detail", json.dumps(response))
        self.assertEqual(directives_of(response)[0]["datasources"]["tariff"]["state"], "message")

    def test_unexpected_exception_with_broken_apl_document_still_answers(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        with mock.patch.object(lf, "_load_document", side_effect=OSError("missing")):
            response, _ = self.run_with_http(make_event(apl=True), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["unexpected"].speech)
        self.assertNotIn("directives", response["response"])


class DeadlineTests(HandlerTestCase):
    def test_slow_feed_hits_deadline_without_blocking(self):
        def slow():
            time.sleep(1.0)
            return (200, TODAY_BODY)

        responses = {CURRENT_URL: slow, NEXT_URL: (200, TOMORROW_BODY)}
        started = time.monotonic()
        with mock.patch.object(lf, "TOTAL_BUDGET_SECONDS", 0.1):
            response, _ = self.run_with_http(make_event(apl=True), responses)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["deadline"].speech)
        self.assertEqual(directives_of(response)[0]["datasources"]["tariff"]["state"], "message")

    def test_slow_future_feed_also_means_unavailable_not_partial(self):
        def slow():
            time.sleep(1.0)
            return (200, TOMORROW_BODY)

        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: slow}
        with mock.patch.object(lf, "TOTAL_BUDGET_SECONDS", 0.1):
            response, _ = self.run_with_http(make_event(), responses)
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["deadline"].speech)

    def test_budget_respects_lambda_remaining_time(self):
        self.assertEqual(lf._call_budget(FakeContext(8000)), lf.TOTAL_BUDGET_SECONDS)
        self.assertAlmostEqual(lf._call_budget(FakeContext(2000)), 2.0 - lf.DEADLINE_MARGIN_SECONDS)
        self.assertEqual(lf._call_budget(FakeContext(500)), 0.0)
        self.assertEqual(lf._call_budget(object()), lf.TOTAL_BUDGET_SECONDS)

    def test_zero_budget_returns_deadline_immediately(self):
        responses = {CURRENT_URL: (200, TODAY_BODY), NEXT_URL: (200, TOMORROW_BODY)}
        response, _ = self.run_with_http(make_event(), responses, context=FakeContext(100))
        self.assertEqual(speech_of(response), lf.presentation.MESSAGES["deadline"].speech)

    def test_total_budget_fits_alexa_window(self):
        self.assertLessEqual(lf.TOTAL_BUDGET_SECONDS + lf.DEADLINE_MARGIN_SECONDS, 8.0)
        self.assertLessEqual(lf.IO_TIMEOUT_SECONDS, lf.TOTAL_BUDGET_SECONDS)


class RoutingTests(HandlerTestCase):
    def assert_no_fetch(self, event):
        response, fake = self.run_with_http(event, {})
        self.assertEqual(fake.calls, [])
        return response

    def test_help_explains_and_keeps_session_without_fetch(self):
        response = self.assert_no_fetch(make_event(request_type="IntentRequest", intent="AMAZON.HelpIntent"))
        speech = speech_of(response)
        self.assertIn("electricity price", speech)
        self.assertIn("link", speech.lower())
        self.assertFalse(response["response"]["shouldEndSession"])
        self.assertIn("reprompt", response["response"])
        self.assertNotIn("directives", response["response"])

    def test_help_on_apl_device_renders_message_document(self):
        response = self.assert_no_fetch(make_event(request_type="IntentRequest", intent="AMAZON.HelpIntent", apl=True))
        tariff = directives_of(response)[0]["datasources"]["tariff"]
        self.assertEqual(tariff["state"], "message")
        self.assertEqual(tariff["headline"], "Home energy")

    def test_stop_and_cancel_end_quietly_without_fetch_or_directive(self):
        for intent in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
            with self.subTest(intent=intent):
                response = self.assert_no_fetch(make_event(request_type="IntentRequest", intent=intent, apl=True))
                self.assertEqual(speech_of(response), "Okay.")
                self.assertTrue(response["response"]["shouldEndSession"])
                self.assertNotIn("directives", response["response"])

    def test_fallback_and_unknown_intents_are_friendly(self):
        for intent in ("AMAZON.FallbackIntent", "SomethingElseIntent"):
            with self.subTest(intent=intent):
                response = self.assert_no_fetch(make_event(request_type="IntentRequest", intent=intent))
                self.assertIn("electricity price", speech_of(response))
                self.assertFalse(response["response"]["shouldEndSession"])

    def test_session_ended_is_silent_and_valid(self):
        response = self.assert_no_fetch(make_event(request_type="SessionEndedRequest"))
        self.assertEqual(response, {"version": "1.0", "response": {}})

    def test_unknown_request_type_does_not_fetch(self):
        response = self.assert_no_fetch(make_event(request_type="Alexa.Presentation.APL.UserEvent"))
        self.assertIn("electricity price", speech_of(response))
        self.assertTrue(response["response"]["shouldEndSession"])

    def test_missing_request_object_is_handled(self):
        event = make_event()
        event["request"] = {"timestamp": iso(NOW)}
        response = self.assert_no_fetch(event)
        self.assertIn("electricity price", speech_of(response))

    def test_apl_detection(self):
        self.assertTrue(lf._supports_apl(make_event(apl=True)))
        self.assertFalse(lf._supports_apl(make_event(apl=False)))
        self.assertFalse(lf._supports_apl({}))


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class TransportTests(unittest.TestCase):
    def test_state_urls_are_fixed_https_on_the_configured_host(self):
        for url in (CURRENT_URL, NEXT_URL):
            self.assertTrue(url.startswith("https://hass.iacob.co.uk/api/states/event.octopus_energy_electricity_"))
        self.assertNotEqual(CURRENT_URL, NEXT_URL)

    def test_refuses_other_hosts_and_plain_http(self):
        for url in (
            "http://hass.iacob.co.uk/api/states/x",
            "https://evil.example/api/states/x",
            "https://hass.iacob.co.uk.evil.example/api/states/x",
        ):
            with self.subTest(url=url):
                opener = FakeOpener(FakeResponse(200, b"{}"))
                with self.assertRaises(lf.FeedError):
                    lf._perform_get(url, FAKE_TOKEN, 1.0, opener=opener)
                self.assertEqual(opener.requests, [])

    def test_request_is_get_with_bearer_and_timeout(self):
        opener = FakeOpener(FakeResponse(200, b"{}"))
        status, body = lf._perform_get(CURRENT_URL, FAKE_TOKEN, 2.5, opener=opener)
        self.assertEqual((status, body), (200, b"{}"))
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 2.5)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_TOKEN}")
        self.assertEqual(request.full_url, CURRENT_URL)

    def test_body_over_cap_is_refused(self):
        opener = FakeOpener(FakeResponse(200, b"x" * (lf.MAX_BODY_BYTES + 1)))
        with self.assertRaises(lf.FeedError):
            lf._perform_get(CURRENT_URL, FAKE_TOKEN, 1.0, opener=opener)

    def test_body_at_cap_is_accepted(self):
        opener = FakeOpener(FakeResponse(200, b"x" * lf.MAX_BODY_BYTES))
        status, body = lf._perform_get(CURRENT_URL, FAKE_TOKEN, 1.0, opener=opener)
        self.assertEqual(len(body), lf.MAX_BODY_BYTES)

    def test_http_error_status_returned_without_body(self):
        error = urllib.error.HTTPError(CURRENT_URL, 401, "Unauthorized", {}, io.BytesIO(SECRET_BODY))
        opener = FakeOpener(error=error)
        status, body = lf._perform_get(CURRENT_URL, FAKE_TOKEN, 1.0, opener=opener)
        self.assertEqual(status, 401)
        self.assertEqual(body, b"")

    def test_proxy_challenge_does_not_request_account_relinking(self):
        for headers in [{"Content-Type": "text/html"}, {"cf-mitigated": "challenge"}]:
            with self.subTest(headers=headers):
                error = urllib.error.HTTPError(CURRENT_URL, 403, "Forbidden", headers, io.BytesIO(SECRET_BODY))
                status, body = lf._perform_get(CURRENT_URL, FAKE_TOKEN, 1.0, opener=FakeOpener(error=error))
                self.assertEqual(status, 502)
                self.assertEqual(body, b"")
                self.assertTrue(error.closed)

    def test_redirects_are_refused(self):
        handler = lf._NoRedirectHandler()
        request = urllib.request.Request(CURRENT_URL, headers={"Authorization": "Bearer x"})
        self.assertIsNone(
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/")
        )
        opener = lf._build_opener()
        redirect_handlers = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertTrue(redirect_handlers)
        for handler in redirect_handlers:
            self.assertIsInstance(handler, lf._NoRedirectHandler)

    def test_redirect_status_counts_as_feed_error_not_auth(self):
        with mock.patch.object(lf, "_perform_get", return_value=(302, b"")):
            outcome = lf._fetch_feed(lf.CURRENT_DAY_ENTITY, FAKE_TOKEN)
        self.assertEqual(outcome.kind, "error")

    def test_zero_budget_skips_network_entirely(self):
        with mock.patch.object(lf, "_perform_get") as perform:
            outcomes = lf._fetch_both(FAKE_TOKEN, 0.0)
        self.assertEqual([o.kind for o in outcomes], ["timeout", "timeout"])
        perform.assert_not_called()

    def test_tls_context_verifies(self):
        self.assertTrue(lf._SSL_CONTEXT.check_hostname)
        self.assertEqual(lf._SSL_CONTEXT.verify_mode, lf.ssl.CERT_REQUIRED)

    def test_fetch_feed_maps_statuses(self):
        for status, kind in ((401, "auth"), (403, "auth"), (404, "missing"), (500, "error"), (302, "error")):
            with self.subTest(status=status):
                with mock.patch.object(lf, "_perform_get", return_value=(status, SECRET_BODY)):
                    self.assertEqual(lf._fetch_feed(lf.CURRENT_DAY_ENTITY, FAKE_TOKEN).kind, kind)
        with mock.patch.object(lf, "_perform_get", return_value=(200, b"{}")):
            outcome = lf._fetch_feed(lf.CURRENT_DAY_ENTITY, FAKE_TOKEN)
        self.assertEqual(outcome.kind, "ok")
        self.assertEqual(outcome.state, {})


if __name__ == "__main__":
    unittest.main()
