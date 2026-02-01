# DavidIlie/home-cluster vs homeops Comparison

Comparison of [DavidIlie/home-cluster](https://github.com/DavidIlie/home-cluster) against this repository (homeops). Generated from a clone-and-compare pass.

---

## 1. Services They Have That You Don’t

| Area | DavidIlie-only (or different) |
|------|-------------------------------|
| **CI/GitHub** | `actions-runner-system`: **actions-runner-controller**, **home-cluster-runner** (self-hosted GitHub Actions runners) |
| **AI** | **SearXNG** (search) |
| **Databases** | **ClickHouse**, **CloudNative-PG** (operator), **Dragonfly** (Redis alternative). You use Postgres/MariaDB/Redis/MinIO/pgAdmin. |
| **Default / tools** | **Minimserver** (DLNA), **nas-download**, **OpenSpeedTest**, **Paperless-ngx**, **Pelican** (static site), **personal-dashboard**, **Shlink** (short links), **uber-item-viewer**, **it-tools** |
| **Downloads** | Separate **downloads** namespace: FlareSolverr, Prowlarr, qbittorrent, SABnZBD (you have these under **media**) |
| **External integrations** | **external-services**: Home Assistant, iDRAC, MacBook, Pelican, Proxmox, TrueNAS, Unifi (likely ExternalSecrets or similar) |
| **Infrastructure** | **infastructure**: **go2rtc** (streaming), **Portainer**, **Scrypted** (cameras/NVR) |
| **Kube-system** | **node-feature-discovery**, **nvidia-device-plugin**, **Spegel** (OCI mirror) |
| **Media** | **Agregarr**, **Stream-Master**, **Tautulli**, **Threadfin** (IPTV) |
| **Observability** | **observability** namespace: **cloudflare-exporter**, **dcgm-exporter**, **Gatus**, **iDRAC exporter**, **Plausible**, **unpoller** (UniFi), plus **kube-prometheus-stack** |
| **Upgrades** | **system-upgrade**: **Tuppr** (Talos/Kubernetes upgrades) |
| **Backup/sync** | **volsync-system**: **VolSync**, **Kopia**; **openebs-system** (OpenEBS in its own namespace) |

---

## 2. Services You Have That They Don’t

| Area | Homeops-only |
|------|--------------|
| **Databases** | **MariaDB**, **MinIO**, **pgAdmin**, **Postgres** (non-operator), **Redis** |
| **Default** | **Gitea**, **Immich**, **n8n**, **Outline**, **Vaultwarden** |
| **Media** | **Calibre-Web**, **Huntarr**, **Jellyfin**, **LazyLibrarian**, **Lidarr**, **Lidify**, **Readarr**, **Recommendarr**, **Tdarr** |
| **Network** | **cloudflare-dns**, **cloudflare-tunnel**, **envoy-gateway**, **k8s-gateway** (they use **network/external** and **network/internal** instead) |
| **Storage** | **nfs-csi**, **nfs-csi-immich**, **openebs** (you have a dedicated **storage** namespace) |

---

## 3. Build and Layout Differences

| Aspect | DavidIlie/home-cluster | Your homeops |
|--------|------------------------|--------------|
| **Bootstrap location** | Under **kubernetes**: `kubernetes/bootstrap/` (single `helmfile.yaml` + `talos/` there) | **Root**: `bootstrap/` (helmfile.d) and **talos/** at repo root |
| **Taskfile vars** | `KUBERNETES_DIR`, `TALHELPER_DIR` = `kubernetes/bootstrap/talos`, `PRIVATE_DIR` | `BOOTSTRAP_DIR`, `TALOS_DIR` = `talos`, `SCRIPTS_DIR` |
| **Bootstrap apps** | `helmfile --file kubernetes/bootstrap/helmfile.yaml apply` (direct helmfile) | `scripts/bootstrap-apps.sh` → namespaces → SOPS secrets → then helmfile (from bootstrap/helmfile.d) |
| **Flux layout** | **Two-step**: `cluster-meta` (applies `kubernetes/flux/meta` first) → `cluster-apps` **dependsOn** `cluster-meta`. Helm/OCI repos live in **flux/meta/repositories/** | **Single** `cluster-apps` kustomization; no `cluster-meta`. Helm/OCI sources are **per-app** (next to each app’s HelmRelease) |
| **Cluster kustomization** | `cluster-apps` patches only **decryption** (SOPS + `secretRef: sops-age`) into child Kustomizations | `cluster-apps` patches **HelmRelease defaults** (install/upgrade/rollback strategy, CRDs) and decryption |
| **SOPS in Flux** | Explicit `secretRef: name: sops-age` in cluster Kustomization | Decryption `provider: sops`; secret ref can be in bootstrap/scripts |
| **Makejinja** | `data = ["./config.yaml"]`, excludes `.mjfilter.py` | `data = ["./cluster.yaml", "./nodes.yaml"]`, `copy_metadata = true` |
| **Template validation** | `.taskfiles/template/resources/`: only **kubeconform.sh** | Same plus **cluster.schema.cue**, **nodes.schema.cue** (Cue schemas) |
| **Bootstrap Cilium** | Cilium **needs** `observability/prometheus-operator-crds` in helmfile | Cilium has no such dependency in bootstrap |
| **Extra in repo** | `.sopsrc`, `.vscode/`, `hack/` (e.g. iperf, swissarmy), `requirements.txt` | `.renovaterc.json5`; no `.vscode` or `hack/` in layout |

---

## 4. Summary

- **Services**: They add runners, VolSync/Kopia, Tuppr, more observability and “infra” apps (Portainer, Scrypted, go2rtc), external-service definitions, and a few extra default apps. You add a bigger media stack, Gitea/Immich/n8n/Outline/Vaultwarden, and different network (Envoy, Cloudflare, k8s-gateway) and storage (NFS CSI, etc.).
- **Build**: They use **flux/meta + dependsOn** and **bootstrap under kubernetes/** with a single helmfile; you use **one cluster-apps** with **per-app sources**, **root bootstrap + talos**, a **bootstrap script**, and **Cue schemas** for templates. Your approach keeps app definitions more self-contained; theirs keeps all Flux sources in one place.
e