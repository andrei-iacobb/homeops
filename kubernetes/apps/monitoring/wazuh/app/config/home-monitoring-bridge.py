#!/usr/bin/env python3
"""Poll complete Prometheus alert snapshots and emit operational transitions.

One writer only. State and output must live on a persistent shared volume.
Delivery is at least once: a crash after appending but before checkpointing can
repeat an event. event_id is stable for deduplication by downstream consumers.
A resolved event means absent from the next complete firing snapshot, which can
also happen when a rule is removed. It is not proof the underlying fault healed.
"""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request

MAX_RESPONSE = 4 * 1024 * 1024
MAX_ALERTS = 10000
LABELS = ('alertname', 'severity', 'instance', 'job', 'namespace', 'service', 'node', 'pod')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(document):
    if not isinstance(document, dict) or document.get('status') != 'success':
        raise ValueError('invalid_api_response')
    if document.get('warnings') or document.get('infos'):
        raise ValueError('incomplete_api_response')
    alerts = document.get('data', {}).get('alerts')
    if not isinstance(alerts, list) or len(alerts) > MAX_ALERTS:
        raise ValueError('invalid_alert_list')
    current = {}
    for alert in alerts:
        if not isinstance(alert, dict) or alert.get('state') not in ('pending', 'firing', 'inactive'):
            raise ValueError('invalid_alert')
        labels = alert.get('labels')
        active = alert.get('activeAt')
        if (not isinstance(labels, dict) or not labels.get('alertname')
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items())
                or not isinstance(active, str) or not active):
            raise ValueError('invalid_alert_identity')
        if alert['state'] != 'firing' or labels['alertname'] == 'Watchdog':
            continue
        key = digest({'labels': labels, 'activeAt': active})
        # Arbitrary annotations and labels can contain URLs, tokens and query text.
        safe = {k: ''.join(c for c in labels[k][:256] if c.isprintable())
                for k in LABELS if k in labels}
        safe['severity'] = safe.get('severity', 'unknown').lower()
        if safe['severity'] not in ('info', 'warning', 'critical'):
            safe['severity'] = 'unknown'
        current[key] = {'labels': safe, 'active_at': active[:64]}
    return current


def fetch(url):
    request = urllib.request.Request(url.rstrip('/') + '/api/v1/alerts', headers={'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError('http_error')
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError('response_too_large')
    return snapshot(json.loads(body))


def alert_event(key, entry, status):
    return {'event_type': 'alert_transition', 'status': status,
            'event_id': digest([key, status]), 'alert_id': key,
            'severity': entry['labels']['severity'], 'active_at': entry['active_at'],
            **entry['labels']}


def transition(state, current, now, failed=False):
    events = []
    updated = dict(state)
    if failed:
        if state.get('healthy') is not False:
            events.append({'event_type': 'collector_health', 'status': 'unavailable'})
        updated['healthy'] = False
        # Never compare an unsuccessful fetch with an empty alert set.
    else:
        if state.get('healthy') is False:
            events.append({'event_type': 'collector_health', 'status': 'recovered'})
        previous = state.get('alerts', {})
        events += [alert_event(k, v, 'firing') for k, v in current.items() if k not in previous]
        events += [alert_event(k, v, 'resolved') for k, v in previous.items() if k not in current]
        updated.update(alerts=current, healthy=True, last_success=now)
    if now - state.get('heartbeat', 0) >= 900:
        events.append({'event_type': 'collector_heartbeat',
                       'status': 'healthy' if updated.get('healthy') else 'unavailable',
                       'active_alerts': len(updated.get('alerts', {})),
                       'last_success': updated.get('last_success', 0)})
        updated['heartbeat'] = now
    return updated, events


def save(path, state):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(state, output, separators=(',', ':'))
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def append(path, events, now):
    if not events:
        return
    if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
        path.replace(path.with_suffix('.log.1'))
    with path.open('a') as output:
        for event in events:
            event = {'source': 'prometheus', 'scope': 'operations', 'observed_at': now, **event}
            output.write('home-monitoring: ' + json.dumps({'home_monitoring': event}, separators=(',', ':')) + '\n')
        output.flush()
        os.fsync(output.fileno())


def main():
    os.umask(0o027)
    directory = Path(os.environ.get('STATE_DIR', '/var/lib/home-monitoring'))
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint = directory / 'state.json'
    # Corrupt state must fail visibly, never silently erase unresolved history.
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {'alerts': {}}
    if not isinstance(state, dict) or not isinstance(state.get('alerts'), dict):
        raise ValueError('invalid_checkpoint')
    url = os.environ.get('PROM_URL', 'http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090')
    while True:
        now = int(time.time())
        try:
            current = fetch(url)
        except Exception as error:
            # No exception text: HTTP errors can include URLs and response bodies.
            print('Prometheus snapshot failed: ' + type(error).__name__, flush=True)
            updated, events = transition(state, None, now, failed=True)
        else:
            updated, events = transition(state, current, now)
        append(directory / 'events.log', events, now)
        save(checkpoint, updated)
        state = updated
        time.sleep(60)


if __name__ == '__main__':
    main()
