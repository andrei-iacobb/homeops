from __future__ import annotations

import json
import stat
import unittest
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import recorder


class FakeResponse:
    def __init__(self, body=b"", headers=None, status=200):
        self.body = body
        self.headers = headers or {}
        self.status = status
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount=-1):
        if amount is None or amount < 0:
            amount = len(self.body) - self.offset
        result = self.body[self.offset : self.offset + amount]
        self.offset += len(result)
        return result


def response_json(value, headers=None):
    return FakeResponse(json.dumps(value).encode(), headers=headers)


class RecorderTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        warning_patch = patch.object(recorder.LOG, "warning")
        warning_patch.start()
        self.addCleanup(warning_patch.stop)
        self.config = recorder.Config()
        self.config.storage_dir = Path(self.temporary.name)
        self.config.public_base_url = "https://hass.iacob.uk/ring-events"
        self.config.time_zone = recorder.ZoneInfo("Europe/London")
        self.config.retention_days = 180
        self.config.ring_refresh_token = "environment-seed"
        self.config.ring_hardware_id = "hardware-id"
        self.config.gemini_api_key = ""
        self.config.gemini_model = "test-model"
        self.config.api_token = ""
        self.config.index_path = self.config.storage_dir / "index.html"
        self.config.card_path = self.config.storage_dir / "card.js"

    def make_recorder(self):
        return recorder.Recorder(self.config)

    def add_event(self, store, event_id, created_at, device=None):
        device = device or {
            "id": "device-1",
            "name": "Front Door",
            "slug": "front-door",
            "kind": "doorbell",
        }
        store.upsert_device(device)
        store.add_discovered_event(
            {
                "id": event_id,
                "created_at": created_at,
                "kind": "motion",
                "duration": 10,
                "recording": {"status": "ready"},
            },
            device,
            disabled=False,
        )

    def test_rotated_token_is_persisted_with_private_mode_and_preferred(self):
        metrics = recorder.Metrics()
        client = recorder.RingClient(self.config, metrics)
        self.assertEqual(client.refresh_token, "environment-seed")
        payload = {
            "access_token": "access-one",
            "refresh_token": "rotated-one",
            "expires_in": 3600,
        }
        with patch.object(recorder, "urlopen", return_value=response_json(payload)):
            client.refresh()

        stored = json.loads(self.config.token_path.read_text())
        self.assertEqual(stored["refresh_token"], "rotated-one")
        mode = stat.S_IMODE(self.config.token_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

        self.config.ring_refresh_token = "different-environment-seed"
        restarted = recorder.RingClient(self.config, recorder.Metrics())
        self.assertEqual(restarted.refresh_token, "rotated-one")

    def test_history_backfills_until_empty_and_stops_at_known_id(self):
        service = self.make_recorder()
        device = {
            "id": "device-1",
            "name": "Front Door",
            "slug": "front-door",
            "kind": "doorbell",
        }
        full_page = [
            {
                "id": f"event-{number}",
                "created_at": f"2026-11-15T12:{number:02d}:00Z",
                "kind": "motion",
            }
            for number in range(recorder.HISTORY_LIMIT)
        ]
        last_page = [
            {"id": "old-1", "created_at": "2026-11-14T11:00:00Z", "kind": "ding"},
            {"id": "old-2", "created_at": "2026-11-14T10:00:00Z", "kind": "motion"},
        ]
        service.ring = Mock()
        service.ring.history.side_effect = [full_page, last_page, []]
        with patch.object(recorder.time, "sleep"):
            discovered = service.history_events(device)
        self.assertEqual(len(discovered), recorder.HISTORY_LIMIT + 2)
        self.assertEqual(service.ring.history.call_count, 3)
        self.assertTrue(service.store.event_exists("old-2"))

        with service.store.connect() as connection:
            connection.execute("UPDATE events SET recording_status = 'expired'")
        self.add_event(service.store, "known", "2026-11-15T11:58:00Z", device)
        with service.store.connect() as connection:
            connection.execute(
                "UPDATE events SET recording_status = 'expired' WHERE event_id = 'known'"
            )
        service.ring.history.reset_mock()
        service.ring.history.side_effect = [[
            {"id": "new", "created_at": "2026-11-15T12:01:00Z", "kind": "motion"},
            {"id": "known", "created_at": "2026-11-15T11:58:00Z", "kind": "motion"},
            {"id": "must-not-insert", "created_at": "2026-11-15T11:57:00Z", "kind": "motion"},
        ]]
        service.history_events(device)
        self.assertEqual(service.ring.history.call_count, 1)
        self.assertTrue(service.store.event_exists("new"))
        self.assertFalse(service.store.event_exists("must-not-insert"))

    def test_recording_not_ready_is_retried_then_expires_after_24_hours(self):
        service = self.make_recorder()
        self.add_event(service.store, "pending-1", "2026-11-15T12:00:00Z")
        service._download = Mock(side_effect=recorder.RecordingNotReady("not ready"))

        service.process_pending_recordings()
        self.assertEqual(service.store.get_row("pending-1")["recording_status"], "pending")
        self.assertEqual(service._download.call_count, 1)

        first_seen = recorder.iso_utc(recorder.utc_now() - timedelta(hours=25))
        with service.store.connect() as connection:
            connection.execute(
                "UPDATE events SET recording_first_seen = ? WHERE event_id = ?",
                (first_seen, "pending-1"),
            )
        service.process_pending_recordings()
        row = service.store.get_row("pending-1")
        self.assertEqual(row["recording_status"], "expired")
        self.assertIn("24 hours", row["recording_error"])
        self.assertEqual(service._download.call_count, 1)

        self.add_event(service.store, "download-failed", "2026-11-15T13:00:00Z")
        with service.store.connect() as connection:
            connection.execute(
                """
                UPDATE events SET recording_first_seen = ?, recording_error = 'HTTPError'
                WHERE event_id = 'download-failed'
                """,
                (first_seen,),
            )
        service.store.mark_recording_expired()
        self.assertEqual(
            service.store.get_row("download-failed")["recording_status"], "pending"
        )

    def test_history_stops_paging_at_first_known_event(self):
        service = self.make_recorder()
        device = {
            "id": "device-1",
            "name": "Front Door",
            "slug": "front-door",
            "kind": "doorbell",
        }
        self.add_event(service.store, "archived", "2026-11-15T13:00:00Z", device)
        service.store.mark_downloaded("archived", "front-door/archived.mp4", 10)
        service.store.add_discovered_event(
            {
                "id": "pending",
                "created_at": "2026-11-15T12:00:00Z",
                "kind": "motion",
                "recording": {"status": "processing"},
            },
            device,
            disabled=False,
        )
        service.ring = Mock()
        service.ring.history.side_effect = [
            [
                {
                    "id": "archived",
                    "created_at": "2026-11-15T13:00:00Z",
                    "kind": "motion",
                    "recording": {"status": "ready"},
                },
                {
                    "id": "older-unknown",
                    "created_at": "2026-11-15T12:30:00Z",
                    "kind": "motion",
                    "recording": {"status": "ready"},
                },
            ],
            [
                {
                    "id": "pending",
                    "created_at": "2026-11-15T12:00:00Z",
                    "kind": "motion",
                    "recording": {"status": "ready"},
                }
            ],
        ]
        with patch.object(recorder.time, "sleep"):
            service.history_events(device)
        # The first known event ends the scan: no second page, nothing older added.
        self.assertEqual(service.ring.history.call_count, 1)
        self.assertFalse(service.store.event_exists("older-unknown"))

    def test_range_parsing(self):
        self.assertEqual(recorder.parse_range_header("bytes=0-", 1000), (0, 999))
        self.assertEqual(recorder.parse_range_header("bytes=100-199", 1000), (100, 199))
        self.assertEqual(recorder.parse_range_header("bytes=-500", 1000), (500, 999))
        for invalid in ("items=0-10", "bytes=", "bytes=200-100", "bytes=1000-", "bytes=0-1,3-4"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                recorder.parse_range_header(invalid, 1000)

    def test_clip_lookup_cannot_traverse_outside_clip_root(self):
        store = recorder.EventStore(self.config)
        self.add_event(store, "escape", "2026-11-15T12:00:00Z")
        outside = self.config.storage_dir / "outside.mp4"
        outside.write_bytes(b"video")
        with store.connect() as connection:
            connection.execute(
                "UPDATE events SET clip_path = '../outside.mp4' WHERE event_id = 'escape'"
            )
        self.assertIsNone(store.clip_for_event("escape"))
        self.assertTrue(outside.exists())

    def test_retention_prunes_old_rows_and_files_only(self):
        self.config.retention_days = 10
        store = recorder.EventStore(self.config)
        now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        self.add_event(store, "old", recorder.iso_utc(now - timedelta(days=11)))
        self.add_event(store, "current", recorder.iso_utc(now - timedelta(days=9)))
        old_relative = Path("front-door/2026-11-05/old.mp4")
        current_relative = Path("front-door/2026-11-07/current.mp4")
        for relative in (old_relative, current_relative):
            path = self.config.clips_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"clip")
        store.mark_downloaded("old", old_relative.as_posix(), 4)
        store.mark_downloaded("current", current_relative.as_posix(), 4)

        self.assertEqual(store.prune(now), 1)
        self.assertIsNone(store.get_row("old"))
        self.assertFalse((self.config.clips_dir / old_relative).exists())
        self.assertIsNotNone(store.get_row("current"))
        self.assertTrue((self.config.clips_dir / current_relative).exists())

    def test_gemini_inline_request_uses_structured_json_shape(self):
        self.config.gemini_api_key = "gemini-key"
        client = recorder.GeminiClient(self.config)
        clip = self.config.storage_dir / "small.mp4"
        clip.write_bytes(b"small video")
        answer = {
            "summary": "A person walks past.",
            "detail": "The person stays on the pavement. They do not approach the door.",
            "people": 1,
            "vehicles": 0,
            "animals": [],
            "packages": False,
            "activity_tags": ["passer-by"],
            "notable": False,
            "confidence": 0.9,
        }
        requests = []

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return response_json({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]})

        event = {
            "device_name": "Front Door",
            "device": "front-door",
            "local_time": "2026-11-16T12:00:00+00:00",
            "kind": "motion",
        }
        with patch.object(recorder, "urlopen", side_effect=fake_urlopen):
            result = client.analyse_clip(clip, event)
        self.assertEqual(result, answer)
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0].data)
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(body["generationConfig"]["responseSchema"], recorder.GeminiClient.RESPONSE_SCHEMA)
        parts = body["contents"][0]["parts"]
        inline = next(part["inline_data"] for part in parts if "inline_data" in part)
        self.assertEqual(inline["mime_type"], "video/mp4")
        self.assertNotIn("file_data", inline)

    def test_gemini_large_clip_uses_files_api_and_deletes_upload(self):
        self.config.gemini_api_key = "gemini-key"
        client = recorder.GeminiClient(self.config)
        clip = self.config.storage_dir / "large.mp4"
        with clip.open("wb") as stream:
            stream.truncate(recorder.INLINE_CLIP_BYTES)
        answer = {
            "summary": "Nothing happens.",
            "detail": "The scene is empty. No movement is visible.",
            "people": 0,
            "vehicles": 0,
            "animals": [],
            "packages": False,
            "activity_tags": ["nothing"],
            "notable": False,
            "confidence": 0.8,
        }
        requests = []
        responses = iter([
            response_json({}, {"X-Goog-Upload-URL": "https://upload.example/session"}),
            response_json({"file": {"name": "files/test-file"}}),
            response_json({"name": "files/test-file", "state": "ACTIVE", "uri": "https://files.example/test"}),
            response_json({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]}),
            FakeResponse(),
        ])

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return next(responses)

        event = {
            "device_name": "Hallway",
            "device": "hallway",
            "local_time": "2026-11-16T12:00:00+00:00",
            "kind": "motion",
        }
        with patch.object(recorder, "urlopen", side_effect=fake_urlopen):
            result = client.analyse_clip(clip, event)
        self.assertEqual(result, answer)
        self.assertEqual(len(requests), 5)
        generate = requests[3]
        body = json.loads(generate.data)
        file_part = next(
            part["file_data"] for part in body["contents"][0]["parts"] if "file_data" in part
        )
        self.assertEqual(file_part["file_uri"], "https://files.example/test")
        self.assertEqual(requests[-1].get_method(), "DELETE")
        self.assertTrue(requests[-1].full_url.endswith("/v1beta/files/test-file"))


    def test_post_auth_missing_wrong_right_and_disabled(self):
        self.config.api_token = "correct-token"
        service = self.make_recorder()
        handler = object.__new__(recorder.RequestHandler)
        handler.server = SimpleNamespace(recorder=service)
        handler.headers = {}
        handler._error = Mock()
        handler._send_json = Mock()
        self.assertFalse(handler._authorized())
        handler._send_json.assert_called_once_with(
            recorder.HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}
        )

        handler._send_json.reset_mock()
        handler.headers = {"Authorization": "Bearer wrong-token"}
        self.assertFalse(handler._authorized())
        handler._send_json.assert_called_once_with(
            recorder.HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}
        )

        handler._send_json.reset_mock()
        handler.headers = {"Authorization": "Bearer correct-token"}
        self.assertTrue(handler._authorized())
        handler._send_json.assert_not_called()

        self.config.api_token = ""
        self.assertFalse(handler._authorized())
        handler._error.assert_called_once_with(
            recorder.HTTPStatus.SERVICE_UNAVAILABLE, "POST API is disabled"
        )

    def test_analyze_rejects_more_than_eight_ids_and_query_caps_at_eight(self):
        self.config.api_token = "correct-token"
        self.config.gemini_api_key = "gemini-key"
        service = self.make_recorder()
        for number in range(10):
            event_id = f"event-{number}"
            created = datetime(2026, 11, 16, 12, tzinfo=timezone.utc) + timedelta(minutes=number)
            self.add_event(service.store, event_id, recorder.iso_utc(created))
            relative = Path("front-door/2026-11-16") / f"{event_id}.mp4"
            path = self.config.clips_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"clip")
            service.store.mark_downloaded(event_id, relative.as_posix(), 4)
        service.gemini.answer_question = Mock(return_value="Eight clips checked.")
        handler = object.__new__(recorder.RequestHandler)
        handler.server = SimpleNamespace(recorder=service)
        handler._error = Mock()
        handler._send_json = Mock()

        selected = handler._select_analyze_rows(
            {"event_ids": [f"event-{number}" for number in range(9)]}
        )
        self.assertIsNone(selected)
        handler._error.assert_called_once_with(
            recorder.HTTPStatus.BAD_REQUEST, "event_ids must contain 1 to 8 values"
        )
        self.assertFalse(service.gemini.answer_question.called)

        handler._error.reset_mock()
        handler._analyze({"question": "What happened?"})
        handler._error.assert_not_called()
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, recorder.HTTPStatus.OK)
        self.assertEqual(len(body["event_ids"]), recorder.MAX_ANALYZE_CLIPS)
        clips = service.gemini.answer_question.call_args.args[0]
        self.assertEqual(len(clips), recorder.MAX_ANALYZE_CLIPS)
        self.assertEqual(body["event_ids"][0], "event-9")


