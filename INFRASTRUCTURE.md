# Homelab Infrastructure

## Physical Hardware

| Server | Model | CPU | RAM | Role | IP | iLO IP |
|--------|-------|-----|-----|------|-----|--------|
| DL360 Gen9 | HP ProLiant | 48 vCPU | 252 GiB | Primary compute (Proxmox) | 192.168.1.100 | 192.168.1.175 |
| DL380 Gen9 | HP ProLiant | 40 vCPU | 157 GiB | Storage (TrueNAS) | — | 192.168.1.180 |

## Network Devices

| Device | IP | Access |
|--------|----|--------|
| Router | 192.168.1.1 | router.iacob.uk |
| HP Switch | 192.168.1.101 | switch.iacob.uk |

## Network Topology

```
Internet
  │
  ├─ Cloudflare (DNS + Tunnel) ─── *.iacob.co.uk
  │                                  │
  │                              Envoy External (192.168.1.8)
  │                                  │
  ├─ Router (192.168.1.1) ──── LAN 192.168.1.0/24
  │     │
  │     ├─ HP Switch (192.168.1.101)
  │     │
  │     ├─ DL360 Gen9 (Proxmox) ─── 192.168.1.100
  │     │     ├─ VM 106: Kubernetes (Talos) ── 192.168.1.85
  │     │     ├─ VM 102: WireGuard VPN
  │     │     ├─ VM 104: AdGuard Home ──────── 192.168.1.120
  │     │     ├─ VM 105: Home Assistant ────── 192.168.1.90
  │     │     └─ Windows VM ────────────────── 192.168.1.187
  │     │
  │     └─ DL380 Gen9 (TrueNAS) ─── 192.168.1.67
  │           └─ VM 103: AdGuard Home ──────── 192.168.1.125
  │
  └─ P2P 10G Link (MTU 9000)
        K8s (10.10.10.2) ←→ TrueNAS (10.10.10.3)
```

## VMs (Proxmox)

| VM ID | Name | Host | IP | Purpose |
|-------|------|------|----|---------|
| 100 | TrueNAS | DL380 | 192.168.1.67 / 10.10.10.3 | NAS, NFS, MinIO S3 |
| 102 | WireGuard | DL360 | 192.168.1.50 | VPN gateway |
| 103 | AdGuard Home | DL380 | 192.168.1.125 | Secondary DNS |
| 104 | AdGuard Home | DL360 | 192.168.1.120 | Primary DNS |
| 105 | Home Assistant | DL360 | 192.168.1.90 | Home automation |
| 106 | Kubernetes | DL360 | 192.168.1.85 | Talos Linux cluster |
| — | Windows | DL360 | 192.168.1.187 | Windows VM |
| — | OpenClaw | — | 192.168.1.128 | OpenClaw service |

## IP Address Map

| IP | Purpose |
|----|---------|
| 192.168.1.1 | Router |
| 192.168.1.6 | k8s-gateway (split DNS) |
| 192.168.1.7 | Envoy internal gateway |
| 192.168.1.8 | Envoy external gateway |
| 192.168.1.50 | WireGuard VPN |
| 192.168.1.67 | TrueNAS (LAN) |
| 192.168.1.85 | Kubernetes API / Talos node |
| 192.168.1.90 | Home Assistant |
| 192.168.1.100 | Proxmox hypervisor |
| 192.168.1.101 | HP Switch |
| 192.168.1.120 | AdGuard Home #1 |
| 192.168.1.125 | AdGuard Home #2 |
| 192.168.1.128 | OpenClaw VM |
| 192.168.1.175 | iLO DL360 |
| 192.168.1.180 | iLO DL380 |
| 192.168.1.187 | Windows VM |
| 10.10.10.2 | K8s NFS interface |
| 10.10.10.3 | TrueNAS NFS interface |
| 10.41.0.0/16 | K8s service CIDR |
| 10.67.0.0/16 | K8s pod CIDR |

