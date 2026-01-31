#!/usr/bin/env bash
# Bootstrap Paperless NGX: create DB (if needed), reconcile Flux, wait for readiness.
# Run from repo root with cluster access (kubectl, flux).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

echo "=== Paperless NGX Bootstrap ==="

# 1. Create paperless DB in Postgres (idempotent - safe if already exists)
echo "Creating paperless database and user (if not exists)..."
DB_PASS=$(mise exec -- sops -d kubernetes/apps/databases/postgres/app/secret.sops.yaml 2>/dev/null | grep "PAPERLESS_DB_PASSWORD:" | sed 's/.*: *//' | tr -d ' ')
kubectl exec -n databases deploy/postgres -- psql -U postgres -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='paperless') THEN CREATE USER paperless WITH PASSWORD '${DB_PASS}'; END IF; END \$\$;" 2>/dev/null || true
kubectl exec -n databases deploy/postgres -- psql -U postgres -v ON_ERROR_STOP=1 -c "SELECT 'CREATE DATABASE paperless OWNER paperless' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='paperless')\gexec" 2>/dev/null || true
kubectl exec -n databases deploy/postgres -- psql -U postgres -d paperless -v ON_ERROR_STOP=1 -c "GRANT ALL ON SCHEMA public TO paperless;" 2>/dev/null || true
echo "  Done."

# 2. Reconcile Flux
echo "Reconciling Flux..."
flux reconcile kustomization cluster-apps --with-source 2>/dev/null || task reconcile 2>/dev/null || echo "  (flux/task not found, skip)"
sleep 5
flux reconcile kustomization paperless -n flux-system --with-source 2>/dev/null || true
flux reconcile helmrelease paperless -n default --with-source 2>/dev/null || true

# 3. Wait for Paperless pod
echo "Waiting for Paperless pod to be ready..."
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=paperless -n default --timeout=300s 2>/dev/null || \
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=paperless -n default --timeout=300s 2>/dev/null || \
echo "  (wait skipped - check pod status manually)"

echo ""
echo "=== Paperless is ready ==="
