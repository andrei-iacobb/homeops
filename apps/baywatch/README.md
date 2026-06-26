# BayWatch

Self-hosted drive-bay health & locator for the HPE Gen9 fleet (DL380 + DL360,
Smart HBA H240 / P440ar in HBA mode). Live LED-accurate chassis view, ZFS + SMART
health, click-to-locate. Design doc: `../../docs/drivebay-dashboard-plan.html`.

## Why two tiers

The LED hardware (`/sys/class/enclosure`, SES) lives on the **bare-metal Proxmox
hosts**. The Kubernetes cluster is Talos VMs on those hosts, so a pod cannot touch
the hardware. Hence:

```
browser ──https──▶ bw-ui (K8s pod, baywatch.iacob.uk)
                     │  LAN, token-auth
            ┌────────┴────────┐
       bw-agent @DL380   bw-agent @DL360   (systemd, root, bare metal)
       owns SES+health+LEDs
```

If bw-ui dies, the agents keep driving the health LEDs. The agent is the **single
writer** of every caddy LED (it replaces the old `drive-health-leds.timer`).

## Components

- `agent/` - `bw-agent`, a dependency-free static Go daemon. Reconcile loop reads
  SES sysfs + `zpool status` + `smartctl`, drives the amber fault LED (ZFS not
  ONLINE / errors, or SMART failing) and the blue locate LED (time-boxed), writing
  sysfs only on change. Serves REST + SSE on `:9099` with bearer-token auth.
- `ui/` - `bw-ui`, a static Go binary that aggregates both agents over the LAN,
  serves the embedded SVG-chassis frontend (`ui/static/index.html`), fans changes
  to browsers over SSE, and proxies time-boxed locate requests.
- `deploy/` - systemd unit + `install-agent.sh` (retires the old timer, installs
  the agent with the shared token).

## API (agent)

| Method | Path | |
|---|---|---|
| GET | `/v1/healthz` | unauthenticated liveness |
| GET | `/v1/enclosures` | full snapshot |
| GET | `/v1/stream` | SSE: initial snapshot + per-slot deltas |
| POST | `/v1/locate` | `{enclosure_id, slot, seconds}` time-boxed; 0 = clear |

bw-ui mirrors these under `/api/fleet`, `/api/stream`, `/api/locate` (+ `host`).

## Build & deploy

```sh
# UI image (no Docker needed):
task monitoring:build-baywatch

# Agents onto both Proxmox hosts:
task baywatch:deploy-agents
# then, on each host (shared token must match the K8s secret):
BW_TOKEN=<shared> BW_CONTROLLER="Smart HBA H240 x3 + H240ar" bash /tmp/install-agent.sh   # DL380
BW_TOKEN=<shared> BW_CONTROLLER="Smart Array P440ar (HBA mode)" bash /tmp/install-agent.sh # DL360
```

The K8s deploy lives in `kubernetes/apps/monitoring/baywatch/`. The shared bearer
token is in `secret.sops.yaml` (`BW_TOKEN`); the same value is in each host's
`/etc/baywatch/agent.env`. Rotate by updating both.

## Config (agent env)

`BW_BIND` `:9099` · `BW_TOKEN` · `BW_HOST_LABEL` · `BW_CONTROLLER` · `BW_POLL` 3s ·
`BW_SMART_POLL` 60s · `BW_LOCATE_DEFAULT` 120 · `BW_LOCATE_MAX` 600.
