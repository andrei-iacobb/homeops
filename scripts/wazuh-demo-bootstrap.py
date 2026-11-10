#!/usr/bin/env python3
"""Restore demo indexer permissions and retention after recreating its PVC.

Requires kubectl, PyYAML, and KUBECONFIG for the home cluster. Reads the existing
Grafana password from its Kubernetes Secret; does not generate new credentials.
Run after Wazuh is Ready. Existing retention policy is left unchanged.
"""
import base64
import json
from pathlib import Path
import subprocess
import yaml

CONFIG = Path(__file__).resolve().parents[1] / 'kubernetes/apps/monitoring/wazuh/app/config'
KUBECTL = ['kubectl', '-n', 'monitoring']


def request(method, path, body=None):
    # Paths and methods below are constants. Credentials stay inside the pod.
    command = ('C=/usr/share/wazuh-indexer/config/certs; curl -sS '
               '--cacert "$C/root-ca.pem" --cert "$C/admin.pem" '
               '--key "$C/admin-key.pem" -H "Content-Type: application/json" '
               f'-X {method} https://localhost:9200/{path} --data-binary @-')
    output = subprocess.check_output(
        KUBECTL + ['exec', '-i', 'wazuh-indexer-0', '--', 'bash', '-c', command],
        input=json.dumps(body).encode() if body is not None else b'')
    return json.loads(output)


def check(result):
    if 'error' in result or result.get('status') == 'error':
        raise RuntimeError('Indexer rejected bootstrap operation; inspect its logs')
    return result


secret = json.loads(subprocess.check_output(
    KUBECTL + ['get', 'secret', 'wazuh-grafana-datasource', '-o', 'json']))
datasource = yaml.safe_load(base64.b64decode(secret['data']['wazuh.yaml']))['datasources'][0]
check(request('PUT', '_plugins/_security/api/roles/grafana_wazuh_read',
              json.loads((CONFIG / 'grafana-read-role.json').read_text())))
check(request('PUT', '_plugins/_security/api/internalusers/grafana_wazuh', {
    'password': datasource['secureJsonData']['basicAuthPassword'], 'backend_roles': []}))
check(request('PUT', '_plugins/_security/api/rolesmapping/grafana_wazuh_read', {
    'users': ['grafana_wazuh'], 'backend_roles': [], 'hosts': []}))
policy_path = '_plugins/_ism/policies/wazuh-demo-retention-7d'
existing = request('GET', policy_path)
if existing.get('status') == 404:
    check(request('PUT', policy_path, json.loads((CONFIG / 'retention-policy.json').read_text())))
else:
    check(existing)
# The policy template covers future indices; attach existing unmanaged ones too.
check(request('POST', '_plugins/_ism/add/wazuh-alerts-*,wazuh-monitoring-*,wazuh-statistics-*',
              {'policy_id': 'wazuh-demo-retention-7d'}))
print('Grafana read-only access and seven-day retention configured.')
