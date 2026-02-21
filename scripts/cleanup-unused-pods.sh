#!/usr/bin/env bash
# Remove unused pods: Succeeded (Completed) and Failed (Evicted, OOMKilled, etc.).
# Safe to run periodically—only deletes pods that have already terminated.

set -euo pipefail

echo "Finding terminated pods (Succeeded/Failed)..."
PODS=$(kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Pending -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null || true)

if [[ -z "$PODS" ]]; then
  echo "No unused pods found."
  exit 0
fi

COUNT=$(echo "$PODS" | wc -l | tr -d ' ')
echo "Deleting $COUNT unused pod(s):"
echo "$PODS" | while read -r line; do
  ns="${line%%/*}"
  name="${line##*/}"
  echo "  Deleting $ns/$name"
  kubectl delete pod -n "$ns" "$name" --grace-period=0 --force 2>/dev/null || true
done
echo "Done."
