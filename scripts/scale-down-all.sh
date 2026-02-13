#!/usr/bin/env bash
# Scale all deployments and statefulsets to 0 replicas.
# Run when cluster is reachable: ./scripts/scale-down-all.sh
set -euo pipefail

if ! kubectl cluster-info &>/dev/null; then
  echo "Error: Cannot reach cluster. Ensure KUBECONFIG is set and the cluster is up."
  exit 1
fi

echo "Scaling down all workloads..."
for ns in $(kubectl get ns -o jsonpath='{.items[*].metadata.name}'); do
  for kind in deployment statefulset; do
    count=$(kubectl get "$kind" -n "$ns" -o name 2>/dev/null | wc -l)
    if [[ "$count" -gt 0 ]]; then
      echo "  $ns: scaling $count $kind(s) to 0"
      kubectl scale "$kind" --all --replicas=0 -n "$ns" 2>/dev/null || true
    fi
  done
done
echo "Done."