## DNS & Domains

| Domain | Type | Gateway | Use |
|--------|------|---------|-----|
| *.iacob.uk | Internal | envoy-internal (192.168.1.7) | Home network services |
| *.iacob.co.uk | External | envoy-external (192.168.1.8) | Public via Cloudflare Tunnel |
| *.iacobapp.dev | External | envoy-external | Developer tools |

**DNS resolution**: AdGuard Home (192.168.1.120/125) → k8s-gateway (192.168.1.6) for *.iacob.uk split DNS → CoreDNS (cluster internal)

**Certificates**: Let's Encrypt via cert-manager, Cloudflare DNS-01 challenge for iacob.co.uk, iacob.uk, iacobapp.dev

**DDNS**: Cloudflare DDNS updates plex.iacob.co.uk and wg.iacob.co.uk every 5 minutes

## Kubernetes Cluster

- **OS**: Talos Linux v1.12.4
- **Kubernetes**: v1.35.1
- **Node**: single node (home-cluster), schedulable control plane
- **CNI**: Cilium (native routing, eBPF, L2 announcements, DSR, kube-proxy replacement)
- **GitOps**: Flux CD watching main branch
- **Secrets**: SOPS + AGE encryption
- **Disk**: /dev/vda (virtual), ~126 GiB

## Kubernetes Services — All Namespaces

### ai

| App | Image | Port | URL | Gateway | Storage |
|-----|-------|------|-----|---------|---------|
| arca | ghcr.io/andrei-iacobb/arca:latest | 3000 | arca.iacob.uk, arca.iacob.co.uk | internal + external | openebs 20Gi |
| ollama | ollama/ollama:0.17.7 | 11434 | — (cluster only) | — | openebs 100Gi |
| openwebui | ghcr.io/open-webui/open-webui:main | 8080 | openwebui.iacob.co.uk | external | openebs 10Gi |

### databases

| App | Image | Port | URL | Gateway | Storage |
|-----|-------|------|-----|---------|---------|
| postgres | pgvector/pgvector:pg16 | 5432 | — | — | openebs 50Gi |
| redis | redis:8-alpine | 6379 | — | — | openebs 10Gi |
| minio | minio/minio:RELEASE.2025-07-23T15-54-02Z | 9000/9001 | console.iacob.uk | internal | openebs 100Gi |
| qdrant | qdrant/qdrant:v1.17.0 | 6333/6334 | qdrant.iacob.uk | internal | openebs 20Gi |
| pgadmin | dpage/pgadmin4:9.13.0 | 80 | pgadmin.iacob.uk | internal | openebs 5Gi |
| mosquitto | eclipse-mosquitto:2.0.22 | 1883 | — | — | openebs 256Mi |

**PostgreSQL databases**: immich (pgvector), vaultwarden, outline, gitea, paperless, authentik, vikunja, informate

### default