if __name__ == "__main__":
    unittest.main()


class GeminiFallbackTests(RecorderTestCase):
    # Only the tests below; the base class tests run once on their own.

    def test_busy_model_falls_through_to_next(self):
        self.config.gemini_api_key = "k"
        self.config.gemini_model = "busy-model, good-model"
        client = recorder.GeminiClient(self.config)
        calls = []

        def fake(url, **_kwargs):
            calls.append(url)
            if "busy-model" in url:
                raise recorder.GeminiError("HTTP 503", transient=True)
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}, {}

        with patch.object(client, "_request_json", side_effect=fake):
            self.assertEqual(client._generate([], {}), "ok")
        self.assertEqual(len(calls), 2)

    def test_permanent_error_does_not_fall_through(self):
        self.config.gemini_api_key = "k"
        self.config.gemini_model = "a,b"
        client = recorder.GeminiClient(self.config)
        with patch.object(client, "_request_json",
                          side_effect=recorder.GeminiError("HTTP 400")) as request:
            with self.assertRaises(recorder.GeminiError):
                client._generate([], {})
        self.assertEqual(request.call_count, 1)

    def test_transient_error_keeps_clip_pending(self):
        rec = recorder.Recorder(self.config)
        row = {"analysis_status": "pending", "device_slug": "driveway", "event_id": "1",
               "created_at": "2026-11-16T19:00:00+00:00", "alerted": 0}
        with patch.object(rec.store, "get_row", return_value=row), patch.object(
                rec.store, "clip_for_event", return_value=self.config.clips_dir / "x.mp4"), patch.object(
                rec.store, "row_to_api", return_value={}), patch.object(
                rec, "deep_context", return_value=""), patch.object(
                rec.gemini, "analyse_clip",
                side_effect=recorder.GeminiError("429", transient=True)), patch.object(
                rec.store, "mark_analysis") as mark:
            self.config.gemini_api_key = "k"
            self.assertEqual(rec.analyse_event("1"), "backoff")
        mark.assert_not_called()


