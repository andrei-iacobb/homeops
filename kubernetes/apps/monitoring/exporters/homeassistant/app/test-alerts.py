"""Alert lifecycle tests: python3 test-alerts.py."""
import json
import io
import urllib.error
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from datetime import datetime, timezone

import bridge


def alert(name="DiskFailure", severity="critical", status="active"):
    return {"labels": {"alertname": name, "severity": severity, "host": "dl380"},
            "annotations": {"summary": "Replace disk"}, "startsAt": "2026-10-28T21:00:00Z",
            "status": {"state": status, "silencedBy": [], "inhibitedBy": []}}


def watchdog(expires=2000):
    row = alert("Watchdog", "none")
    row["endsAt"] = datetime.fromtimestamp(expires, timezone.utc).isoformat()
    return row


class Source:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self):
        return None if self.rows is None else bridge.parse_alerts(json.dumps(self.rows))


class HA:
    def __init__(self):
        self.sent = []
        self.published = []
        self.send_ok = True

    def notify_alert(self, data, resolved=False):
        if self.send_ok:
            self.sent.append(data)
        return self.send_ok

    def publish(self, topic, payload, retain, **kw):
        self.published.append((topic, payload, retain))
        return True

    def state(self):
        data = json.loads(next(p for t, p, r in reversed(self.published) if t.endswith("/attributes")))
        data["count"] = int(next(p for t, p, r in reversed(self.published) if t.endswith("/state")))
        return data


class AlertLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "alerts.json"
        self.source, self.ha = Source([alert()]), HA()
        self.now = 1000
        self.b = bridge.AlertBridge(self.source, self.ha, self.path, clock=lambda: self.now)

    def test_restart_dedup_and_recovery(self):
        self.b.cycle()
        self.assertEqual(len(self.ha.sent), 1)
        restarted = bridge.AlertBridge(self.source, self.ha, self.path, clock=lambda: self.now)
        restarted.cycle()
        self.assertEqual(len(self.ha.sent), 1)
        self.source.rows = []
        restarted.cycle()
        self.assertEqual(len(self.ha.sent), 2)
        self.assertIn("1 resolved", self.ha.sent[-1]["title"])
        self.assertEqual(self.ha.state()["history"][0]["status"], "resolved")
        restarted.cycle()
        self.assertEqual(len(self.ha.sent), 2)

    def test_failure_is_not_recovery_and_send_retries(self):
        self.ha.send_ok = False
        self.b.cycle()
        self.assertEqual(self.ha.state()["pending_notifications"], 1)
        self.assertEqual(len(self.b.pending), 1)
        self.ha.send_ok = True
        self.b.cycle()
        self.source.rows = None
        self.b.cycle()
        self.assertEqual(len(self.ha.sent), 1)
        self.assertEqual(len(self.b.observed), 1)
        self.assertEqual(self.ha.published[-1][1], "offline")
        self.source.rows = []
        self.ha.send_ok = False
        self.b.cycle()
        self.assertEqual(len(self.b.pending), 1)
        self.ha.send_ok = True
        self.b.cycle()
        self.assertEqual(len(self.ha.sent), 2)
        self.assertEqual(self.b.pending, [])

    def test_failed_firing_resolves_and_survives_restart_before_delivery(self):
        self.ha.send_ok = False
        self.b.cycle()
        self.assertEqual(self.b.history[0]["status"], "firing")
        self.assertEqual(len(self.b.pending), 1)
        self.source.rows = []
        self.now += 30
        self.b.cycle()
        saved = json.loads(self.path.read_text())
        self.assertEqual([e["status"] for e in saved["pending"]], ["firing", "resolved"])
        self.assertEqual([e["status"] for e in saved["history"]], ["resolved", "firing"])
        restarted = bridge.AlertBridge(self.source, self.ha, self.path, clock=lambda: self.now)
        self.ha.send_ok = True
        restarted.cycle()
        self.assertEqual(len(self.ha.sent), 1)
        self.assertIn("1 new, 1 resolved", self.ha.sent[0]["title"])
        self.assertEqual(restarted.pending, [])
        self.assertEqual(len(restarted.history), 2)
        restarted.cycle()
        self.assertEqual(len(self.ha.sent), 1)

    def test_failed_firing_survives_restart_before_disappearance(self):
        self.ha.send_ok = False
        self.b.cycle()
        self.source.rows = []
        restarted = bridge.AlertBridge(self.source, self.ha, self.path, clock=lambda: self.now)
        self.ha.send_ok = True
        restarted.cycle()
        self.assertIn("1 new, 1 resolved", self.ha.sent[0]["title"])
        self.assertEqual(len(restarted.history), 2)

    def test_full_queue_drains_then_observes_new_incident(self):
        with patch.object(bridge, "MAX_PENDING_ALERTS", 2):
            self.ha.send_ok = False
            self.b.cycle()
            self.source.rows = []
            self.b.cycle()
            self.assertEqual(len(self.b.pending), 2)
            self.ha.send_ok = True
            self.source.rows = [alert("NewFailure")]
            self.b.cycle()
            self.assertEqual(len(self.ha.sent), 1)
            self.assertEqual(self.b.pending, [])
            self.assertEqual(self.b.state_error, 1)
            self.assertEqual(self.ha.published[-1][1], "offline")
            self.b.cycle()
            self.assertEqual(len(self.ha.sent), 2)
            self.assertEqual(self.b.state_error, 0)
            self.assertEqual(self.ha.state()["count"], 1)

    def test_state_byte_limit_drains_without_mutating_refused_observation(self):
        self.ha.send_ok = False
        self.b.cycle()
        existing = self.path.stat().st_size
        self.ha.send_ok = True
        self.source.rows = [alert(), alert("NewFailure")]
        with patch.object(bridge, "MAX_ALERT_STATE_BYTES", existing + 1):
            self.b.cycle()
        self.assertEqual(len(self.ha.sent), 1)
        self.assertEqual(len(self.b.observed), 1)
        self.assertEqual(self.b.pending, [])
        self.assertEqual(self.b.state_error, 1)
        self.b.cycle()
        self.assertEqual(len(self.b.observed), 2)
        self.assertEqual(len(self.ha.sent), 2)

    def test_full_queue_never_sends_without_durable_state(self):
        with patch.object(bridge, "MAX_PENDING_ALERTS", 2):
            self.ha.send_ok = False
            self.b.cycle()
            self.source.rows = []
            self.b.cycle()
            self.source.rows = [alert("NewFailure")]
            self.ha.send_ok = True
            with patch.object(self.b, "save", side_effect=OSError("disk unavailable")):
                self.b.cycle()
            self.assertEqual(self.ha.sent, [])
            self.assertEqual(len(self.b.pending), 2)

    def test_suppression_is_not_recovery(self):
        self.b.cycle()
        for key in ("silencedBy", "inhibitedBy", "mutedBy"):
            suppressed = alert()
            suppressed["status"][key] = ["rule"]
            self.source.rows = [suppressed]
            self.b.cycle()
            self.assertEqual(len(self.ha.sent), 1)
            self.assertEqual(self.ha.state()["count"], 0)
        self.source.rows = [alert()]
        self.b.cycle()
        self.assertEqual(len(self.ha.sent), 1)

    def test_unannounced_suppressed_alert_not_paged_on_recovery(self):
        self.source.rows = [alert(status="suppressed")]
        self.b.cycle()
        self.source.rows = []
        self.b.cycle()
        self.assertEqual(self.ha.sent, [])

    def test_large_initial_batch_is_one_digest_and_stable_heartbeat(self):
        self.source.rows = [alert(f"Fault{i}", "warning") for i in range(120)]
        self.b.cycle()
        self.assertEqual(len(self.ha.sent), 1)
        self.assertIn("120 new", self.ha.sent[0]["title"])
        self.assertEqual(len(self.ha.state()["active"]), 120)
        self.assertEqual(len(self.b.history), 50)
        old_state = self.ha.state()
        attr_count = sum(t.endswith("/attributes") for t, _, _ in self.ha.published)
        self.now += 30
        self.b.cycle()
        self.assertEqual(self.ha.state(), old_state)
        self.assertEqual(sum(t.endswith("/attributes") for t, _, _ in self.ha.published), attr_count)
        self.assertEqual(len(self.ha.sent), 1)
        self.assertFalse(next(r for t, _, r in self.ha.published if t.endswith("/state")))

    def test_attributes_retry_after_rejection(self):
        original = self.ha.publish
        self.ha.publish = lambda topic, payload, retain, **kw: False if topic.endswith("/attributes") else original(topic, payload, retain, **kw)
        self.b.cycle()
        self.assertIsNone(self.b.last_payload)
        self.ha.publish = original
        self.b.cycle()
        self.assertEqual(self.ha.state()["count"], 1)
        self.assertIsNotNone(self.b.last_payload)

    def test_new_episode_realerts(self):
        self.b.cycle()
        self.source.rows[0]["startsAt"] = "2026-10-28T22:00:00Z"
        self.b.cycle()
        self.assertEqual(len(self.ha.sent), 2)
        self.assertIn("1 new, 1 resolved", self.ha.sent[-1]["title"])

    def test_unwritable_state_fails_closed(self):
        self.b.path = Path(self.tmp.name) / "directory"
        self.b.path.mkdir()
        self.b.cycle()
        self.assertEqual(self.ha.sent, [])
        self.assertEqual(self.b.state_error, 1)
        self.assertEqual(self.ha.published[-1][1], "offline")

    def test_corrupt_state_is_not_silently_reset(self):
        self.path.write_text('{"version": 1, "notified": []}')
        with self.assertRaises(ValueError):
            bridge.AlertBridge(self.source, self.ha, self.path)