| App | Image | Port | URL | Gateway | Storage | DB |
|-----|-------|------|-----|---------|---------|-----|
| homepage | ghcr.io/gethomepage/homepage:latest | 3000 | home.iacob.uk, iacob.uk | internal | config only | — |
| website | ghcr.io/andrei-iacobb/website:latest | 3000 | iacob.uk, website.iacob.co.uk | internal + external | — | — |
| authentik | helm chart | 9000 | authentik.iacob.uk | internal | — | postgres |
| gitea | gitea/gitea:1.25.4 | 3000/22 | git.iacob.co.uk | external | openebs 20Gi | postgres |
| vaultwarden | vaultwarden/server:1.35.4 | 80 | vault.iacob.co.uk | external | openebs 10Gi | postgres |
| outline | outlinewiki/outline:1.5.0 | 3000 | notes.iacob.uk | internal | openebs 20Gi | postgres, redis |
| paperless | ghcr.io/paperless-ngx/paperless-ngx:2.20.10 | 8000 | paperless.iacob.uk | internal | openebs 20Gi | postgres, redis |
| n8n | docker.n8n.io/n8nio/n8n:2.12.0 | 5678 | n8n.iacob.uk | internal | openebs 10Gi | postgres |
| immich-server | ghcr.io/immich-app/immich-server:release | 2283 | photos.iacob.uk | internal | nfs-immich 100Gi | postgres, redis |
| immich-ml | ghcr.io/immich-app/immich-machine-learning:release | 3003 | — | — | nfs-immich 50Gi | — |
| informate | ghcr.io/andrei-iacobb/informate-*:latest | 8080/80 | informate.iacob.uk | internal | openebs 10Gi | postgres, qdrant |
| vikunja | docker.io/vikunja/vikunja:2.1.0 | 3456 | tasks.iacob.uk | internal | openebs 1Gi | postgres |
| shlink | shlinkio/shlink:stable + web-client | 8080 | s.iacobapp.dev (API), shlink.iacob.uk (web) | external (API), internal (web) | — | postgres |
| zipline | ghcr.io/diced/zipline:v4 | 3000 | share.iacob.co.uk | external | existing PVC | — |
| stirling-pdf | stirlingtools/stirling-pdf:2.5.3 | 8080 | pdf.iacob.uk | internal | openebs 1Gi | — |
| searxng | searxng/searxng:latest | 8080 | searxng.iacob.uk | internal | configMap | — |
| it-tools | ghcr.io/corentinth/it-tools:2024.10.22 | 80 | it-tools.iacob.uk | internal | — | — |
| echo | ghcr.io/mendhak/http-https-echo:39 | 80 | echo.iacob.uk | internal | — | — |
| openspeedtest | openspeedtest/latest:v2.0.6 | 3000 | openspeedtest.iacob.uk | internal | — | — |

### media

