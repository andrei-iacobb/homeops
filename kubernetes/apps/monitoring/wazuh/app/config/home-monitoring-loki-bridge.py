#!/usr/bin/env python3
"""Bounded five-minute Loki summaries; never export raw log content.

Keyword matches are operational investigation leads, not intrusion detections.
Each closed bucket is processed once, with 30 seconds for ingestion. Late logs
arriving after that are not revisited. Delivery is at least once across crashes.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request

WINDOW = 300
MAX_SERIES = 95  # Leaves room for health and gap events within 100 events/minute.
MAX_RESPONSE = 512 * 1024
MAX_BACKLOG = 6 * 3600
SELECTORS = (
    '{namespace=~"network|kube-system|flux-system|monitoring",app!~"wazuh.*|alloy|loki.*"}',
    '{namespace="default",app="authentik"}',
    '{namespace=~"homeassistant|proxmox"}',
)
# RE2 word boundaries reduce matches of names such as error_count in healthy output.
PATTERN = r'(?i)(\b(error|fatal|panic|failed|failure|denied|unauthorized)\b|"response_code"\s*:\s*"?5[0-9][0-9])'


def query(matching):
    pipeline = ' |~ ' + json.dumps(PATTERN) if matching else ''
    parts = ['count_over_time(' + selector + pipeline + '[5m])' for selector in SELECTORS]
    return 'sum by (namespace, app) (' + ' or '.join(parts) + ')'


def parse(document):
    if (not isinstance(document, dict) or document.get('status') != 'success'
            or document.get('warnings') or document.get('infos')):
        raise ValueError('incomplete_response')
    data = document.get('data', {})
    rows = data.get('result')
    if data.get('resultType') != 'vector' or not isinstance(rows, list) or len(rows) > MAX_SERIES:
        raise ValueError('invalid_or_excessive_series')
    result = {}
    for row in rows:
        metric = row.get('metric', {})
        namespace, app = metric.get('namespace'), metric.get('app', '')
        if (not isinstance(namespace, str) or namespace not in ('network','kube-system','flux-system','monitoring','default','homeassistant','proxmox')
                or not isinstance(app, str) or len(app) > 128
                or (namespace == 'default' and app != 'authentik')):
            raise ValueError('unexpected_labels')
        value = float(row['value'][1])
        if not math.isfinite(value) or value < 0 or value != int(value):
            raise ValueError('invalid_count')
        key = (namespace, app)
        if key in result:
            raise ValueError('duplicate_series')
        result[key] = int(value)
    return result


def fetch(url, expression, end):
    # Request one extra series so a result limit cannot silently clip at our cap.
    params = urllib.parse.urlencode({'query': expression, 'time': str(end), 'limit': MAX_SERIES + 1})
    request = urllib.request.Request(url.rstrip('/') + '/loki/api/v1/query?' + params)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError('http_error')
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError('oversized_response')
    return parse(json.loads(body))


def next_bucket(state, now):
    latest = ((int(now) - 30) // WINDOW) * WINDOW
    previous = state.get('last_bucket')
    if previous is None:
        return latest, None
    if previous >= latest:
        return None, None
    candidate = previous + WINDOW
    if latest - candidate > MAX_BACKLOG:
        return latest, {'event_type': 'collection_gap', 'status': 'history_skipped',
                        'from_bucket': candidate, 'to_bucket': latest - WINDOW,
                        'skipped_buckets': (latest - candidate) // WINDOW}
    return candidate, None


def summarize(matches, totals, end):
    if len(totals) > MAX_SERIES or len(matches) > MAX_SERIES:
        raise ValueError('excessive_series')
    events = []
    for (namespace, app), count in sorted(matches.items()):
        if count > totals.get((namespace, app), 0):
            raise ValueError('inconsistent_snapshots')
        if not count:
            continue
        identity = json.dumps([namespace, app, end], separators=(',', ':'))
        events.append({'event_type': 'error_summary', 'status': 'observed',
                       'event_id': hashlib.sha256(identity.encode()).hexdigest(),
                       'namespace': namespace, 'app': app or 'unlabelled',
                       'window_start': end - WINDOW, 'window_end': end,
                       'matched_lines': count, 'total_lines': totals[(namespace, app)]})
    events.append({'event_type': 'collector_heartbeat',
                   'status': 'logs_observed' if sum(totals.values()) else 'no_logs_observed',
                   'window_start': end - WINDOW, 'window_end': end,
                   'observed_sources': len(totals), 'total_lines': sum(totals.values()),
                   'matched_lines': sum(matches.values())})
    return events


def step(state, now, loader):
    end, gap = next_bucket(state, now)
    if end is None:
        return state, []
    updated = dict(state)
    try:
        matches = loader(query(True), end)
        totals = loader(query(False), end)
        events = summarize(matches, totals, end)
    except Exception as error:
        print('Loki summary failed: ' + type(error).__name__, flush=True)
        # Cursor stays put; no zero counts or recovery inferred from an error.
        updated['healthy'] = False
        events = [] if state.get('healthy') is False else [
            {'event_type':'collector_health', 'status':'unavailable'}]
        if now - state.get('failure_heartbeat', 0) >= 900:
            events.append({'event_type':'collector_heartbeat', 'status':'unavailable'})
            updated['failure_heartbeat'] = now
        return updated, events
    if state.get('healthy') is False:
        events.append({'event_type':'collector_health', 'status':'recovered'})
    if gap:
        events.append(gap)
    updated.update(last_bucket=end, healthy=True)
    return updated, events


def persist(directory, state, events, now):
    output = directory / 'events.log'
    if output.exists() and output.stat().st_size > 10 * 1024 * 1024:
        output.replace(directory / 'events.log.1')
    with output.open('a') as stream:
        for event in events:
            payload = {'home_log_monitoring': {'source':'loki', 'scope':'operations',
                                              'observed_at':int(now), **event}}
            stream.write('home-log-monitoring: ' + json.dumps(payload, separators=(',', ':')) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary = directory / 'state.tmp'
    with temporary.open('w') as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(directory / 'state.json')


def main():
    os.umask(0o027)
    directory = Path(os.environ.get('STATE_DIR','/var/ossec/logs/home-log-monitoring'))
    directory.mkdir(parents=True, exist_ok=True)
    checkpoint = directory / 'state.json'
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    if not isinstance(state, dict) or ('last_bucket' in state and not isinstance(state['last_bucket'], int)):
        raise ValueError('invalid_checkpoint')
    url = os.environ.get('LOKI_URL','http://loki.monitoring.svc.cluster.local:3100')
    while True:
        now = int(time.time())
        updated, events = step(state, now, lambda expression,end: fetch(url, expression, end))
        persist(directory, updated, events, now)
        state = updated
        time.sleep(60)


if __name__ == '__main__':
    main()
