# Homeops Claude Code Guidelines

This document contains the comprehensive guidelines for working with this homelab Kubernetes cluster managed by Flux CD, running on Talos Linux.

## Critical Rules - NEVER Touch Automatically

**Manual Updates Only:**
- Talos OS version - Manual updates only
- Kubernetes version - Manual updates only
- If a package update requires a newer Kubernetes version, do not proceed - flag it for manual review

**Before Any Change:**
- This is a LIVE cluster - every merge affects production
- Validate YAML syntax before committing
- Check for breaking changes in release notes
- Ensure secrets are properly encrypted (.sops.yaml suffix)

## Project Structure

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
└── Taskfile.yaml                # Main Taskfile (task runner)
```

### Standard Application Structure

Each application follows this consistent structure:
```
app-name/
├── ks.yaml                      # Flux Kustomization
└── app/
    ├── kustomization.yaml       # Resource aggregator
    ├── helmrelease.yaml         # Helm deployment
    ├── helm-values.yaml         # Helm values (optional)
    └── secret.sops.yaml         # Encrypted secrets (optional)
```

## Homelab Follow-Through

**Complete homelab tasks end-to-end without stopping for user input.**

When working on homelab tasks (app deployment, config changes, upgrades, etc.), complete the full workflow yourself. Do not pause to ask the user to run commands.

### End-to-End Completion

- **App deployments**: From manifests to encrypted secrets to Flux reconcile — run every step until the app is fully deployed and reconciled.
- **Config changes**: Apply changes, run validation, and trigger reconciliation.
- **Secrets**: Generate values, encrypt, and run sops yourself — never ask the user to run sops.

### Secret Generation & Encryption

- **Random secrets** (passwords, tokens): Generate with OpenSSL:
  ```bash
  openssl rand -base64 32
  ```
- **SOPS encryption**: Run sops yourself after creating/editing secret files:
  ```bash
  sops -e -i path/to/secret.sops.yaml
  ```
- Do not ask the user to run `sops`, `sops -e -i`, or similar; execute these commands in the terminal.

### Commands to Run Yourself

- `sops -e -i <file>` — Encrypt new/edited secret files
- `task reconcile` or `flux reconcile kustomization/helmrelease ...` — Force Flux reconciliation
- `flux get all -A` — Verify deployment status
- `kubectl get pods -n <namespace>` — Confirm pods are running
- Any validation (YAML lint, helm template, etc.)

### When to Stop

Only pause if you need input the user must provide (e.g., hostname, namespace choice, or an API key from an external service that cannot be generated). For everything else—secret generation, sops encryption, reconciliation, verification—run it yourself.

## Homelab Maintenance

When running homelab maintenance, do the following in order. Use `mise exec --` (or ensure `KUBECONFIG` and tools like `flux`, `kubectl`, `kubeconform` are available) when commands are not in PATH.

### 1. Get pods and diagnose problems

- **List all pods:** `kubectl get pods -A`
- **Non-running / problematic pods:** `kubectl get pods -A | grep -v Running` (and/or filter for `Error`, `CrashLoopBackOff`, `ImagePullBackOff`, `Pending`)
- For any **crashing** or **frequently restarted** pods, find out why:
  - `kubectl describe pod <name> -n <namespace>`
  - `kubectl logs -n <namespace> <pod-name> --previous` (if restarted)
  - `kubectl logs -n <namespace> <pod-name> -f` (current logs)
- Fix root cause (config, image, secrets, resources, etc.) and re-check.

### 2. Validate manifests (repo-side)

- **Kubeconform:** `mise exec -- bash .taskfiles/template/resources/kubeconform.sh kubernetes`
- Fix any validation errors before proceeding.

### 3. Flux reconciliation

- **Force Flux to pull changes:** `task reconcile` or `flux reconcile kustomization flux-system --with-source`
- **Check all Flux resources:** `flux get all -A`
- Fix any **failing** kustomizations or helm releases (suspend/resume, fix values or sources, then reconcile again).

### 4. Pods again and fix remaining issues

- **Non-running pods:** `kubectl get pods -A | grep -v Running`
- For **CrashLoopBackOff:** use `describe` and `logs` (including `--previous`) to identify cause; fix config/image/resources and redeploy if needed.
- For **ImagePullBackOff:** fix image name, tag, or pull secret.
- For **Pending:** check `kubectl describe pod` for scheduling (resources, PVCs, taints); fix or scale.

Run these steps where cluster access is available; kubeconform can run without cluster access.

## Installing New Applications

The user may provide various input formats:
- Just an app name: "install jellyfin"
- GitHub project link: "install https://github.com/jellyfin/jellyfin"
- KubeSearch link: "use this https://kubesearch.dev/?search=jellyfin"
- Other home cluster links: "use this as reference https://github.com/someone/home-cluster/tree/main/kubernetes/apps/media/jellyfin"
- Multiple references: Combination of the above

### Step 1: Research Phase

**If given just an app name:**
- Search https://kubesearch.dev/ for the app to find community implementations
- Find the official project GitHub/docs
- Look for official Helm charts

**If given a GitHub project link:**
- Check if they have an official Helm chart
- Search kubesearch.dev for community implementations
- Review their documentation for Kubernetes deployment

**If given a KubeSearch link:**
- Open the link and review the community implementations shown
- Pick well-structured examples that match this cluster's patterns
- Cross-reference with official documentation

**If given another home cluster reference:**
- Fetch and analyze their implementation
- Adapt to match this cluster's conventions
- Verify chart versions and values are current

### Step 2: Ask Required Questions (Unless Already Provided)

ALWAYS ask these questions before proceeding (skip if user already specified):

1. **Namespace:** "Which namespace should this go in?"
   - Suggest based on app type (media, downloads, observability, default, etc.)
   - List existing namespaces as options

2. **Ingress/Access:** "Do you want this publicly accessible or internal only?"
   - external = Public via Cloudflare tunnel (accessible from internet)
   - internal = Internal network only (home network access)
   - Or no ingress at all

3. **Hostname:** "What hostname/subdomain do you want?"
   - e.g., jellyfin.davidhome.ro for external
   - e.g., jellyfin.internal for internal

**Example questions:**

```
Before I set up Jellyfin, I need to know:

