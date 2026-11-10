#!/usr/bin/env python3
"""One-time journal collector setup for the two personal Proxmox hosts.
Requires key-auth SSH aliases dl360/dl380 and the source-restricted Loki ingest service.
Stops if Alloy is already installed so existing configurations are never overwritten.
"""
import subprocess
for host in ['dl360','dl380']:
 script='''set -eu
if command -v alloy >/dev/null 2>&1; then echo 'Existing Alloy installation needs inspection'; exit 2; fi
curl -fsSL https://apt.grafana.com/gpg.key | gpg --batch --yes --dearmor -o /usr/share/keyrings/grafana.gpg
chmod 644 /usr/share/keyrings/grafana.gpg
printf '%s\\n' 'deb [signed-by=/usr/share/keyrings/grafana.gpg] https://apt.grafana.com stable main' > /etc/apt/sources.list.d/grafana.list
apt-get update -qq -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/grafana.list -o Dir::Etc::sourceparts=-
apt-get install -y -qq alloy=1.21.3-1
usermod -aG adm,systemd-journal alloy
cat > /etc/alloy/config.alloy <<'CONFIG'
loki.relabel "journal" {
  forward_to = []
  rule {
    source_labels = ["__journal__systemd_unit"]
    target_label = "unit"
  }
}
loki.source.journal "host" {
  max_age = "1h"
  labels = {namespace = "proxmox", host = "HOST_LABEL", job = "proxmox-journal"}
  relabel_rules = loki.relabel.journal.rules
  forward_to = [loki.write.home.receiver]
}
loki.write "home" {
  endpoint {
    url = "http://192.168.1.85:30100/loki/api/v1/push"
  }
}
CONFIG
alloy validate /etc/alloy/config.alloy
systemctl enable alloy
systemctl restart alloy
apt-mark hold alloy
systemctl is-active alloy
'''.replace('HOST_LABEL',host)
 r=subprocess.run(['ssh',host,'bash','-s'],input=script,text=True,capture_output=True,timeout=180);print(host,r.returncode,r.stdout[-500:],r.stderr[-250:])
