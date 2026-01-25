# 🏠 Homelab Infrastructure

A GitOps-managed homelab running on enterprise HP ProLiant servers, featuring a Kubernetes cluster deployed with Talos Linux and managed by Flux CD.

## 📊 Infrastructure Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              PROXMOX CLUSTER                                │
├─────────────────────────────────┬───────────────────────────────────────────┤
│         HP DL360 Gen9           │              HP DL380 Gen9                │
│    48 vCPUs | 252 GiB RAM       │         40 vCPUs | 157 GiB RAM            │
├─────────────────────────────────┼───────────────────────────────────────────┤
│  ┌───────────────────────────┐  │  ┌─────────────────────────────────────┐  │
│  │ 102 - WireGuard VPN       │  │  │ 100 - TrueNAS (Storage Server)      │  │
│  │ 104 - AdGuard Home (DNS)  │  │  │ 103 - AdGuard Home (DNS Backup)     │  │
│  │ 105 - Home Assistant OS   │  │  └─────────────────────────────────────┘  │
│  │ 106 - Kubernetes (main)   │  │                                           │
│  └───────────────────────────┘  │                                           │
└─────────────────────────────────┴───────────────────────────────────────────┘
```

## 🖥️ Hardware

| Server | Model | CPUs | RAM | Role |
|--------|-------|------|-----|------|
| **DL360G9** | HP ProLiant DL360 Gen9 | 48 vCPUs | 252 GiB | Primary compute - Kubernetes, VPN, DNS, Home Automation |
| **DL380G9** | HP ProLiant DL380 Gen9 | 40 vCPUs | 157 GiB | Storage server (TrueNAS), Secondary DNS |

**Total Resources:** 88 vCPUs | 409 GiB RAM

## 🌐 Network Architecture

### DNS & VPN Services

| Service | Purpose | Location |
|---------|---------|----------|
| **AdGuard Home** | Primary DNS with ad-blocking | DL360G9 (VM 104) |
| **AdGuard Home** | Secondary DNS (failover) | DL380G9 (VM 103) |
| **WireGuard** | VPN for secure remote access | DL360G9 (VM 102) |

### Domain Configuration

| Domain | Usage |
|--------|-------|
| `iacob.uk` | Internal services (accessible within LAN & VPN) |
| `iacob.co.uk` | External services (accessible via Cloudflare Tunnel) |

## 🏡 Home Automation

**Home Assistant OS** (VM 105 on DL360G9) serves as the central home automation hub, integrating with various smart home devices and providing a unified control interface.

## 💾 Storage

**TrueNAS** (VM 100 on DL380G9) provides centralized network storage:
- NFS shares for Kubernetes persistent volumes
- Media library storage for Plex, Jellyfin, and *arr stack
- Backup storage for critical data

## ☸️ Kubernetes Cluster

A single-node Kubernetes cluster running on **Talos Linux**, managed entirely through GitOps principles using **Flux CD**.

### Core Components

| Component | Purpose |
|-----------|---------|
| **Talos Linux** | Immutable, secure Kubernetes OS |
| **Flux CD** | GitOps continuous delivery |
| **Cilium** | CNI networking & network policies |
| **Envoy Gateway** | Ingress/Gateway API implementation |
| **cert-manager** | Automated TLS certificate management |
| **SOPS** | Secrets encryption for GitOps |

### Storage Providers

| Provider | Purpose |
|----------|---------|
| **NFS CSI** | TrueNAS NFS storage provisioner |
| **OpenEBS** | Local persistent volumes |

---

## 🎬 Media Stack

A complete media automation and streaming setup using the *arr stack.

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Overseerr  │────▶│   Prowlarr   │────▶│  Indexers    │
│   (Requests) │     │  (Indexers)  │     │              │
└──────────────┘     └──────────────┘     └──────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│    Sonarr    │     │    Radarr    │     │    Lidarr    │
│  (TV Shows)  │     │   (Movies)   │     │   (Music)    │
└──────────────┘     └──────────────┘     └──────────────┘
        │                   │                   │
        └───────────────────┼───────────────────┘
                            ▼
        ┌───────────────────┬───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ qBittorrent  │     │   SABnzbd    │     │    Bazarr    │
│ (Torrents)   │     │  (Usenet)    │     │ (Subtitles)  │
└──────────────┘     └──────────────┘     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │    Tdarr     │
                     │(Transcoding) │
                     └──────────────┘
                            │
        ┌───────────────────┴───────────────────┐
        ▼                                       ▼
┌──────────────┐                         ┌──────────────┐
│     Plex     │                         │   Jellyfin   │
│  (Streaming) │                         │  (Streaming) │
└──────────────┘                         └──────────────┘
```

### Media Applications