| App | Image | Port | URL | Storage |
|-----|-------|------|-----|---------|
| plex | ghcr.io/home-operations/plex:1.43.0.10492 | 32400 | plex.iacob.uk | NFS 1Ti (movies/tv/music) |
| jellyfin | jellyfin/jellyfin:10.11.6 | 8096 | jellyfin.iacob.uk | openebs 10Gi + 20Gi cache + NFS |
| sonarr | ghcr.io/home-operations/sonarr:4.0.16.2946 | 8989 | sonarr.iacob.uk | openebs + NFS |
| sonarr-lowq | ghcr.io/home-operations/sonarr:4.0.16.2946 | 8989 | sonarr-lowq.iacob.uk | openebs + NFS |
| radarr | ghcr.io/home-operations/radarr:6.1.1.10317 | 7878 | radarr.iacob.uk | openebs + NFS |
| radarr-lowq | ghcr.io/home-operations/radarr:6.1.1.10317 | 7878 | radarr-lowq.iacob.uk | openebs + NFS |
| lidarr | ghcr.io/home-operations/lidarr:3.1.2.4902 | 8686 | lidarr.iacob.uk | openebs + NFS |
| readarr | ghcr.io/home-operations/readarr:0.4.18.2805 | 8787 | readarr.iacob.uk | openebs + NFS |
| bazarr | ghcr.io/home-operations/bazarr:1.5.6 | 6767 | bazarr.iacob.uk | openebs 5Gi + NFS |
| prowlarr | ghcr.io/home-operations/prowlarr:2.3.3.5296 | 9696 | prowlarr.iacob.uk | openebs |
| sabnzbd | ghcr.io/home-operations/sabnzbd:4.5.5 | 8080 | sabnzbd.iacob.uk | openebs + NFS |
| qbittorrent | ghcr.io/home-operations/qbittorrent:5.1.4 | 8080 | qbittorrent.iacob.uk | openebs + NFS |
| tautulli | ghcr.io/home-operations/tautulli:2.16.1 | 8181 | tautulli.iacob.uk | openebs |
| overseerr | lscr.io/linuxserver/overseerr:1.35.0 | 5055 | overseerr.iacob.uk | openebs |
| recyclarr | ghcr.io/recyclarr/recyclarr:8.4.0 | — | recyclarr.iacob.uk | CronJob |
| huntarr | huntarr/huntarr:9.3.0 | 9705 | huntarr.iacob.uk | openebs 1Gi |
| agregarr | docker.io/agregarr/agregarr:v2.4.1 | 7171 | agregarr.iacob.uk | openebs 1Gi |
| wizarr | ghcr.io/wizarrrr/wizarr:v2026.2.1 | 5690 | wizarr.iacob.uk | openebs |
| tdarr | ghcr.io/haveagitgat/tdarr:2.62.01 | 8265 | tdarr.iacob.uk | openebs + NFS |
| ersatztv | jasongdove/ersatztv:latest | 8409 | ersatztv.iacob.uk | openebs 5Gi + NFS |
| calibre-web | lscr.io/linuxserver/calibre-web:0.6.26 | 8083 | calibre-web.iacob.uk | openebs 5Gi + NFS |
| lazylibrarian | lscr.io/linuxserver/lazylibrarian:latest | 5299 | lazylibrarian.iacob.uk | openebs 5Gi + NFS |
| lidify | custom image | 3006/3030 | lidify.iacob.uk | NFS 500Gi |
| frigate | ghcr.io/blakeblackshear/frigate:0.17.0 | 5000 | frigate.iacob.uk | openebs 5Gi + NFS |
| ring-mqtt | tsightler/ring-mqtt:5.9.3 | 55123 | ring-mqtt.iacob.uk | openebs |
| scrypted | ghcr.io/koush/scrypted:latest | 11080 | — | openebs |
| flaresolverr | ghcr.io/svaningelgem/flaresolverr:latest | 8191 | — | — |
| plexo | ghcr.io/davidilie/plexo:master | 3000 | — | — |
| pulsarr | docker.io/lakker/pulsarr:0.13.0 | 3003 | — | — |
| recommendarr | tannermiddleton/recommendarr:v1.4.4 | 3000 | — | — |

All media apps use envoy-internal gateway.

### monitoring

| App | Image | Port | URL | Storage |
|-----|-------|------|-----|---------|
| prometheus | kube-prometheus-stack | 9090 | prometheus.iacob.uk | openebs 50Gi |
| grafana | (in kube-prometheus-stack) | 3000 | grafana.iacob.uk | openebs 10Gi |
| alertmanager | (in kube-prometheus-stack) | — | — | openebs 10Gi |
| loki | grafana/loki | 3100 | — | openebs 50Gi |
| alloy | grafana/alloy (DaemonSet) | — | — | — |
| promtail | promtail | 3101 | — | — |
| plausible | ghcr.io/plausible/community-edition:v3.2.0 | 8000 | plausible.iacob.co.uk | openebs 10Gi |
| clickhouse | clickhouse/clickhouse-server:26.2-alpine | 8123 | — | openebs 20Gi |
| uptime-kuma | louislam/uptime-kuma:2 | 3001 | uptime.iacob.uk | openebs 5Gi |
| scrutiny | ghcr.io/analogj/scrutiny:master-omnibus | 8080 | scrutiny.iacob.uk | openebs 2Gi |
| graphite-exporter | prom/graphite-exporter:v0.16.0 | 9108/9109 | — | — |

**Exporters:**

| Exporter | Target | Port |
|----------|--------|------|
| adguard-1 | 192.168.1.120 | 9617 |
| adguard-2 | 192.168.1.125 | 9617 |
| proxmox | 192.168.1.100 | 9221 |
| ilo-dl360 | 192.168.1.175 | 9545 |
| ilo-dl380 | 192.168.1.180 | 9545 |
| truenas | 192.168.1.67 | 9814 |
| wireguard | 192.168.1.50 | 9586 |

