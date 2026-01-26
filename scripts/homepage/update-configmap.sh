#!/usr/bin/env bash
# Update the homepage-stats-scripts ConfigMap with the latest scripts

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

echo "Updating homepage-stats-scripts ConfigMap..."

kubectl create configmap homepage-stats-scripts \
  --from-file=discover-services.py="${SCRIPT_DIR}/discover-services.py" \
  --from-file=fetch-stats.py="${SCRIPT_DIR}/fetch-stats.py" \
  -n default --dry-run=client -o yaml | kubectl apply -f -

echo "ConfigMap updated successfully!"
echo "To verify: kubectl get configmap -n default homepage-stats-scripts -o yaml"
