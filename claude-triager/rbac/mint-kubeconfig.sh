#!/bin/bash
set -euo pipefail

# mint-kubeconfig.sh: Create long-lived kubeconfigs for claude-triager ServiceAccounts
# Usage: ./mint-kubeconfig.sh
# Output: ./claude-triager-ro.kubeconfig, ./claude-triager-rw.kubeconfig
#
# This script:
# 1. Creates a Secret of type kubernetes.io/service-account-token for EACH SA
# 2. Waits for the Kubernetes token controller to populate tokens
# 3. Extracts tokens and CA certificate
# 4. Builds two standalone kubeconfigs
#
# NOTE: Token rotation recommended quarterly (re-run this script).

SA_NAMESPACE="monitoring"
SA_RO_NAME="claude-triager-ro"
SA_RW_NAME="claude-triager-rw"
CLUSTER_NAME="home-cluster"
CLUSTER_SERVER="https://192.168.1.85:6443"
OUTPUT_FILE_RO="./claude-triager-ro.kubeconfig"
OUTPUT_FILE_RW="./claude-triager-rw.kubeconfig"
SECRET_NAME_RO="${SA_RO_NAME}-token"
SECRET_NAME_RW="${SA_RW_NAME}-token"

TIMEOUT=30

# Function to create and populate a token secret for a given SA
create_token_secret() {
  local sa_name=$1
  local secret_name=$2
  
  echo "[*] Creating long-lived token secret for ${sa_name}..."
  
  cat <<SECRETEOF | kubectl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: ${secret_name}
  namespace: ${SA_NAMESPACE}
  annotations:
    kubernetes.io/service-account.name: ${sa_name}
type: kubernetes.io/service-account-token
SECRETEOF

  echo "[*] Waiting for token controller to populate the secret..."
  
  local ELAPSED=0
  local TOKEN=""
  
  while [ $ELAPSED -lt $TIMEOUT ]; do
    TOKEN=$(kubectl -n "${SA_NAMESPACE}" get secret "${secret_name}" -o jsonpath='{.data.token}' 2>/dev/null || echo "")
    
    if [ -n "$TOKEN" ]; then
      echo "[+] Token populated (after ${ELAPSED}s)"
      echo "$TOKEN"
      return 0
    fi
    
    ELAPSED=$((ELAPSED + 1))
    if [ $((ELAPSED % 5)) -eq 0 ]; then
      echo "  ... waiting (${ELAPSED}/${TIMEOUT}s)"
    fi
    sleep 1
  done
  
  echo "[!] ERROR: Token not populated after ${TIMEOUT} seconds for ${sa_name}"
  echo "    Debugging: kubectl -n ${SA_NAMESPACE} describe secret ${secret_name}"
  exit 1
}

# Function to extract CA certificate from a secret
get_ca_cert() {
  local secret_name=$1
  kubectl -n "${SA_NAMESPACE}" get secret "${secret_name}" -o jsonpath='{.data.ca\.crt}'
}

# Function to build a kubeconfig
build_kubeconfig() {
  local sa_name=$1
  local token_decoded=$2
  local ca_cert=$3
  local output_file=$4
  
  cat > "${output_file}" <<KUBECONFIG_EOF
apiVersion: v1
kind: Config
clusters:
  - cluster:
      certificate-authority-data: ${ca_cert}
      server: ${CLUSTER_SERVER}
    name: ${CLUSTER_NAME}
contexts:
  - context:
      cluster: ${CLUSTER_NAME}
      user: ${sa_name}
    name: ${CLUSTER_NAME}
current-context: ${CLUSTER_NAME}
users:
  - name: ${sa_name}
    user:
      token: ${token_decoded}
KUBECONFIG_EOF

  chmod 600 "${output_file}"
  echo "[+] Kubeconfig written to: ${output_file}"
}

# ========== CREATE RO TOKEN ==========
echo ""
echo "========== CLAUDE-TRIAGER-RO (READ-ONLY) =========="
TOKEN_RO=$(create_token_secret "${SA_RO_NAME}" "${SECRET_NAME_RO}")
TOKEN_RO_DECODED=$(echo "$TOKEN_RO" | base64 -d)
CA_CERT=$(get_ca_cert "${SECRET_NAME_RO}")

