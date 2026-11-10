import importlib.util
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).with_name('home-monitoring-bridge.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def alert(name='DiskFull', active='2026-11-10T12:00:00Z'):
    return {'labels': {'alertname': name, 'severity': 'warning', 'instance': 'server:9100',
                       'private_url': 'https://secret/token'},
            'annotations': {'description': 'password=secret'}, 'state': 'firing', 'activeAt': active}


def document(*alerts):
    return {'status': 'success', 'data': {'alerts': list(alerts)}}


class BridgeTests(unittest.TestCase):
    def test_duplicate_and_restart_then_recovery(self):
        current = b.snapshot(document(alert()))
        state, events = b.transition({'alerts': {}}, current, 1000)
        self.assertEqual([e['status'] for e in events if e['event_type']=='alert_transition'], ['firing'])
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'state.json'
            b.save(p, state)
            restored = json.loads(p.read_text())
        same, events = b.transition(restored, current, 1060)
        self.assertEqual(events, [])
        _, events = b.transition(same, {}, 1120)
        self.assertEqual(events[0]['status'], 'resolved')

    def test_fetch_failure_keeps_alerts_and_health_is_deduplicated(self):
        current = b.snapshot(document(alert()))
        state, _ = b.transition({'alerts': {}}, current, 1000)
        failed, events = b.transition(state, None, 1060, failed=True)
        self.assertEqual(failed['alerts'], current)
        self.assertEqual([e['status'] for e in events], ['unavailable'])
        failed2, events = b.transition(failed, None, 1120, failed=True)
        self.assertEqual(events, [])
        _, events = b.transition(failed2, current, 1180)
        self.assertEqual([e['status'] for e in events], ['recovered'])

    def test_partial_or_malformed_snapshots_rejected(self):
        for bad in [document(alert(), {}), {'status':'success','data':{}},
                    document(alert()) | {'warnings':['truncated']},
                    {'status':'error','data':{'alerts':[]}}]:
            with self.assertRaises(ValueError): b.snapshot(bad)
        # No truncation to the configured cap that could resolve omitted alerts.
        with patch.object(b, 'MAX_ALERTS', 1):
            with self.assertRaises(ValueError): b.snapshot(document(alert(), alert('Other')))

    def test_payload_minimizes_labels_and_omits_annotations(self):
        current = b.snapshot(document(alert(), alert('Watchdog')))
        self.assertEqual(len(current), 1)
        self.assertNotIn('secret', json.dumps(current))
        self.assertNotIn('private_url', json.dumps(current))

    def test_new_occurrence_same_labels_has_new_identity(self):
        old = b.snapshot(document(alert()))
        new = b.snapshot(document(alert(active='2026-11-10T13:00:00Z')))
        _, events = b.transition({'alerts':old, 'healthy':True, 'heartbeat':1000}, new, 1060)
        self.assertEqual({e['status'] for e in events}, {'resolved','firing'})
        self.assertNotEqual(events[0]['alert_id'], events[1]['alert_id'])

    def test_oversize_response_rejected(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b' ' * limit
        with patch.object(b.urllib.request, 'urlopen', return_value=Response()):
            with self.assertRaises(ValueError): b.fetch('http://localhost')

    def test_log_event_is_single_line_json(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'events.log'
            b.append(p, [{'event_type':'alert_transition','alertname':'bad\nline'}], 1000)
            lines = p.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0].split(': ',1)[1])['home_monitoring']['scope'], 'operations')


if __name__ == '__main__': unittest.main()