| Application | Purpose | Access |
|-------------|---------|--------|
| **Plex** | Media streaming server | Internal |
| **Jellyfin** | Open-source media streaming | Internal |
| **Sonarr** | TV show management & automation | Internal |
| **Radarr** | Movie management & automation | Internal |
| **Lidarr** | Music management & automation | Internal |
| **Bazarr** | Subtitle management | Internal |
| **Prowlarr** | Indexer manager for *arr apps | Internal |
| **qBittorrent** | Torrent download client | Internal |
| **SABnzbd** | Usenet download client | Internal |
| **Overseerr** | Media request management | Internal |
| **Tdarr** | Automated media transcoding | Internal |
| **Recyclarr** | TRaSH Guides sync for *arr apps | Internal |
| **Huntarr** | Hunt missing media | Internal |
| **Recommendarr** | Media recommendations | Internal |

### Books & Reading

| Application | Purpose |
|-------------|---------|
| **Readarr** | eBook/audiobook management |
| **Calibre-Web** | eBook library & reader |
| **LazyLibrarian** | Book metadata & organization |
| **Lidify** | Audiobook management |

---

## 🤖 AI & Automation

| Application | Purpose |
|-------------|---------|
| **Ollama** | Local LLM inference server |
| **Open WebUI** | ChatGPT-like interface for Ollama |
| **n8n** | Workflow automation platform |

---

## 📱 Applications

### Productivity & Self-Hosted Services

| Application | Purpose | Access |
|-------------|---------|--------|
| **Homepage** | Dashboard for all services | Internal |
| **Vaultwarden** | Bitwarden-compatible password manager | External |
| **Gitea** | Self-hosted Git service | Internal |
| **Outline** | Team wiki & knowledge base | Internal |
| **Immich** | Self-hosted photo & video backup | External |

### Databases

| Application | Purpose |
|-------------|---------|
| **PostgreSQL** | Primary relational database |
| **MariaDB** | MySQL-compatible database |
| **Redis** | In-memory cache & message broker |
| **MinIO** | S3-compatible object storage |
| **pgAdmin** | PostgreSQL administration |

---

## 🔒 Security & Access

### External Access (via Cloudflare Tunnel)

Services exposed to the internet are secured through **Cloudflare Tunnel**, providing:
- Zero-trust access without exposing ports
- DDoS protection
- SSL/TLS termination
- Access policies and authentication

### Internal Access

Internal services are accessible via:
- **WireGuard VPN** for remote access
- Local network access
- Split DNS via AdGuard Home (resolves `iacob.uk` to internal IPs)

---

## 🛠️ GitOps Workflow

This repository follows GitOps principles:

1. **Infrastructure as Code** - All Kubernetes manifests are stored in this repository
2. **Flux CD** watches the repository for changes
3. **Automated reconciliation** - Changes pushed to `main` are automatically applied
4. **Secrets management** - Sensitive data encrypted with SOPS/Age

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   GitHub    │────▶│   Flux CD   │────▶│ Kubernetes  │
│ Repository  │     │  (GitOps)   │     │   Cluster   │
└─────────────┘     └─────────────┘     └─────────────┘
       │                                       │
       │            ┌─────────────┐            │
       └───────────▶│  Renovate   │◀───────────┘
                    │ (Auto PRs)  │
                    └─────────────┘
```

### Repository Structure

```
kubernetes/
├── apps/                    # Application deployments
│   ├── ai/                  # AI services (Ollama, Open WebUI)
│   ├── cert-manager/        # TLS certificate management
│   ├── databases/           # Database services
│   ├── default/             # Core applications
│   ├── flux-system/         # Flux CD configuration
│   ├── kube-system/         # System components
│   ├── media/               # Media stack (*arr apps, Plex, etc.)
│   ├── network/             # Network services
│   └── storage/             # Storage provisioners
├── components/              # Shared components
└── flux/                    # Flux configuration
```

---

## 🔧 Operations

### Useful Commands

```bash
# Check Flux status
flux get ks -A
flux get hr -A

# Force reconciliation
task reconcile

# Check Cilium status
cilium status

# View all pods
kubectl get pods -A

# Check certificates
kubectl -n network describe certificates
```

### Maintenance Tasks

| Task | Command |
|------|---------|
| Bootstrap Talos | `task bootstrap:talos` |
| Bootstrap Apps | `task bootstrap:apps` |
| Upgrade Talos | `task talos:upgrade-node IP=<ip>` |
| Upgrade Kubernetes | `task talos:upgrade-k8s` |
| Reset Cluster | `task talos:reset` |

---

## 📈 Monitoring

Services can be monitored through:
- **Homepage** dashboard for quick status overview
- Kubernetes native metrics via **metrics-server**
- Application-specific health checks

---

## 🙏 Acknowledgments

This setup is based on the [onedr0p/cluster-template](https://github.com/onedr0p/cluster-template), providing a solid foundation for GitOps-managed Kubernetes homelab deployments.

---

<div align="center">

**[iacob.uk](https://iacob.uk)** | Internal Services

**[iacob.co.uk](https://iacob.co.uk)** | External Services

</div>
