# Prod Cluster Scaffolding Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a standalone `~/prod` Git repo with the same Talos + Flux + Cilium architecture as homeops, scaffolded with core infrastructure only.

**Architecture:** Mirror of homeops — Talos Linux single node, Cilium CNI, Flux GitOps, OpenEBS storage, Envoy Gateway ingress, SOPS/Age encryption. Core namespaces only: kube-system, cert-manager, network, storage, flux-system.

**Tech Stack:** Talos Linux, Kubernetes, Cilium, Flux CD, CoreDNS, cert-manager, OpenEBS, Envoy Gateway, SOPS/Age, mise, Taskfile, helmfile

---

### Task 1: Create repo and install tooling

**Files:**
- Create: `~/prod/` (directory + git init)
- Create: `~/prod/.mise.toml`
- Create: `~/prod/.editorconfig`
- Create: `~/prod/.gitignore`

**Step 1: Create directory and initialize git**

```bash
mkdir -p ~/prod && cd ~/prod && git init
```

**Step 2: Create .mise.toml**

Copy from homeops verbatim — same tool versions ensure compatibility.

**Step 3: Create .editorconfig**

Copy from homeops verbatim.

**Step 4: Create .gitignore**

Copy from homeops verbatim.

**Step 5: Install tools via mise**

```bash
cd ~/prod && mise install
```

**Step 6: Commit**

```bash
git add -A && git commit -m "feat: initialize repo with mise tooling"
```

---

### Task 2: Create Taskfile and scripts

**Files:**
- Create: `~/prod/Taskfile.yaml`
- Create: `~/prod/.taskfiles/bootstrap/Taskfile.yaml`
- Create: `~/prod/.taskfiles/talos/Taskfile.yaml`
- Create: `~/prod/scripts/bootstrap-apps.sh`
- Create: `~/prod/scripts/lib/common.sh`

**Step 1: Create Taskfile.yaml**

Copy from homeops, remove the `monitoring:build-truenas-exporter` task and the `template` include (not needed for prod). Keep: reconcile, bootstrap include, talos include.

**Step 2: Create bootstrap and talos Taskfiles**

Copy from homeops verbatim — same bootstrap flow.

**Step 3: Create bootstrap scripts**

Copy `scripts/bootstrap-apps.sh` and `scripts/lib/common.sh` from homeops verbatim.

**Step 4: Make scripts executable**

```bash
chmod +x ~/prod/scripts/bootstrap-apps.sh
```

**Step 5: Commit**

```bash
git add -A && git commit -m "feat: add Taskfile and bootstrap scripts"
```

---

### Task 3: Create SOPS and encryption setup

**Files:**
- Create: `~/prod/.sops.yaml`
- Create: `~/prod/age.key` (gitignored)

**Step 1: Generate new Age key**

```bash
cd ~/prod && age-keygen -o age.key 2>&1 | tee /dev/stderr | grep "public key" | awk '{print $NF}'
```

Save the public key — it goes in `.sops.yaml`.

**Step 2: Create .sops.yaml**

Same structure as homeops but with the NEW age public key (not the homeops key).

```yaml
---
creation_rules:
  - path_regex: talos/.*\.sops\.ya?ml
    mac_only_encrypted: true
    age: "<NEW_AGE_PUBLIC_KEY>"
  - path_regex: (bootstrap|kubernetes)/.*\.sops\.ya?ml
    encrypted_regex: "^(data|stringData)$"
    mac_only_encrypted: true
    age: "<NEW_AGE_PUBLIC_KEY>"
stores:
  yaml:
    indent: 2
```

**Step 3: Commit**

```bash
git add .sops.yaml && git commit -m "feat: add SOPS encryption config"
```

Note: `age.key` is gitignored and must NOT be committed.

---

### Task 4: Create Talos configuration

**Files:**
- Create: `~/prod/talos/talconfig.yaml`
- Create: `~/prod/talos/talenv.yaml`
- Create: `~/prod/talos/patches/global/machine-kubelet.yaml`
- Create: `~/prod/talos/patches/global/machine-network.yaml`
- Create: `~/prod/talos/patches/global/machine-sysctls.yaml`
- Create: `~/prod/talos/patches/global/machine-time.yaml`
- Create: `~/prod/talos/patches/global/machine-files.yaml`
- Create: `~/prod/talos/patches/controller/cluster.yaml`

**Step 1: Create talenv.yaml**

Copy from homeops — same Talos/K8s versions.

**Step 2: Create talconfig.yaml with placeholders**

