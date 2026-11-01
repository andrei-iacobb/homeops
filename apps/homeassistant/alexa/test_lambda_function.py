"""Offline regressions for the Alexa/Home Assistant authentication boundary."""

import copy
import http.client
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import lambda_function as bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "directive": {
                "header": {
                    "namespace": "Alexa.PowerController",
                    "name": "TurnOn",
                    "payloadVersion": "3",
                    "messageId": "incoming-id",
                    "correlationToken": "correlate-this-command",
                },
                "endpoint": {
                    "endpointId": "light#bedroom",
                    "scope": {"type": "BearerToken", "token": "account-token"},
                },
                "payload": {},
            }
        }

    def invoke_response(self, body):
        with patch.object(bridge._opener, "open", return_value=io.BytesIO(body)) as opened:
            result = bridge.lambda_handler(self.event, None)
        return result, opened

    def assert_error(self, result, error_type):
        event = result["event"]
        self.assertEqual(event["payload"]["type"], error_type)
        self.assertEqual(event["header"]["namespace"], "Alexa")
        self.assertEqual(event["header"]["name"], "ErrorResponse")
        self.assertEqual(event["header"]["payloadVersion"], "3")
        self.assertNotEqual(event["header"]["messageId"], "incoming-id")
        self.assertEqual(event["header"]["correlationToken"], "correlate-this-command")
        self.assertEqual(event["endpoint"], {"endpointId": "light#bedroom"})
        self.assertNotIn("account-token", json.dumps(result))

    def test_forwarding_preserves_success_and_supports_all_scope_locations(self):
        for location, namespace, name in [
            ("endpoint", "Alexa.PowerController", "Response"),
            ("scope", "Alexa.Discovery", "Discover.Response"),
            ("grantee", "Alexa.Authorization", "AcceptGrant.Response"),
        ]:
            with self.subTest(location=location):
                self.setUp()
                directive = self.event["directive"]
                token_scope = directive["endpoint"].pop("scope")
                if location == "endpoint":
                    directive["endpoint"]["scope"] = token_scope
                else:
                    directive.pop("endpoint")
                    directive["payload"][location] = token_scope
                expected = {"event": {"header": {"namespace": namespace, "name": name}, "payload": {}}}
                result, opened = self.invoke_response(json.dumps(expected).encode())
                self.assertEqual(result, expected)
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, bridge.HA_URL)
                self.assertEqual(request.get_header("Authorization"), "Bearer account-token")
                self.assertEqual(json.loads(request.data), self.event)

    def test_bad_tokens_never_reach_home_assistant(self):
        for scope in [None, {}, {"token": "account-token"}, {"type": "BearerToken", "token": ""}, {"type": "BearerToken", "token": 123}, {"type": "BearerToken", "token": "token\r\nX-Evil: yes"}]:
            with self.subTest(scope=scope), patch.object(bridge._opener, "open") as opened:
                self.event["directive"]["endpoint"]["scope"] = scope
                result = bridge.lambda_handler(self.event, None)
                self.assert_error(result, "INVALID_AUTHORIZATION_CREDENTIAL")
                opened.assert_not_called()

    def test_http_authentication_failures_are_not_internal_errors_or_body_leaks(self):
        for status in [401, 403]:
            with self.subTest(status=status):
                error = urllib.error.HTTPError(bridge.HA_URL, status, "private detail", {}, io.BytesIO(b"account-token private HA body"))
                with patch.object(bridge._opener, "open", side_effect=error):
                    result = bridge.lambda_handler(self.event, None)
                self.assert_error(result, "INVALID_AUTHORIZATION_CREDENTIAL")
                self.assertNotIn("private", json.dumps(result))

    def test_unreachable_bridge_has_correlated_sanitized_error(self):
        failures = [TimeoutError("account-token"), urllib.error.URLError("private address account-token"), http.client.IncompleteRead(b"account-token"), urllib.error.HTTPError(bridge.HA_URL, 503, "account-token", {}, io.BytesIO(b"secret"))]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(bridge._opener, "open", side_effect=failure):
                result = bridge.lambda_handler(self.event, None)
                self.assert_error(result, "BRIDGE_UNREACHABLE")

    def test_cloudflare_challenges_do_not_invalidate_account_linking(self):
        for headers in [{"Content-Type": "text/html; charset=UTF-8"}, {"cf-mitigated": "challenge"}]:
            with self.subTest(headers=headers):
                error = urllib.error.HTTPError(bridge.HA_URL, 403, "challenge", headers, io.BytesIO(b"private proxy response"))
                with patch.object(bridge._opener, "open", side_effect=error):
                    result = bridge.lambda_handler(self.event, None)
                self.assert_error(result, "BRIDGE_UNREACHABLE")

    def test_redirect_and_other_rejections_do_not_leak_http_details(self):
        for status in [301, 302, 307, 308, 400, 404, 429]:
            with self.subTest(status=status):
                error = urllib.error.HTTPError(bridge.HA_URL, status, "account-token", {}, io.BytesIO(b"private response"))
                with patch.object(bridge._opener, "open", side_effect=error):
                    result = bridge.lambda_handler(self.event, None)
                self.assert_error(result, "INTERNAL_ERROR")
                self.assertNotIn("private", json.dumps(result))

    def test_redirects_do_not_forward_bearer_credentials(self):
        request = bridge.urllib.request.Request(bridge.HA_URL, headers={"Authorization": "Bearer account-token"})
        for status in [301, 302, 303, 307, 308]:
            with self.subTest(status=status):
                self.assertIsNone(bridge.NoRedirect().redirect_request(request, None, status, "Moved", {}, "https://other.example/"))

    def test_invalid_or_oversized_responses_do_not_escape_as_success(self):
        for body in [b"", b"<html>private account-token</html>", b"null", b"[]", b'{}', b'{"event":{"header":null}}', b"\xff", b" " * (bridge.MAX_RESPONSE_BYTES + 1)]:
            with self.subTest(body=body[:30]):
                result, _ = self.invoke_response(body)
                self.assert_error(result, "INTERNAL_ERROR")

    def test_malformed_directives_fail_before_network(self):
        for event in [None, [], {}, {"directive": []}, {"directive": {"header": None}}]:
            with self.subTest(event=event), patch.object(bridge._opener, "open") as opened:
                result = bridge.lambda_handler(event, None)
                self.assertEqual(result["event"]["payload"]["type"], "INVALID_DIRECTIVE")
                opened.assert_not_called()

    def test_error_message_ids_are_unique(self):
        self.event["directive"]["endpoint"]["scope"] = None
        first = bridge.lambda_handler(copy.deepcopy(self.event), None)
        second = bridge.lambda_handler(copy.deepcopy(self.event), None)
        self.assertNotEqual(first["event"]["header"]["messageId"], second["event"]["header"]["messageId"])


if __name__ == "__main__":
    unittest.main()
