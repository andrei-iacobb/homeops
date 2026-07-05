#!/usr/bin/env python3
"""Minimal, read-only WireGuard exporter for Prometheus.

Serves /metrics on :9586. Runs `wg show all dump` and emits per-peer transfer
and handshake metrics. It NEVER modifies WireGuard state - it only reads it, so
it cannot affect the tunnel or connectivity in any way.

Deployed on the DL360 Proxmox host (192.168.1.100) via systemd (wg-exporter.service)
because `wg show` must run on the host that holds the interface. Scraped by the
in-cluster Prometheus via a headless Service + Endpoints + ServiceMonitor.
"""
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 9586


def _esc(v: str) -> str:
    """Escape a Prometheus label value (backslash, double-quote, newline)."""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def collect() -> str:
    try:
        out = subprocess.run(
            ["wg", "show", "all", "dump"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return "# HELP wireguard_up 1 if wg show succeeded\n# TYPE wireguard_up gauge\nwireguard_up 0\n"
    if out.returncode != 0:
        return "# HELP wireguard_up 1 if wg show succeeded\n# TYPE wireguard_up gauge\nwireguard_up 0\n"

    rx, tx, hs, peers = [], [], [], []
    for line in out.stdout.strip().splitlines():
        f = line.split("\t")
        # interface line has 5 fields; peer line has 9.
        if len(f) == 9:
            ifc, pubkey, _psk, endpoint, allowed, handshake, r, t, _ka = f
            lbl = (f'interface="{_esc(ifc)}",public_key="{_esc(pubkey)}",'
                   f'endpoint="{_esc(endpoint)}",allowed_ips="{_esc(allowed)}"')
            rx.append(f"wireguard_peer_received_bytes_total{{{lbl}}} {r}")
            tx.append(f"wireguard_peer_sent_bytes_total{{{lbl}}} {t}")
            hs.append(f"wireguard_peer_latest_handshake_seconds{{{lbl}}} {handshake}")
            peers.append(f"wireguard_peer_up{{{lbl}}} 1")

    lines = [
        "# HELP wireguard_up 1 if wg show succeeded",
        "# TYPE wireguard_up gauge",
        "wireguard_up 1",
    ]
    if rx:
        lines += ["# TYPE wireguard_peer_received_bytes_total counter"] + rx
        lines += ["# TYPE wireguard_peer_sent_bytes_total counter"] + tx
        lines += ["# TYPE wireguard_peer_latest_handshake_seconds gauge"] + hs
        lines += ["# TYPE wireguard_peer_up gauge"] + peers
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") not in ("/metrics", ""):
            self.send_response(404)
            self.end_headers()
            return
        body = collect().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # stay quiet; systemd journal would fill with scrape lines otherwise


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