if [ -z "$CA_CERT" ]; then
  echo "[!] ERROR: CA certificate not found in secret ${SECRET_NAME_RO}"
  exit 1
fi

build_kubeconfig "${SA_RO_NAME}" "${TOKEN_RO_DECODED}" "${CA_CERT}" "${OUTPUT_FILE_RO}"

# ========== CREATE RW TOKEN ==========
echo ""
echo "========== CLAUDE-TRIAGER-RW (READ-WRITE) =========="
TOKEN_RW=$(create_token_secret "${SA_RW_NAME}" "${SECRET_NAME_RW}")
TOKEN_RW_DECODED=$(echo "$TOKEN_RW" | base64 -d)
CA_CERT=$(get_ca_cert "${SECRET_NAME_RW}")

if [ -z "$CA_CERT" ]; then
  echo "[!] ERROR: CA certificate not found in secret ${SECRET_NAME_RW}"
  exit 1
fi

build_kubeconfig "${SA_RW_NAME}" "${TOKEN_RW_DECODED}" "${CA_CERT}" "${OUTPUT_FILE_RW}"

# ========== SUMMARY AND INSTRUCTIONS ==========
echo ""
echo "========== DEPLOYMENT CHECKLIST =========="
echo ""
echo "BOTH kubeconfigs are ready (600 permissions):"
echo "  - ${OUTPUT_FILE_RO}"
echo "  - ${OUTPUT_FILE_RW}"
echo ""
echo "Copy BOTH to the LXC:"
echo "  scp ./${OUTPUT_FILE_RO} <lxc-user>@<lxc-ip>:/opt/claude-triager/kubeconfig-ro"
echo "  scp ./${OUTPUT_FILE_RW} <lxc-user>@<lxc-ip>:/opt/claude-triager/kubeconfig-rw"
echo ""
echo "Set restrictive permissions on both files in the LXC:"
echo "  chmod 600 /opt/claude-triager/kubeconfig-ro"
echo "  chmod 600 /opt/claude-triager/kubeconfig-rw"
echo "  chown triager /opt/claude-triager/kubeconfig-{ro,rw}"
echo ""
echo "Test both kubeconfigs (from LXC):"
echo "  kubectl --kubeconfig=/opt/claude-triager/kubeconfig-ro get nodes"
echo "  kubectl --kubeconfig=/opt/claude-triager/kubeconfig-rw get pods -n media"
echo ""
echo "========== PERMISSIONS SUMMARY =========="
echo ""
echo "RO Kubeconfig (claude-triager-ro):"
echo "  - READ: cluster-wide (pods, logs, events, nodes, Flux CRDs, metrics, etc.)"
echo "  - NO write access anywhere"
echo "  - NO access to secrets or configmaps"
echo ""
echo "RW Kubeconfig (claude-triager-rw):"
echo "  - READ: cluster-wide (same as RO)"
echo "  - WRITE: ONLY in media, default, monitoring namespaces"
echo "    * Pod deletion, deployment/statefulset/daemonset restart (patch)"
echo "    * Flux reconcile (patch helmrelease/kustomization)"
echo "    * Job deletion"
echo ""
echo "========== HARD BOUNDARIES =========="
echo ""
echo "ENFORCED by RBAC (no exceptions):"
echo "  - NO access to secrets, configmaps"
echo "  - NO write in: kube-system, cert-manager, network, storage, openebs,"
echo "    databases, flux-system, cilium-secrets, external-services, volsync-system"
echo "  - NO node operations, PVC deletion, workload scaling, resource limits changes"
echo ""
echo "========== TOKEN ROTATION =========="
echo ""
echo "Tokens are long-lived. For rotation (recommended quarterly):"
echo "  1. Delete the old secrets in monitoring namespace:"
echo "     kubectl -n monitoring delete secret ${SECRET_NAME_RO} ${SECRET_NAME_RW}"
echo "  2. Re-run this script to mint new tokens"
echo "  3. Copy new kubeconfigs to LXC and restart the bot"