class AlertParsingTest(unittest.TestCase):
    def test_noise_is_excluded(self):
        rows = [alert("Watchdog"), alert("InfoInhibitor"), alert("Ordinary", "info")]
        self.assertEqual(bridge.parse_alerts(json.dumps(rows)), {})

    def test_reject_whole_snapshot_on_malformed_row(self):
        bad = alert()
        bad["status"] = None
        for rows in ({}, [None], [alert(), bad]):
            with self.assertRaises((ValueError, TypeError)):
                bridge.parse_alerts(json.dumps(rows))

    def test_saved_history_and_messages_are_bounded(self):
        row = alert()
        row["annotations"]["description"] = "x" * 10000
        parsed = next(iter(bridge.parse_alerts(json.dumps([row])).values()))
        self.assertLess(len(parsed["message"]), 810)


class Response(io.BytesIO):
    status = 200


class Opener:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.requests = body, error, []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if self.error:
            raise self.error
        return Response(self.body)


class AlertTransportTest(unittest.TestCase):
    def test_fetch_rejects_bad_partial_and_oversized_payloads(self):
        for body in (b"not json", b"{}", b"[]" + b" " * bridge.MAX_ALERT_BYTES):
            source = bridge.Alertmanager("http://am", opener=Opener(body))
            self.assertIsNone(source.fetch())
        source = bridge.Alertmanager("http://am", opener=Opener(error=TimeoutError()))
        self.assertIsNone(source.fetch())

    def test_fetch_explicitly_includes_suppressed_alerts(self):
        opener = Opener(json.dumps([alert(status="suppressed"), watchdog()]).encode())
        source = bridge.Alertmanager("http://am", opener=opener, clock=lambda: 1000)
        self.assertTrue(next(iter(source.fetch().values()))["suppressed"])
        self.assertIn("silenced=true", opener.requests[0][0])
        self.assertIn("inhibited=true", opener.requests[0][0])
        self.assertEqual(opener.requests[0][1], 5)

    def test_watchdog_must_be_present_unexpired_and_timezone_aware(self):
        invalid = watchdog()
        invalid["endsAt"] = "2026-10-28T21:00:00"
        for rows in ([], [alert()], [watchdog(999)], [invalid]):
            with self.subTest(rows=rows):
                source = bridge.Alertmanager("http://am", opener=Opener(json.dumps(rows).encode()), clock=lambda: 1000)
                self.assertIsNone(source.fetch())
        muted = watchdog()
        muted["status"]["silencedBy"] = ["intentional"]
        source = bridge.Alertmanager("http://am", opener=Opener(json.dumps([muted]).encode()), clock=lambda: 1000)
        self.assertEqual(source.fetch(), {})

    def test_expired_producer_heartbeat_does_not_resolve_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = Opener(json.dumps([watchdog(), alert()]).encode())
            source = bridge.Alertmanager("http://am", opener=opener, clock=lambda: 1000)
            ha = HA()
            b = bridge.AlertBridge(source, ha, Path(tmp) / "alerts.json", clock=lambda: 1000)
            b.cycle()
            self.assertEqual(len(ha.sent), 1)
            opener.body = json.dumps([watchdog(999)]).encode()
            b.cycle()
            self.assertEqual(len(ha.sent), 1)
            self.assertEqual(len(b.observed), 1)
            self.assertEqual(ha.published[-1][1], "offline")

    def test_phone_uses_fixed_service_link_and_stable_tag(self):
        opener = Opener(b"[]")
        ha = bridge.HomeAssistant("http://ha", "test-only", opener=opener)
        event = {"title": "Failure", "message": "details", "id": "stable"}
        self.assertTrue(ha.notify_alert(event))
        req, timeout = opener.requests[0]
        data = json.loads(req.data)
        self.assertEqual(req.full_url, "http://ha/api/services/notify/mobile_app_andreis_iphone")
        self.assertEqual(data["data"], {"tag": "homelab-stable", "url": "/lovelace/server-alerts"})
        self.assertEqual(timeout, bridge.PUBLISH_TIMEOUT)
        failing = bridge.HomeAssistant("http://ha", "test-only", opener=Opener(error=TimeoutError()))
        self.assertFalse(failing.notify_alert(event))

    def test_phone_payload_is_bounded_for_unicode_and_escaping(self):
        opener = Opener(b"[]")
        ha = bridge.HomeAssistant("http://ha", "test-only", opener=opener)
        self.assertTrue(ha.notify_alert({"title": "Homelab alerts", "message": ("😀\n\t\"" * 2000), "id": "digest"}))
        self.assertLessEqual(len(opener.requests[0][0].data), 3500)


if __name__ == "__main__":
    unittest.main()
