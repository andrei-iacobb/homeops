# Project Structure

```
homeops/
├── kubernetes/                  # Kubernetes manifests (GitOps)
│   ├── apps/                    # Application deployments by namespace
│   │   ├── ai/                  # AI services (Ollama, Open WebUI)
│   │   ├── cert-manager/        # TLS certificate management
│   │   ├── databases/           # PostgreSQL, MariaDB, Redis, MinIO, pgAdmin
│   │   ├── default/             # Personal apps (Homepage, Gitea, Outline, Immich, Vaultwarden, n8n, Echo)
│   │   ├── flux-system/         # Flux operator & instance
│   │   ├── kube-system/         # Core services (Cilium, CoreDNS, Metrics Server, Reloader)
│   │   ├── media/               # Media stack (*arr apps, Plex, Jellyfin, etc.)
│   │   ├── monitoring/          # Prometheus, Grafana, exporters (AdGuard, iLO, Proxmox, TrueNAS, WireGuard)
│   │   ├── network/             # Ingress (Envoy Gateway, Cloudflare Tunnel, Cloudflare DNS, k8s-gateway)
│   │   └── storage/             # Storage provisioners (NFS CSI, OpenEBS)
│   ├── components/              # Shared components (SOPS secrets)
│   └── flux/                    # Main Flux cluster Kustomization
├── bootstrap/                   # Initial cluster setup
│   └── helmfile.d/              # Helmfile configuration for bootstrap apps
│       ├── 00-crds.yaml         # CRD definitions
│       ├── 01-apps.yaml         # Application definitions
│       └── templates/           # Helmfile templates
│           └── values.yaml.gotmpl
├── talos/                       # Talos Linux configuration
│   ├── clusterconfig/           # Generated Talos machine configs (.gitignored)
│   ├── patches/                 # Talos configuration patches
│   │   ├── controller/          # Controller node patches
│   │   └── global/              # Global patches (network, kubelet, sysctls, time, files)
│   ├── talconfig.yaml           # Talos configuration
│   ├── talenv.yaml              # Talos environment variables
│   └── talsecret.sops.yaml      # Encrypted Talos secrets
├── templates/                   # Jinja2 templates for generating manifests
│   ├── config/                  # Configuration templates
│   │   ├── bootstrap/           # Bootstrap templates
│   │   ├── kubernetes/          # Kubernetes manifest templates
│   │   └── talos/               # Talos configuration templates
│   ├── overrides/               # Template override partials
│   └── scripts/                 # Template generation scripts
│       └── plugin.py            # Jinja2 plugin
├── scripts/                     # Utility scripts
│   ├── bootstrap-apps.sh        # Bootstrap applications script
│   ├── homepage/                # Homepage service discovery scripts
│   │   ├── discover-services.py
│   │   ├── fetch-stats.py
│   │   └── update-configmap.sh
│   └── lib/                     # Shared script libraries
│       └── common.sh
├── .taskfiles/                  # Modular Taskfile definitions
│   ├── bootstrap/               # Bootstrap tasks
│   ├── talos/                   # Talos operations
│   └── template/                # Template generation tasks
│       └── resources/           # Schema validation resources
│           ├── cluster.schema.cue
│           ├── nodes.schema.cue
│           └── kubeconform.sh
├── .github/                     # GitHub configuration
│   ├── workflows/               # CI/CD pipelines
│   │   ├── e2e.yaml             # End-to-end tests
│   │   ├── flux-local.yaml     # Flux local testing
│   │   ├── label-sync.yaml      # Label synchronization
│   │   ├── labeler.yaml         # PR labeler
│   │   └── release.yaml         # Release automation
│   ├── labels.yaml              # GitHub labels definition
│   ├── release.yaml             # Release configuration
│   └── tests/                   # Test workflows
├── docs/                        # Documentation
│   └── homepage-dashboard.md    # Homepage dashboard documentation
├── bootstrap/                   # Bootstrap secrets (SOPS encrypted)
│   └── sops-age.sops.yaml       # Age encryption key for SOPS
├── Taskfile.yaml                # Main Taskfile (task runner)
├── makejinja.toml               # Makejinja configuration
├── .mise.toml                   # Mise (tool version manager) config
├── .renovaterc.json5            # Renovate bot configuration
├── .editorconfig                # Editor configuration
├── .shellcheckrc                # ShellCheck configuration
├── .gitignore                   # Git ignore rules
└── README.md                    # Project documentation
```

## Key Directories

### `kubernetes/apps/`
Each application follows a consistent structure:
```
app-name/
├── ks.yaml                      # KustomizationSet (Flux app definition)
├── namespace.yaml               # Namespace definition
├── kustomization.yaml           # Namespace-level kustomization
└── app-name/                    # Application-specific directory
    ├── app/
    │   ├── helmrelease.yaml     # HelmRelease (if using Helm)
    │   ├── ocirepository.yaml   # OCI repository (if using OCI)
    │   ├── kustomization.yaml   # App-level kustomization
    │   ├── httproute.yaml       # HTTPRoute (Gateway API)
    │   ├── secret.sops.yaml     # Encrypted secrets (if needed)
    │   └── *.yaml               # Additional resources (PVCs, ConfigMaps, etc.)
    └── ks.yaml                  # KustomizationSet reference
```

### `templates/`
Jinja2 templates mirror the `kubernetes/` structure and are used to generate manifests from configuration. Templates are processed by Makejinja.

### `talos/`
Talos Linux configuration files:
- `talconfig.yaml`: Main Talos configuration
- `patches/`: Configuration patches applied to base config
- `clusterconfig/`: Generated machine configs (gitignored)

### `bootstrap/`
Initial cluster setup using Helmfile. Contains CRD definitions and application manifests for bootstrapping Flux and other critical components.

### `.taskfiles/`
Modular Taskfile definitions for different operational areas:
- `bootstrap/`: Cluster bootstrap tasks
- `talos/`: Talos upgrade and management tasks
- `template/`: Template generation and validation tasks
