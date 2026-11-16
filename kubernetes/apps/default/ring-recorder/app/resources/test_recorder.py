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

    def test_history_scans_past_archive_to_refresh_known_pending_event(self):
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
        self.assertEqual(service.ring.history.call_count, 2)
        self.assertEqual(service.store.get_row("pending")["recording_ready"], 1)
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
        row = {"analysis_status": "pending"}
        with patch.object(rec.store, "get_row", return_value=row), patch.object(
                rec.store, "clip_for_event", return_value=self.config.clips_dir / "x.mp4"), patch.object(
                rec.store, "row_to_api", return_value={}), patch.object(
                rec.gemini, "analyse_clip",
                side_effect=recorder.GeminiError("429", transient=True)), patch.object(
                rec.store, "mark_analysis") as mark:
            self.config.gemini_api_key = "k"
            self.assertEqual(rec.analyse_event("1"), "backoff")
        mark.assert_not_called()
