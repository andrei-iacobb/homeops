# Prod Cluster Design

## Overview

Separate `~/prod` Git repo mirroring homeops architecture, scaffolded with core infrastructure only. Apps added later.

## Environment

- **Platform:** Proxmox VM on separate hardware from homeops
- **OS:** Talos Linux (single node)
- **Repo:** Independent Git repo at `~/prod`
- **Domain:** Placeholder (`prod.example.com`) — fill in later

## Stack

| Component | Purpose |
|-----------|---------|
| Talos Linux | Immutable Kubernetes OS |
| Cilium | CNI |
| Flux CD | GitOps (operator + instance) |
| CoreDNS | Cluster DNS |
| cert-manager | TLS certificates |
| OpenEBS | Local hostpath storage |
| Envoy Gateway | Ingress (internal + external gateways) |
| SOPS + Age | Secrets encryption (new key) |
| mise | CLI tool management |
| Taskfile | Automation tasks |
| Renovate | Dependency updates |

## Directory Structure

```
~/prod/
├── .mise.toml
├── .sops.yaml
├── .renovaterc.json5
├── .gitignore
├── .editorconfig
├── Taskfile.yaml
├── CLAUDE.md
├── .taskfiles/
│   ├── bootstrap/Taskfile.yaml
│   └── talos/Taskfile.yaml
├── scripts/
│   ├── bootstrap-apps.sh
│   └── lib/common.sh
├── talos/
│   ├── talconfig.yaml          # Placeholder node config
│   ├── talenv.yaml             # Talos/K8s versions
│   └── patches/global/         # Machine patches
├── bootstrap/
│   └── helmfile.d/             # Cilium, CoreDNS, cert-manager, Flux
└── kubernetes/
    ├── flux/cluster/ks.yaml
    ├── components/sops/
    └── apps/
        ├── cert-manager/
        ├── kube-system/        # Cilium, CoreDNS, Metrics Server, Reloader
        ├── network/            # Envoy Gateway
        ├── storage/            # OpenEBS
        └── flux-system/
```

## What's excluded (add later)

- Application workloads
- Monitoring (managed from homeops)
- NFS CSI
- Cloudflare Tunnel
- Specific node IPs, hostnames, domain

## Approach

Clone homeops structure, strip to core infra, generate new Age key for independent encryption. Same bootstrap flow: `task bootstrap:talos` → `task bootstrap:apps`.