class ExcludedDeviceTests(unittest.TestCase):
    def test_excluded_device_never_analysed(self):
        with TemporaryDirectory() as tmp:
            config = recorder.Config()
            config.storage_dir = Path(tmp)
            config.gemini_api_key = "k"
            config.gemini_model = "m"
            config.gemini_exclude = {"hallway"}
            rec = recorder.Recorder(config)
            row = {"analysis_status": "pending", "device_slug": "hallway"}
            with patch.object(rec.store, "get_row", return_value=row), patch.object(
                    rec.store, "clip_for_event", return_value=Path(tmp) / "x.mp4"), patch.object(
                    rec.gemini, "analyse_clip") as analyse, patch.object(
                    rec.store, "mark_analysis") as mark:
                self.assertIsNone(rec.analyse_event("1"))
            analyse.assert_not_called()
            mark.assert_called_once_with("1", "excluded", None)


class PublicHostAuthTests(RecorderTestCase):
    def setUp(self):
        super().setUp()
        import http.client
        import threading

        self.http = http.client
        self.config.auth_hosts = {"hass.example.org"}
        self.config.ha_url = "http://ha.invalid:8123"
        self.config.card_path.write_text("// card")
        rec = self.make_recorder()
        self.server = recorder.RingHTTPServer(("127.0.0.1", 0), rec)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, host, headers=None):
        connection = self.http.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request(method, path, headers={"Host": host, **(headers or {})})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_lan_host_stays_open(self):
        response, _ = self.request("GET", "/api/events", "hass.iacob.uk")
        self.assertEqual(response.status, 200)

    def test_public_host_requires_session_but_serves_card(self):
        response, _ = self.request("GET", "/api/events", "hass.example.org")
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/clips/abc.mp4", "hass.example.org:443")
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/", "hass.example.org")
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/card.js", "hass.example.org")
        self.assertEqual(response.status, 200)

    def test_session_needs_valid_home_assistant_token(self):
        with patch.object(recorder, "validate_ha_token", return_value="rejected") as check:
            response, _ = self.request("POST", "/api/session", "hass.example.org", {"Authorization": "Bearer bad"})
        self.assertEqual(response.status, 401)
        check.assert_called_once_with("http://ha.invalid:8123", "bad")
        self.assertIsNone(response.getheader("Set-Cookie"))

    def test_session_cookie_unlocks_public_host(self):
        with patch.object(recorder, "validate_ha_token", return_value="ok"):
            response, _ = self.request("POST", "/api/session", "hass.example.org", {"Authorization": "Bearer good"})
        self.assertEqual(response.status, 200)
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=Strict", cookie)
        value = cookie.split(";", 1)[0]
        response, _ = self.request("GET", "/api/events", "hass.example.org", {"Cookie": value})
        self.assertEqual(response.status, 200)
        response, _ = self.request("GET", "/api/events", "hass.example.org", {"Cookie": "rr_session=forged"})
        self.assertEqual(response.status, 401)

    def test_validate_ha_token_rejects_without_config_or_on_http_error(self):
        self.assertEqual(recorder.validate_ha_token("", "token"), "rejected")
        self.assertEqual(recorder.validate_ha_token("http://ha", ""), "rejected")
        error = recorder.HTTPError("http://ha/api/", 401, "Unauthorized", {}, None)
        with patch.object(recorder, "urlopen", side_effect=error):
            self.assertEqual(recorder.validate_ha_token("http://ha", "token"), "rejected")
        busy = recorder.HTTPError("http://ha/api/", 502, "Bad Gateway", {}, None)
        with patch.object(recorder, "urlopen", side_effect=busy):
            self.assertEqual(recorder.validate_ha_token("http://ha", "token"), "error")
        with patch.object(recorder, "urlopen", return_value=FakeResponse(b"{}", status=200)):
            self.assertEqual(recorder.validate_ha_token("http://ha", "token"), "ok")

    def test_failed_logins_stop_reaching_home_assistant(self):
        with patch.object(recorder, "validate_ha_token", return_value="rejected") as check:
            for _ in range(recorder.LOGIN_FAILURE_LIMIT):
                self.request("POST", "/api/session", "hass.example.org", {"Authorization": "Bearer bad"})
            response, _ = self.request("POST", "/api/session", "hass.example.org", {"Authorization": "Bearer bad"})
        self.assertEqual(response.status, 429)
        self.assertEqual(check.call_count, recorder.LOGIN_FAILURE_LIMIT)

    def test_login_limit_is_per_client_and_ignores_ha_outages(self):
        with patch.object(recorder, "validate_ha_token", return_value="rejected"):
            for _ in range(recorder.LOGIN_FAILURE_LIMIT):
                self.request("POST", "/api/session", "hass.example.org", {"Authorization": "Bearer bad", "CF-Connecting-IP": "203.0.113.9"})
        with patch.object(recorder, "validate_ha_token", return_value="ok"):
            response, _ = self.request("POST", "/api/session", "hass.example.org", {"Authorization": "Bearer good", "CF-Connecting-IP": "198.51.100.4"})
        self.assertEqual(response.status, 200)
        with patch.object(recorder, "validate_ha_token", return_value="error"):
            for _ in range(recorder.LOGIN_FAILURE_LIMIT + 1):
                response, _ = self.request("POST", "/api/session", "hass.example.org", {"Authorization": "Bearer x", "CF-Connecting-IP": "198.51.100.5"})
        self.assertEqual(response.status, 502)

    def test_public_post_needs_session_even_with_api_token(self):
        self.server.recorder.config.api_token = "secret"
        response, _ = self.request("POST", "/api/analyze", "hass.example.org", {"Authorization": "Bearer secret", "Content-Type": "application/json"})
        self.assertEqual(response.status, 401)


