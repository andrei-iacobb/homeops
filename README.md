<div align="center">

# 🏠 homeops

**A single-node, GitOps-managed Kubernetes homelab. Talos underneath, Flux on top, everything declared in this repo.**

<br />

[![Talos](https://img.shields.io/badge/Talos-v1.14.5-2A2D34?style=for-the-badge&logo=kubernetes&logoColor=white)](https://www.talos.dev)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-v1.36.6-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white)](https://kubernetes.io)
[![Flux](https://img.shields.io/badge/Flux-v2.10-5468FF?style=for-the-badge&logo=flux&logoColor=white)](https://fluxcd.io)
[![Renovate](https://img.shields.io/badge/Renovate-self--hosted-1A1F6C?style=for-the-badge&logo=renovatebot&logoColor=white)](https://docs.renovatebot.com)

[![Last Commit](https://img.shields.io/github/last-commit/andrei-iacobb/homeops?style=flat-square&color=blue)](https://github.com/andrei-iacobb/homeops/commits/main)
[![Commit Activity](https://img.shields.io/github/commit-activity/m/andrei-iacobb/homeops?style=flat-square&color=blue)](https://github.com/andrei-iacobb/homeops/pulse)
[![Repo Size](https://img.shields.io/github/repo-size/andrei-iacobb/homeops?style=flat-square&color=blue)](https://github.com/andrei-iacobb/homeops)

<br />

**[🦊 Forgejo (primary)](https://git.iacob.co.uk/andrei/homeops)** ·
**[🐙 GitHub (mirror)](https://github.com/andrei-iacobb/homeops)** ·
**[🌍 iacob.co.uk](https://iacob.co.uk)**

</div>

---

## 📡 At a glance

```
  Cluster      home-cluster          single-node Talos Linux VM, 32 cores / 128 GB
  Git          Forgejo -> GitHub     Forgejo is the source of truth, GitHub is a mirror
  Reconciler   Flux Operator         watches main on Forgejo, push webhook for instant sync
  CNI          Cilium                LBIPAM + L2 announcements, Gateway API via Envoy
  Auth         Authentik             forward auth (SSO) on every internal route
  Storage      OpenEBS + NFS CSI     hostpath for app state, TrueNAS for media and bulk data
  Backups      VolSync + pg_dumpall  restic to MinIO on TrueNAS, nightly logical DB dumps
  Secrets      SOPS + age            encrypted in git, decrypted by Flux
  Updates      Renovate + tuppr      hourly dependency PRs, nightly Talos/K8s upgrades
```

| Namespace | Apps | | Namespace | Apps |
|---|---|---|---|---|
| `default` | 20 | | `monitoring` | 9 |
| `media` | 16 | | `storage` | 5 |
| `databases` | 5 | | `network` | 5 |
| `code` | 4 | | `kube-system` | 4 |
| `flux-system` | 2 | | `cert-manager`, `system-upgrade` | 2 |
| | | | **Total** | **~72** |

---

## 🏗️ Architecture

```
            *.iacob.co.uk                         *.iacob.uk
         (public, internet)                (LAN, WireGuard, tailnet)
                 │                                     │
                 ▼                                     │
       ┌───────────────────┐                           │
       │ Cloudflare Tunnel │                           │
       └─────────┬─────────┘                           │
                 ▼                                     ▼
       ╔═══════════════════╗               ╔═══════════════════╗
       ║  envoy-external   ║               ║  envoy-internal   ║
       ║    192.168.1.8    ║               ║    192.168.1.7    ║
       ╚═════════╤═════════╝               ╚═════════╤═════════╝
                 │                                   │ Authentik SSO
                 └─────────────────┬─────────────────┘
                                   ▼
                  ┌─────────────────────────────────┐
                  │   Talos  ·  home-cluster        │
                  │   VM on dl360, ~72 apps         │
                  └────────────────┬────────────────┘
                                   │ NFS (192.168.1.67)
                                   ▼
                  ┌─────────────────────────────────┐
                  │   TrueNAS  ·  VM on dl380       │
                  │   media · MinIO · backups       │
                  └─────────────────────────────────┘
```

- No inbound ports are open. Public apps go through a Cloudflare Tunnel.
- Internal names resolve to `192.168.1.7` through three wildcards: AdGuard, the UniFi gateway, and k8s-gateway. Which one answers depends on the client's VLAN.
- Remote access is UniFi WireGuard or the Headscale tailnet. Devbox advertises the home subnet as a tailnet route.

---

## 🖥️ Hardware

| Host | Model | RAM | Role |
|---|---|---|---|
| **dl360** | HPE ProLiant DL360 Gen9 | 252 GiB | Compute. Runs the Talos VM, AdGuard and the Windows lab VMs |
| **dl380** | HPE ProLiant DL380 Gen9 | 157 GiB | Storage. Runs the TrueNAS VM (96 GiB) with every data disk passed through |

Both hosts run Proxmox VE. The iLO fan curves are patched and watched by [`fanwatch`](./apps/fanwatch), and drive bay health is tracked by [`baywatch`](./apps/baywatch).

### TrueNAS pools

| Pool | Layout | Size | Holds |
|---|---|---|---|
| `plex` | 9x 1.8T SAS stripes + 960G special mirror | 14.4T | Plex/arr media library (`/mnt/plex/media`) |
| `SSD` | 1x 4TB 870 EVO | 3.6T | Time Machine, personal shares, ISOs for Proxmox, Immich |
| `sexy-pool` | raidz1, 3x 300G 10k SAS | 539G | MinIO (VolSync restic repos), k8s NFS volumes, VM cold archive |

> `plex` and `SSD` have no redundancy. Everything that matters has a backup somewhere else.

---

## 🧱 The Stack

<table>
<tr>
<td valign="top">

**Platform**
- [Talos Linux](https://www.talos.dev) · immutable K8s OS
- [Flux Operator](https://fluxcd.control-plane.io) · GitOps
- [tuppr](https://github.com/home-operations/tuppr) · Talos/K8s upgrades
- [Renovate](https://docs.renovatebot.com) · self-hosted CronJob
- [SOPS + age](https://github.com/getsops/sops) · secrets
- [Forgejo](https://forgejo.org) · git, CI runner, registry

</td>
<td valign="top">

**Networking**
- [Cilium](https://cilium.io) · CNI + LBIPAM
- [Envoy Gateway](https://gateway.envoyproxy.io) · Gateway API
- [cert-manager](https://cert-manager.io) · TLS
- [k8s_gateway](https://github.com/ori-edge/k8s_gateway) · cluster DNS
- [Cloudflare Tunnel](https://www.cloudflare.com/products/tunnel/) · public ingress
- [Headscale](https://github.com/juanfont/headscale) · tailnet
- [Authentik](https://goauthentik.io) · SSO

</td>
<td valign="top">

**Storage & Data**
- [OpenEBS](https://openebs.io) · hostpath PVs
- [NFS CSI](https://github.com/kubernetes-csi/csi-driver-nfs) · TrueNAS shares
- [VolSync](https://volsync.readthedocs.io) · restic to MinIO
- [PostgreSQL 17](https://www.postgresql.org) · shared DB
- [Redis](https://redis.io) · cache
- [MinIO](https://min.io) · S3

</td>
</tr>
</table>

---

## 📦 Applications

> Generated from the namespace kustomizations in [`kubernetes/apps/`](./kubernetes/apps). Click to expand.

<details>
<summary><b>🎬 Media · 16</b> - Plex, the arr stack and the watchlist flow</summary>

Plex · Tautulli · Sonarr · Radarr · Lidarr · Readarr · Bazarr · Prowlarr · Recyclarr · Pulsarr · qBittorrent · SABnzbd · Plexo · Lidify · Ring-MQTT · netboot.xyz

**How a request flows:** you add something to the Plex watchlist. Pulsarr sends it to Sonarr or Radarr. Those use the Recyclarr-synced max-quality profiles (Remux/WEB 2160p with a 1080p fallback). SABnzbd and qBittorrent download it, and Plex direct-plays the result. Lidarr grabs FLAC only.

</details>

<details>
<summary><b>🛠️ Default · 20</b> - identity, git, hosted sites and services</summary>

Authentik · Vaultwarden · Immich · Forgejo · Forgejo Runner · Forgejo Backup · Renovate · Website · NeatPlan · Shlink · Solis-Charge · Fanwatch · PetKit Snapshotter · Slate · Spill · Last Orders · Holden Systems · Burthorpe Pooch Parlour · Minecraft (Bedrock + ATM10 behind mc-router)

</details>

<details>
<summary><b>💻 Code · 4</b> - remote dev workspaces</summary>

T3 Code workspaces `alpha` · `beta` · `gamma` · cliproxy (shared Claude/Codex auth proxy)

</details>

<details>
<summary><b>📊 Monitoring · 9</b> - metrics, logs and analytics</summary>

Prometheus · Grafana · Loki · Alloy · Scrutiny · Plausible · Bays · Exporters (AdGuard, Proxmox, TrueNAS, iLO, Home Assistant, Syncthing, WireGuard-OCI, cliproxy)

</details>

<details>
<summary><b>🗄️ Databases · 5</b></summary>

PostgreSQL 17 · Redis · MinIO · Mosquitto (MQTT) · db-backup (nightly `pg_dumpall` to MinIO)

</details>

<details>
<summary><b>🌐 Network · 5</b></summary>

Envoy Gateway · Cloudflare Tunnel · Cloudflare DNS · k8s_gateway · Headscale

</details>

<details>
<summary><b>⚙️ System</b> - kube-system, storage, flux, upgrades</summary>

Cilium · CoreDNS · Metrics Server · Reloader · OpenEBS · NFS CSI (x3) · VolSync · cert-manager · Flux Operator + Instance · tuppr

</details>

<details>
<summary><b>🔗 External services</b> - routes to things that live outside the cluster</summary>

Home Assistant · Zigbee · iLO · Proxmox · TrueNAS · OpenClaw · UniFi switch · Windows VM · DNS · devbox previews · Hermes

</details>

---

## 🔄 GitOps workflow

```
   ┌──────────┐   git push            ┌───────────┐  webhook   ┌─────────┐
   │    me    │ ────────────────────▶ │  Forgejo  │ ─────────▶ │  Flux   │ ──▶ cluster
   └──────────┘                       │   main    │            └─────────┘
   ┌──────────┐   PR + automerge      │           │  mirror    ┌─────────┐
   │ Renovate │ ────────────────────▶ │           │ ─────────▶ │ GitHub  │
   └──────────┘   (hourly CronJob)    └───────────┘            └─────────┘
```

- **Source of truth:** Flux pulls `main` from `git.iacob.co.uk`. A Forgejo workflow fast-forwards the GitHub mirror after every push.
- **CI:** `flux-local` runs on the in-cluster Forgejo runner against every PR.
- **Updates:** patch and minor updates auto-merge. Majors and critical infra (Postgres, Cilium, Envoy, cert-manager, Plex and friends) wait for manual review.
- **Talos and Kubernetes:** [tuppr](https://github.com/home-operations/tuppr) applies upgrades overnight, so `talenv.yaml` is never edited by hand.

---

## 🗂️ Repository layout

```
homeops/
├── apps/                  # host-side tools: baywatch (drive bays), fanwatch (iLO fans), homeassistant
├── bootstrap/             # one-shot Helmfile to seed a fresh cluster
├── kubernetes/
│   ├── apps/<namespace>/  # one folder per workload
│   │   └── <app>/
│   │       ├── ks.yaml    # Flux Kustomization (entry point)
│   │       └── app/       # HelmRelease, OCIRepository, HTTPRoute, *.sops.yaml
│   ├── components/        # shared bits: sops, volsync
│   └── flux/              # top-level Kustomization graph + Helm repos
├── talos/                 # talconfig, talenv (pinned versions) and node patches
├── .forgejo/workflows/    # flux-local CI, GitHub mirror push
├── .taskfiles/            # bootstrap, talos, template tasks
└── Taskfile.yaml          # reconcile + volsync helpers
```

Most apps use [`bjw-s/app-template`](https://github.com/bjw-s-labs/helm-charts) v5 through an `OCIRepository`. Every app with an `openebs-hostpath` PVC also gets the VolSync component, which runs a daily restic backup to MinIO.

---

## 🔧 Operations

```bash
export KUBECONFIG=$PWD/kubeconfig

# Health
flux get all -A | grep -i false
kubectl get pods -A | grep -vE 'Running|Completed'

# Reconcile
task reconcile                                    # flux-system
flux reconcile ks <name> -n <ns> --with-source    # one app

# Talos
task talos:generate-config
task talos:apply-node IP=<ip>

# Backups (VolSync -> MinIO on TrueNAS)
task volsync:backup-all                           # sets spec.trigger.manual on every source
task volsync:status
task volsync:clear-manual                         # hand control back to the cron schedules

# Secrets
sops <file.sops.yaml>                             # edit
sops -e -i <file.sops.yaml>                       # encrypt in place
```

---

## 🙏 Credits

Built on [`onedr0p/cluster-template`](https://github.com/onedr0p/cluster-template), with patterns borrowed from [`onedr0p/home-ops`](https://github.com/onedr0p/home-ops), [`DavidIlie/home-cluster`](https://github.com/DavidIlie/home-cluster) and [kubesearch.dev](https://kubesearch.dev).

<div align="center">
<br />

<sub>Reconciled by Flux. Updated by Renovate. Maintained by coffee. ☕</sub>

</div>
