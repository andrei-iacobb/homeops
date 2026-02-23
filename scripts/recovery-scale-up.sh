#!/usr/bin/env bash
# Recovery script: scale deployments back to 1 after cluster stabilizes.
# Run after recovery-scale-down.sh and control plane is healthy.

set -euo pipefail

NAMESPACES="ai default databases media monitoring network cert-manager"

echo "Scaling up deployments in: $NAMESPACES"
for ns in $NAMESPACES; do
  for dep in $(kubectl get deployments -n "$ns" -o name 2>/dev/null | cut -d/ -f2); do
    echo "  [SCALE 1] $ns/$dep"
    kubectl scale deployment -n "$ns" "$dep" --replicas=1 --timeout=30s 2>/dev/null || true
  done
done
echo "Done. Monitor: kubectl get pods -A -w"
