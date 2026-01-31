# Monitoring Stack

This directory contains the Prometheus and Grafana monitoring stack for the home cluster.

## Components

### Core Stack
- **Prometheus Operator** - Manages Prometheus and Alertmanager instances
- **Prometheus** - Metrics collection and storage
- **Grafana** - Visualization and dashboards
- **Alertmanager** - Alert routing and notification

### Exporters

#### AdGuard Home (2 instances)
- **Exporter**: `sfragata/adguardhome_exporter`
- **Port**: 9617
- **Secrets**: `adguard-1-secret`, `adguard-2-secret`
- **Metrics**: DNS queries, blocked domains, upstream stats

#### Proxmox
- **Exporter**: `prometheuscommunity/pve-exporter`
- **Port**: 9221
- **Secret**: `proxmox-secret`
- **Metrics**: VM stats, node resources, storage

#### TrueNAS
- **Exporter**: `ghcr.io/andrei-iacobb/truenas-exporter` (API-based, pulls from TrueNAS)
- **Port**: 9814
- **Secret**: `truenas-secret` (TRUENAS_HOST, TRUENAS_API_KEY)
- **Metrics**: Replication, apps, VMs, Incus instances
- **Build**: `docker build -t ghcr.io/andrei-iacobb/truenas-exporter:latest kubernetes/apps/monitoring/exporters/truenas && docker push ghcr.io/andrei-iacobb/truenas-exporter:latest`

#### WireGuard
- **Exporter**: `mindflavor/prometheus-wireguard-exporter`
- **Port**: 9586
- **Metrics**: VPN connections, traffic stats
- **Note**: Runs on WireGuard host (192.168.1.67). Prometheus scrapes it remotely.
- **Setup on WireGuard host**: `docker run -d --restart unless-stopped --net=host --cap-add=NET_ADMIN --name wireguard-exporter mindflavor/prometheus-wireguard-exporter`

#### iLO (DL360 G9 & DL380 G9)
- **Exporter**: `mdvorak/ilo4-metrics-exporter`
- **Port**: 8080
- **Secrets**: `ilo-dl360-secret`, `ilo-dl380-secret`
- **Metrics**: Server temperature, power, hardware health

## Configuration Required

### Secrets to Update

All secrets need to be encrypted with SOPS before committing:

1. **AdGuard Home** (`adguard-1-secret.sops.yaml`, `adguard-2-secret.sops.yaml`)
   - `ADGUARD_IP`: IP address of AdGuard instance
   - `ADGUARD_USER`: Admin username
   - `ADGUARD_PASSWORD`: Admin password

2. **Proxmox** (`proxmox-secret.sops.yaml`)
   - `PROXMOX_HOST`: Proxmox hostname/IP
   - `PROXMOX_USER`: Proxmox API user (e.g., `prometheus@pam`)
   - `PROXMOX_PASSWORD`: API token or password

3. **TrueNAS** (`truenas-secret.sops.yaml`)
   - `TRUENAS_HOST`: TrueNAS hostname/IP
   - `TRUENAS_API_KEY`: TrueNAS API key

4. **iLO** (`ilo-dl360-secret.sops.yaml`, `ilo-dl380-secret.sops.yaml`)
   - `ILO_HOST`: iLO IP address
   - `ILO_USER`: iLO username
   - `ILO_PASSWORD`: iLO password

5. **Grafana** (`prometheus/app/secret.sops.yaml`)
   - `admin-user`: Grafana admin username (default: admin)
   - `admin-password`: Grafana admin password

## Access

- **Grafana**: https://grafana.iacob.uk
- **Prometheus**: https://prometheus.iacob.uk

## Grafana Dashboards

Recommended dashboards to import:
- Kubernetes Cluster Monitoring: `6417`
- Node Exporter Full: `1860`
- AdGuard Home: `20799` or `24520`
- Proxmox: `10347`
- TrueNAS: Search for "TrueNAS" dashboards
- WireGuard: Search for "WireGuard" dashboards
- iLO: Search for "HP iLO" dashboards