**Grafana dashboards**: node-exporter-full, k8s-pods, k8s-global, TrueNAS, AdGuard, iLO, Proxmox

**Alertmanager**: sends to n8n webhook (http://n8n.default.svc:5678/webhook/alertmanager)

### network

| App | Image | Port | IP | Purpose |
|-----|-------|------|----|---------|
| envoy-gateway | Envoy Gateway helm | 80/443 | 192.168.1.7 (int), 192.168.1.8 (ext) | L7 gateway (Gateway API) |
| cloudflare-tunnel | cloudflare/cloudflared:2026.2.0 | 8080 | — | Tunnel external traffic |
| cloudflare-ddns | favonia/cloudflare-ddns:1.15.1 | — | — | DDNS updates |
| k8s-gateway | k8s-gateway helm | 53 | 192.168.1.6 | Split DNS for *.iacob.uk |

### kube-system

| App | Purpose |
|-----|---------|
| cilium | CNI (eBPF, L2 announcements, DSR, kube-proxy replacement) |
| coredns | Cluster DNS (2 replicas) |
| metrics-server | Resource metrics |
| reloader | Auto-restart on ConfigMap/Secret changes |

### cert-manager

| App | Purpose |
|-----|---------|
| cert-manager | Let's Encrypt certificates, Cloudflare DNS-01 solver |

### storage

| App | Purpose |
|-----|---------|
| openebs | Local hostpath storage (/var/openebs/local) |
| nfs-csi | NFS provisioner → TrueNAS /mnt/plex/media |
| nfs-csi-immich | NFS provisioner → TrueNAS /mnt/SSD/immich |
| volsync | Restic backups to MinIO S3 (40 apps, 6h/7d/4w/3m retention) |

### flux-system

| App | Purpose |
|-----|---------|
| flux-operator | Flux CD operator |
| flux-instance | GitOps reconciler (flux.iacob.uk dashboard) |

## External Service Routes (non-K8s → Envoy)

| Service | IP | Port | URL |
|---------|----|------|-----|
| TrueNAS | 192.168.1.67 | 443 | truenas.iacob.uk |
| Proxmox | 192.168.1.100 | 8006 | proxmox.iacob.uk |
| Home Assistant | 192.168.1.90 | 8123 | hass.iacob.co.uk |
| AdGuard #1 | 192.168.1.120 | 80 | dns1.iacob.uk |
| AdGuard #2 | 192.168.1.125 | 80 | dns2.iacob.uk |
| iLO DL360 | 192.168.1.175 | 443 | ilo1.iacob.uk |
| iLO DL380 | 192.168.1.180 | 443 | ilo2.iacob.uk |
| Router | 192.168.1.1 | 80 | router.iacob.uk |
| HP Switch | 192.168.1.101 | 80 | switch.iacob.uk |
| OpenClaw | 192.168.1.128 | 18789 | openclaw.iacob.uk |
| Windows VM | 192.168.1.187 | 3000 | cc.iacobapp.dev |

## Storage Summary

| Class | Backend | Capacity | Use |
|-------|---------|----------|-----|
| openebs-hostpath | /var/openebs/local (node disk) | ~126 GiB shared | Databases, app configs, monitoring |
| nfs-media | TrueNAS 10.10.10.3:/mnt/plex/media | TBs | Movies, TV, music, books, downloads |
| nfs-immich | TrueNAS 10.10.10.3:/mnt/SSD/immich | SSD pool | Photo backups |

## Service Count

| Category | Count |
|----------|-------|
| Physical servers | 2 |
| VMs | 7+ |
| Kubernetes namespaces | 12 |
| Kubernetes apps | ~80 |
| Externally accessible services | ~10 |
| Internally accessible services | ~50 |
| PostgreSQL databases | 8 |
| Prometheus exporters | 7 |
| VolSync-backed apps | 40 |
| Container images | 80+ |