```yaml
---
clusterName: prod

talosVersion: "${talosVersion}"
kubernetesVersion: "${kubernetesVersion}"

endpoint: https://REPLACE_NODE_IP:6443
additionalApiServerCertSans: &sans
  - "127.0.0.1"
  - "REPLACE_NODE_IP"
additionalMachineCertSans: *sans

clusterPodNets: ["10.69.0.0/16"]
clusterSvcNets: ["10.96.0.0/16"]

cniConfig:
  name: none

nodes:
  - hostname: "prod-node"
    ipAddress: "REPLACE_NODE_IP"
    installDisk: "/dev/sda"
    machineSpec:
      secureboot: false
    talosImageURL: factory.talos.dev/installer/REPLACE_SCHEMATIC_ID
    controlPlane: true
    networkInterfaces:
      - interface: "REPLACE_INTERFACE"
        dhcp: false
        addresses:
          - "REPLACE_NODE_IP/24"
        routes:
          - gateway: "REPLACE_GATEWAY_IP"
            network: 0.0.0.0/0
        mtu: 1500

patches:
  - "@./patches/global/machine-files.yaml"
  - "@./patches/global/machine-kubelet.yaml"
  - "@./patches/global/machine-network.yaml"
  - "@./patches/global/machine-sysctls.yaml"
  - "@./patches/global/machine-time.yaml"

controlPlane:
  patches:
    - "@./patches/controller/cluster.yaml"
```

Use different pod/service CIDRs from homeops to avoid conflicts if clusters ever peer.

**Step 3: Create machine patches**

Copy all patches from homeops. Adapt `machine-kubelet.yaml` to use a generic nodeIP subnet (`REPLACE_SUBNET/24`). Adapt `controller/cluster.yaml` etcd advertisedSubnets to use placeholder.

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: add Talos configuration with placeholders"
```

---

### Task 5: Create bootstrap helmfile

**Files:**
- Create: `~/prod/bootstrap/helmfile.d/00-crds.yaml`
- Create: `~/prod/bootstrap/helmfile.d/01-apps.yaml`
- Create: `~/prod/bootstrap/helmfile.d/templates/values.yaml.gotmpl`

**Step 1: Create CRDs helmfile**

Same as homeops — Envoy Gateway and External DNS CRDs. Remove kube-prometheus-stack (no monitoring).

```yaml
---
helmDefaults:
  args:
    - --include-crds
    - --no-hooks

releases:
  - name: cloudflare-dns
    namespace: network
    chart: oci://ghcr.io/home-operations/charts-mirror/external-dns
    version: 1.20.0

  - name: envoy-gateway
    namespace: network
    chart: oci://mirror.gcr.io/envoyproxy/gateway-helm
    version: v1.7.0
```

**Step 2: Create apps helmfile**

Same as homeops — Cilium, CoreDNS, cert-manager, Flux operator + instance.

**Step 3: Create values template**

```
{{- (fromYaml (readFile (printf "../../../kubernetes/apps/%s/%s/app/helmrelease.yaml" .Release.Namespace .Release.Name))).spec.values | toYaml }}
```

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: add bootstrap helmfile for core infrastructure"
```

---

### Task 6: Create Flux and SOPS component

**Files:**
- Create: `~/prod/kubernetes/flux/cluster/ks.yaml`
- Create: `~/prod/kubernetes/components/sops/kustomization.yaml`
- Create: `~/prod/kubernetes/components/sops/cluster-secrets.sops.yaml` (placeholder)

**Step 1: Create root Flux Kustomization**

Copy from homeops. Set `suspend: true` initially (unsuspend after bootstrap).

**Step 2: Create SOPS component**

Copy kustomization.yaml from homeops.

**Step 3: Create placeholder cluster-secrets**

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: cluster-secrets
  namespace: flux-system
stringData:
  SECRET_DOMAIN: "prod.example.com"
```

Then encrypt: `sops -e -i cluster-secrets.sops.yaml`

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: add Flux root kustomization and SOPS component"
```

---

### Task 7: Create kube-system namespace apps

**Files:**
- Create: `~/prod/kubernetes/apps/kube-system/namespace.yaml`
- Create: `~/prod/kubernetes/apps/kube-system/kustomization.yaml`
- Create: `~/prod/kubernetes/apps/kube-system/cilium/` (ks.yaml + app/)
- Create: `~/prod/kubernetes/apps/kube-system/coredns/` (ks.yaml + app/)
- Create: `~/prod/kubernetes/apps/kube-system/metrics-server/` (ks.yaml + app/)
- Create: `~/prod/kubernetes/apps/kube-system/reloader/` (ks.yaml + app/)