1. Namespace: Where should this live?
   - media (recommended, alongside Plex/Sonarr/Radarr)
   - default
   - Other?

2. Access: Public or private?
   - External (public via Cloudflare - accessible from anywhere)
   - Internal (home network only)

3. Hostname: What subdomain?
   - e.g., jellyfin.davidhome.ro
```

If the user provided these details upfront, skip the questions and proceed.

### Step 3: Present Implementation Plan

After getting answers (or if already provided), present the full plan:
- Namespace: Confirmed choice
- Helm chart: Which chart and version to use
- Ingress configuration: Class and hostname
- Storage: What volumes/mounts are needed
- Secrets: What secrets are required (if any)
- Dependencies: Other apps or resources required
- Reference sources: What implementations were used as reference

**Example:**

```
Here's the plan for Jellyfin:

- Namespace: media
- Helm chart: bjw-s/app-template v3.x
- Ingress: internal class, hostname jellyfin.internal
- Storage: NFS mount for media library at /media
- Secrets: None required for basic setup
- GPU: Will enable NVIDIA transcoding

References used:
- kubesearch.dev example from user X
- Official Jellyfin docs

Should I proceed with creating the files?
```

Wait for approval before creating any files.

### Step 4: Implementation (After Approval)

1. Create app directory: `kubernetes/apps/<namespace>/<app-name>/`
2. Create `ks.yaml` (Flux Kustomization)
3. Create `app/kustomization.yaml`
4. Create `app/helmrelease.yaml`
5. Create `app/helm-values.yaml` if needed
6. Create `app/secret.sops.yaml` if secrets required

### Step 5: Encrypt Secrets (if any)

```bash
sops -e -i kubernetes/apps/<namespace>/<app>/app/secret.sops.yaml
```

### Step 6: Validate

- YAML syntax is correct
- Helm repository exists in `kubernetes/flux/meta/repositories/`
- Namespace exists or add to parent kustomization
- All referenced secrets/configmaps exist

### Step 7: Final Review

Present the created files for review before committing. Ask: "Here are the files I've created. Should I commit these changes?"

## Homepage and Monitoring Integration

**Every service you add:**
- Add it to homepage as a widget, if not possible then add it as an app

**Whenever a new homelab service is added:**
- Add it to homepage
- Add to monitoring if possible
- Create a table at the end with the URL and login credentials

## Local API Keys (gitignored)

- **`secrets/api-keys.yaml`** is gitignored. The user stores real API keys there so the agent can read them when needed (e.g. to know what to document or when guiding sops edits). Never commit this file.
- **`secrets/api-keys.yaml.example`** is committed. It lists all API key names used by apps and where to get them (e.g. "Sonarr > Settings > General > API Key").
- When adding a new app that needs an API key in a SOPS secret:
  1. Add the key name(s) to the app's secret.sops.yaml (with empty or placeholder value).
  2. Add the same key(s) and a short "where to get it" comment to `secrets/api-keys.yaml.example`.
  3. Tell the user to add the value to `secrets/api-keys.yaml` and run `sops <secret-file>` to encrypt.
- When the user asks to set or update a secret that uses keys from the local file, you can read `secrets/api-keys.yaml` (if present) to see the values and guide them, or they can copy from that file when running sops.

## Command Reference

### Flux Commands

```bash
# Check all Flux resources
flux get all -A

