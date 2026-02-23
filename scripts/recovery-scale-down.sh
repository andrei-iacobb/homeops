#!/usr/bin/env bash
# Recovery script: scale down app deployments to reduce cluster load.
# Run when cluster is overwhelmed (etcd slow, scheduler crashlooping).
# After control plane stabilizes, run recovery-scale-up.sh

set -euo pipefail

# Deployments to keep running (critical for cluster function)
KEEP="coredns metrics-server reloader nfs-subdir-external-provisioner"
KEEP_RE="nfs-subdir-external-provisioner|openebs-localpv-provisioner"

echo "Scaling down non-critical deployments..."
for ns in $(kubectl get ns -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  for dep in $(kubectl get deployments -n "$ns" -o name 2>/dev/null | cut -d/ -f2); do
    if echo " $KEEP " | grep -q " $dep " || echo "$dep" | grep -qE "$KEEP_RE"; then
      echo "  [SKIP] $ns/$dep"
      continue
    fi
    echo "  [0] $ns/$dep"
    kubectl scale deployment -n "$ns" "$dep" --replicas=0 --timeout=15s 2>/dev/null || true
  done
done
echo "Done. Wait 2-3 min for pods to terminate, then: flux reconcile kustomization cluster-apps --with-source"