class Fixture(unittest.TestCase):
    """RecorderTestCase's setup and helpers without re-running its tests."""

    setUp = RecorderTestCase.setUp
    make_recorder = RecorderTestCase.make_recorder
    add_event = RecorderTestCase.add_event


class QuickLookTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.config.gemini_api_key = "k"
        self.config.gemini_exclude = {"hallway"}
        self.config.api_token = "secret"
        self.config.notify_base_url = "https://hass.example.org/ring-events"
        self.config.ha_webhook_url = "http://ha.invalid/api/webhook/x"
        self.rec = self.make_recorder()
        self.store = self.rec.store

    def downloaded(self, event_id, created, device=None):
        self.add_event(self.store, event_id, created, device)
        slug = (device or {}).get("slug", "front-door")
        clip = self.config.clips_dir / slug / f"{event_id}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"mp4")
        self.store.mark_downloaded(event_id, f"{slug}/{event_id}.mp4", 3)

    def run_quick(self, event_id, quick):
        with patch.object(recorder, "extract_frame", return_value=b"jpg"), patch.object(
                self.rec.gemini, "quick_look", return_value=quick) as look, patch.object(
                self.rec, "notify") as notify:
            self.rec.quick_event(event_id)
        return look, notify

    def test_ring_detection_offset_comes_from_verified_timestamp(self):
        created = datetime(2026, 11, 16, 19, 37, 10, 894000, tzinfo=timezone.utc)
        event = {"cv_properties": {"detection_type": "human", "detection_types": [
            {"detection_type": "human", "verified_timestamps": [int(created.timestamp() * 1000) + 4577]}]}}
        detection, hero = recorder.ring_detection(event, created)
        self.assertEqual(detection, "human")
        self.assertAlmostEqual(hero, 4.577, places=2)
        self.assertEqual(recorder.ring_detection({}, created), (None, None))

    def test_full_pass_waits_for_quick_look(self):
        now = recorder.iso_utc()
        self.downloaded("1", now)
        self.assertEqual(self.store.pending_analyses(), [])
        self.assertEqual(self.store.pending_quick(), ["1"])

    def test_fresh_person_at_doorbell_alerts_and_queues_full_pass(self):
        self.downloaded("1", recorder.iso_utc())
        quick = {"headline": "Man walking up to the door", "label": "Person at door",
                 "people": ["man in dark coat"], "vehicles": 0, "notable": False, "nothing_there": False}
        _, notify = self.run_quick("1", quick)
        row = self.store.get_row("1")
        self.assertEqual(row["quick_status"], "ok")
        self.assertEqual(row["analysis_status"], "pending")
        self.assertEqual(row["alerted"], 1)
        self.assertEqual(self.store.pending_analyses(), ["1"])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[1:3], ("quick", "Person at door"))

    def test_empty_quick_look_still_gets_full_pass_but_no_alert(self):
        device = {"id": "d3", "name": "Driveway", "slug": "driveway", "kind": "cam"}
        self.downloaded("1", recorder.iso_utc(), device)
        quick = {"headline": "Empty path", "label": "Nothing", "people": [], "vehicles": 0,
                 "notable": False, "nothing_there": True}
        _, notify = self.run_quick("1", quick)
        self.assertEqual(self.store.pending_analyses(), ["1"])
        notify.assert_not_called()

    def test_busy_model_is_skipped_on_the_next_call(self):
        self.config.gemini_model = "busy,good"
        client = recorder.GeminiClient(self.config)
        calls = []

        def fake(url, **_kwargs):
            calls.append(url)
            if "busy" in url:
                raise recorder.GeminiError("429", transient=True)
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}, {}

        with patch.object(client, "_request_json", side_effect=fake):
            client._generate([], {})
            client._generate([], {})
        self.assertEqual([("busy" in url) for url in calls], [True, False, False])

    def test_webhook_payload_carries_camera_tag_and_signed_image(self):
        self.downloaded("1", recorder.iso_utc())
        sent = []
        with patch.object(recorder, "urlopen", side_effect=lambda request, timeout: sent.append(
                json.loads(request.data)) or FakeResponse()):
            self.rec.notify(self.store.get_row("1"), "quick", "Person at door", "Man at door", False)
        self.assertEqual(sent[0]["tag"], "ring-front-door")
        self.assertTrue(sent[0]["image"].startswith("https://hass.example.org/ring-events/thumbs/1.jpg?exp="))

    def test_backlog_gets_quick_look_only_and_never_alerts(self):
        old = recorder.iso_utc(recorder.utc_now() - timedelta(days=5))
        self.downloaded("1", old)
        quick = {"headline": "Courier at the door", "label": "Delivery", "people": ["courier"],
                 "vehicles": 0, "notable": False, "nothing_there": False}
        _, notify = self.run_quick("1", quick)
        row = self.store.get_row("1")
        self.assertEqual(row["analysis_status"], "ok")
        self.assertEqual(json.loads(row["analysis"])["source"], "quick")
        notify.assert_not_called()

    def test_indoor_camera_skips_quick_look(self):
        device = {"id": "d2", "name": "Hallway", "slug": "hallway", "kind": "cam"}
        self.downloaded("1", recorder.iso_utc(), device)
        look, _ = self.run_quick("1", {})
        look.assert_not_called()
        self.assertEqual(self.store.get_row("1")["quick_status"], "skipped")

    def test_transient_failure_on_fresh_event_falls_through_to_full_pass(self):
        self.downloaded("1", recorder.iso_utc())
        with patch.object(recorder, "extract_frame", return_value=b"jpg"), patch.object(
                self.rec.gemini, "quick_look", side_effect=recorder.GeminiError("429", transient=True)):
            self.assertIsNone(self.rec.quick_event("1"))
        self.assertEqual(self.store.get_row("1")["quick_status"], "failed")
        self.assertEqual(self.store.pending_analyses(), ["1"])

    def test_transient_failure_on_backlog_backs_off(self):
        self.downloaded("1", recorder.iso_utc(recorder.utc_now() - timedelta(days=5)))
        with patch.object(recorder, "extract_frame", return_value=b"jpg"), patch.object(
                self.rec.gemini, "quick_look", side_effect=recorder.GeminiError("429", transient=True)):
            self.assertEqual(self.rec.quick_event("1"), "backoff")
        self.assertIsNone(self.store.get_row("1")["quick_status"])

    def test_deep_context_lists_neighbours_and_presence(self):
        base = recorder.utc_now()
        self.downloaded("1", recorder.iso_utc(base))
        self.add_event(self.store, "2", recorder.iso_utc(base + timedelta(seconds=40)),
                       {"id": "d2", "name": "Hallway", "slug": "hallway", "kind": "cam"})
        self.store.add_presence("andrei", "home", base - timedelta(minutes=5))
        context = self.rec.deep_context(self.store.get_row("1"))
        self.assertIn("Hallway: motion (indoor camera, not analysed)", context)
        self.assertIn("andrei's phone reported home", context)

    def test_poll_checks_indoor_cameras_only_on_the_slow_cadence(self):
        devices = [{"id": "a", "name": "Front Door", "slug": "front-door", "kind": "doorbell"},
                   {"id": "b", "name": "Hallway", "slug": "hallway", "kind": "cam"}]
        polled = []
        with patch.object(self.rec.ring, "devices", return_value=devices) as listed, patch.object(
                self.rec, "history_events", side_effect=lambda d: polled.append(d["slug"])), patch.object(
                self.rec, "process_pending_recordings"):
            self.rec.poll_once()
            self.rec.poll_once()
        self.assertEqual(polled, ["front-door", "hallway", "front-door"])
        self.assertEqual(listed.call_count, 1)


class ThumbAndPresenceHttpTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        import http.client
        import threading

        self.http = http.client
        self.config.auth_hosts = {"hass.example.org"}
        self.server = recorder.RingHTTPServer(("127.0.0.1", 0), self.make_recorder())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        rec = self.server.recorder
        rec.config.api_token = "secret"
        self.add_event(rec.store, "1", recorder.iso_utc())
        rec.store.mark_downloaded("1", "front-door/1.mp4", 3)
        thumb = rec.store.thumb_for_event("1")
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"jpeg")

    request = PublicHostAuthTests.request

    def test_signed_thumb_link_works_without_session_and_expires(self):
        expires = int(recorder.time.time()) + 60
        sig = recorder.sign_thumb("secret", "1", expires)
        response, body = self.request("GET", f"/thumbs/1.jpg?exp={expires}&sig={sig}", "hass.example.org")
        self.assertEqual((response.status, body), (200, b"jpeg"))
        bad, _ = self.request("GET", f"/thumbs/1.jpg?exp={expires}&sig={'0' * 64}", "hass.example.org")
        self.assertEqual(bad.status, 401)
        past = int(recorder.time.time()) - 1
        stale, _ = self.request(
            "GET", f"/thumbs/1.jpg?exp={past}&sig={recorder.sign_thumb('secret', '1', past)}", "hass.example.org")
        self.assertEqual(stale.status, 401)
        other, _ = self.request("GET", f"/thumbs/2.jpg?exp={expires}&sig={sig}", "hass.example.org")
        self.assertEqual(other.status, 401)

    def test_presence_needs_token_and_is_stored(self):
        body = json.dumps({"person": "andrei", "state": "home"})
        connection = self.http.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST", "/api/presence", body=body, headers={
            "Host": "hass.iacob.uk", "Content-Type": "application/json"})
        self.assertEqual(connection.getresponse().status, 401)
        connection.close()
        connection = self.http.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST", "/api/presence", body=body, headers={
            "Host": "hass.iacob.uk", "Content-Type": "application/json", "Authorization": "Bearer secret"})
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()
        connection = self.http.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST", "/api/presence", body=json.dumps({"person": "cristina", "state": "home"}),
                           headers={"Host": "hass.example.org", "Content-Type": "application/json",
                                    "Authorization": "Bearer secret"})
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()
        rows = self.server.recorder.store.presence_between(
            recorder.utc_now() - timedelta(minutes=1), recorder.utc_now() + timedelta(minutes=1))
        self.assertEqual([(r["person"], r["state"]) for r in rows], [("andrei", "home"), ("cristina", "home")])


class ReviewFixTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.config.gemini_api_key = "k"
        self.rec = self.make_recorder()
        self.store = self.rec.store

    def test_old_event_quick_failure_never_queues_full_pass(self):
        self.add_event(self.store, "1", recorder.iso_utc(recorder.utc_now() - timedelta(days=5)))
        clip = self.config.clips_dir / "front-door" / "1.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"x")
        self.store.mark_downloaded("1", "front-door/1.mp4", 1)
        with patch.object(recorder, "extract_frame", side_effect=ValueError("no frame")):
            self.rec.quick_event("1")
        row = self.store.get_row("1")
        self.assertEqual((row["quick_status"], row["analysis_status"]), ("failed", "failed"))
        self.assertTrue(self.rec.analysis_queue.empty())

    def test_all_models_cooling_raises_transient(self):
        self.config.gemini_model = "a"
        client = recorder.GeminiClient(self.config)
        client._cooldown["a"] = recorder.time.monotonic() + 60
        with self.assertRaises(recorder.GeminiError) as caught:
            client._generate([], {})
        self.assertTrue(caught.exception.transient)

    def test_one_camera_failure_does_not_stop_the_others_or_downloads(self):
        devices = [{"id": "a", "name": "A", "slug": "front-door", "kind": "d"},
                   {"id": "b", "name": "B", "slug": "garden", "kind": "d"}]
        seen = []

        def history(device):
            seen.append(device["slug"])
            if device["slug"] == "front-door":
                raise recorder.URLError("down")

        with patch.object(self.rec.ring, "devices", return_value=devices), patch.object(
                self.rec, "history_events", side_effect=history), patch.object(
                self.rec, "process_pending_recordings") as pending:
            with self.assertRaises(ValueError):
                self.rec.poll_once()
        self.assertEqual(seen, ["front-door", "garden"])
        pending.assert_called_once()

    def test_cached_devices_survive_a_failed_refresh(self):
        devices = [{"id": "a", "name": "A", "slug": "front-door", "kind": "d"}]
        with patch.object(self.rec, "history_events"), patch.object(self.rec, "process_pending_recordings"):
            with patch.object(self.rec.ring, "devices", return_value=devices):
                self.rec.poll_once()
            self.rec._last_full_poll = 0
            with patch.object(self.rec.ring, "devices", side_effect=recorder.URLError("down")):
                self.rec.poll_once()
        self.assertEqual(self.rec._devices, devices)