**Step 1: Create namespace.yaml**

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: kube-system
```

**Step 2: Create kustomization.yaml**

Same as homeops — references SOPS component and all app ks.yaml files.

**Step 3: Copy app directories**

Copy each app (cilium, coredns, metrics-server, reloader) from homeops verbatim. These are chart configs with no cluster-specific values (chart versions, feature flags).

**Step 4: Commit**

```bash
git add -A && git commit -m "feat: add kube-system apps (cilium, coredns, metrics-server, reloader)"
```

---

### Task 8: Create cert-manager namespace

**Files:**
- Create: `~/prod/kubernetes/apps/cert-manager/namespace.yaml`
- Create: `~/prod/kubernetes/apps/cert-manager/kustomization.yaml`
- Create: `~/prod/kubernetes/apps/cert-manager/cert-manager/` (ks.yaml + app/)

**Step 1: Copy cert-manager from homeops**

Copy the full cert-manager directory structure. The `secret.sops.yaml` and `clusterissuer.yaml` reference Cloudflare — create placeholders that can be filled in later.

**Step 2: Create placeholder secret**

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: cert-manager-secret
  namespace: cert-manager
stringData:
  api-token: "REPLACE_CLOUDFLARE_API_TOKEN"
```

Encrypt with SOPS.

**Step 3: Commit**

```bash
git add -A && git commit -m "feat: add cert-manager with placeholder secrets"
```

---

### Task 9: Create network namespace (Envoy Gateway only)

**Files:**
- Create: `~/prod/kubernetes/apps/network/namespace.yaml`
- Create: `~/prod/kubernetes/apps/network/kustomization.yaml`
- Create: `~/prod/kubernetes/apps/network/envoy-gateway/` (ks.yaml + app/)

**Step 1: Create namespace and kustomization**

Only include envoy-gateway (no cloudflare-ddns, cloudflare-dns, cloudflare-tunnel, k8s-gateway — those depend on domain and Cloudflare setup).

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: network

components:
  - ../../components/sops

resources:
  - ./namespace.yaml
  - ./envoy-gateway/ks.yaml
```

**Step 2: Copy envoy-gateway from homeops**

Adapt: remove domain-specific certificates, use placeholder hostnames. Keep the core Envoy Gateway helmrelease and gateway definitions.

**Step 3: Commit**

```bash
git add -A && git commit -m "feat: add network namespace with envoy-gateway"
```

---

### Task 10: Create storage namespace (OpenEBS only)

**Files:**
- Create: `~/prod/kubernetes/apps/storage/namespace.yaml`
- Create: `~/prod/kubernetes/apps/storage/kustomization.yaml`
- Create: `~/prod/kubernetes/apps/storage/openebs/` (ks.yaml + app/)

**Step 1: Create namespace and kustomization**

Only include OpenEBS (no NFS CSI, no VolSync).

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: openebs

components:
  - ../../components/sops

resources:
  - ./namespace.yaml
  - ./openebs/ks.yaml
```

**Step 2: Copy OpenEBS from homeops verbatim**

Same chart config, same hostpath basePath.

**Step 3: Commit**

```bash
git add -A && git commit -m "feat: add storage namespace with openebs"
```

---

### Task 11: Create flux-system namespace

**Files:**
- Create: `~/prod/kubernetes/apps/flux-system/namespace.yaml`
- Create: `~/prod/kubernetes/apps/flux-system/kustomization.yaml`
- Create: `~/prod/kubernetes/apps/flux-system/flux-operator/` (ks.yaml + app/)
- Create: `~/prod/kubernetes/apps/flux-system/flux-instance/` (ks.yaml + app/)

**Step 1: Copy flux-system from homeops**

Copy flux-operator and flux-instance. The flux-instance has a secret.sops.yaml for GitHub deploy key — create a placeholder.

**Step 2: Adapt flux-instance**

Remove the httproute (no ingress for Flux UI yet) and receiver (no webhook yet). Keep the core helmrelease.

**Step 3: Commit**

```bash
git add -A && git commit -m "feat: add flux-system apps (operator + instance)"
```

---

### Task 12: Create Renovate config and CLAUDE.md

**Files:**
- Create: `~/prod/.renovaterc.json5`
- Create: `~/prod/CLAUDE.md`

**Step 1: Create .renovaterc.json5**

Copy from homeops, remove references to personal container images (ghcr.io/andrei-iacobb).

**Step 2: Create CLAUDE.md**

Write a prod-specific CLAUDE.md with the same structure as homeops but referencing prod-specific details (different CIDRs, placeholder domain, no monitoring namespace).

**Step 3: Commit**

```bash
git add -A && git commit -m "feat: add Renovate config and CLAUDE.md"
```

---

### Task 13: Validate and final commit

**Step 1: Validate YAML**

```bash
cd ~/prod && find . -name "*.yaml" -not -path "./.git/*" | xargs -I {} yq eval '.' {} > /dev/null
```

**Step 2: Validate talconfig**

```bash
cd ~/prod && talhelper validate talconfig
```

**Step 3: Review directory structure**

```bash
find ~/prod -type f -not -path "*/.git/*" | sort
```

**Step 4: Final commit if any fixes needed**

```bash
git add -A && git commit -m "fix: validation fixes"
```