# Reconcile a kustomization
flux reconcile kustomization <name> --with-source

# Reconcile a helmrelease
flux reconcile helmrelease <name> -n <namespace>

# Suspend a helmrelease (pause updates)
flux suspend helmrelease <name> -n <namespace>

# Resume a helmrelease
flux resume helmrelease <name> -n <namespace>

# View Flux logs
flux logs -A --follow

# Check source status
flux get sources all -A
```

### Kubectl Commands

```bash
# Get all pods in a namespace
kubectl get pods -n <namespace>

# Restart a deployment (rollout restart)
kubectl rollout restart deployment/<name> -n <namespace>

# Scale a deployment down/up
kubectl scale deployment/<name> --replicas=0 -n <namespace>
kubectl scale deployment/<name> --replicas=1 -n <namespace>

# View logs
kubectl logs -n <namespace> <pod-name> -f

# Describe a resource
kubectl describe pod/<name> -n <namespace>

# Get events
kubectl get events -n <namespace> --sort-by='.lastTimestamp'

# Execute into a pod
kubectl exec -it -n <namespace> <pod-name> -- /bin/sh
```

### Helm Commands

```bash
# List releases
helm list -A

# Get values from a release
helm get values <release> -n <namespace>

# Show chart values
helm show values <chart>
```

### SOPS Commands

```bash
# Decrypt a secret (view only)
sops -d <file.sops.yaml>

# Encrypt a new secret file
sops -e -i <file.sops.yaml>

# Edit encrypted file in place
sops <file.sops.yaml>
```

### Talos Commands

```bash
# Force Flux reconciliation
task reconcile

# Generate Talos config
task talos:generate-config

# Bootstrap apps
task bootstrap:apps
```

### Quick Reference Commands

```bash
# Decrypt a secret
sops -d <file.sops.yaml>

# Encrypt a secret
sops -e -i <file.sops.yaml>

# Force Flux reconciliation
task reconcile

# Check Flux status
flux get all -A

# View Talos config
task talos:generate-config

# List open PRs
gh pr list --state open

# Restart a stuck deployment
kubectl rollout restart deployment/<name> -n <namespace>

# Check why a pod is failing
kubectl describe pod/<name> -n <namespace>
kubectl logs -n <namespace> <pod-name>
```
