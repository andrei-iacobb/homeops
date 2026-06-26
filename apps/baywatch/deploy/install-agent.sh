#!/usr/bin/env bash
# install-agent.sh - install/upgrade the BayWatch host agent on a Proxmox host.
# Run ON the host (root). Expects these files staged in /tmp:
#   /tmp/bw-agent                  the linux/amd64 static binary
#   /tmp/baywatch-agent.service    the systemd unit
# And these env vars set by the caller:
#   BW_TOKEN        shared bearer token (same on every host + the K8s UI secret)
#   BW_CONTROLLER   human controller summary, e.g. "Smart HBA H240 x3 + H240ar"
#   BW_HOST_LABEL   optional; defaults to hostname
set -euo pipefail

: "${BW_TOKEN:?BW_TOKEN must be set}"
: "${BW_CONTROLLER:=}"
: "${BW_HOST_LABEL:=$(hostname)}"

echo "[*] Retiring the standalone drive-health-leds timer (folded into bw-agent)"
systemctl disable --now drive-health-leds.timer 2>/dev/null || true
systemctl stop drive-health-leds.service 2>/dev/null || true

echo "[*] Installing binary -> /usr/local/sbin/bw-agent"
install -m 0755 /tmp/bw-agent /usr/local/sbin/bw-agent

echo "[*] Writing /etc/baywatch/agent.env (0600)"
install -d -m 0755 /etc/baywatch
umask 077
cat > /etc/baywatch/agent.env <<ENV
BW_BIND=:9099
BW_TOKEN=${BW_TOKEN}
BW_HOST_LABEL=${BW_HOST_LABEL}
BW_CONTROLLER=${BW_CONTROLLER}
BW_POLL=3
BW_SMART_POLL=60
BW_LOCATE_DEFAULT=120
BW_LOCATE_MAX=600
ENV
chmod 0600 /etc/baywatch/agent.env

echo "[*] Installing systemd unit"
install -m 0644 /tmp/baywatch-agent.service /etc/systemd/system/baywatch-agent.service
systemctl daemon-reload
systemctl enable --now baywatch-agent.service

sleep 2
echo "[*] Verifying..."
if ! systemctl is-active --quiet baywatch-agent.service; then
  echo "ERROR: baywatch-agent.service is not active" >&2
  systemctl --no-pager status baywatch-agent.service || true
  exit 1
fi
if ! curl -fsS --max-time 5 http://127.0.0.1:9099/v1/healthz >/dev/null; then
  echo "ERROR: agent healthz did not respond" >&2
  exit 1
fi
echo "[*] OK - baywatch-agent active and healthy on $(hostname)"
