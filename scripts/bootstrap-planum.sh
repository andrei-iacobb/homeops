#!/usr/bin/env bash
# Bootstrap Planum: create planum DB + user in shared Postgres (idempotent).
# Does NOT touch any other database. Safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

echo "=== Planum Database Bootstrap ==="
echo "This only creates the 'planum' database and 'planum' user if missing."
echo ""

SECRET_FILE="kubernetes/apps/databases/postgres/app/secret.sops.yaml"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not found — skipping live cluster bootstrap."
  echo "GitOps changes are in init-configmap.yaml for future PVC rebuilds."
  exit 0
fi

if ! kubectl get deploy -n databases postgres >/dev/null 2>&1; then
  echo "Postgres deployment not reachable — apply homeops changes first, then re-run."
  exit 1
fi

DB_PASS=""
if command -v sops >/dev/null 2>&1 && [[ -f "${SECRET_FILE}" ]]; then
  if grep -q "PLANUM_DB_PASSWORD:" "${SECRET_FILE}" 2>/dev/null; then
    DB_PASS=$(mise exec -- sops -d "${SECRET_FILE}" 2>/dev/null | grep "PLANUM_DB_PASSWORD:" | sed 's/.*: *//' | tr -d ' ' || true)
  fi
fi

if [[ -z "${DB_PASS}" ]]; then
  if [[ -n "${PLANUM_DB_PASSWORD:-}" ]]; then
    DB_PASS="${PLANUM_DB_PASSWORD}"
  else
    echo "PLANUM_DB_PASSWORD not in ${SECRET_FILE}."
    echo "Add it with sops, or run: PLANUM_DB_PASSWORD=\$(openssl rand -base64 32) $0"
    exit 1
  fi
fi

echo "Creating planum role (if missing)..."
kubectl exec -n databases deploy/postgres -- psql -U postgres -v ON_ERROR_STOP=1 -c \
  "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='planum') THEN CREATE USER planum WITH PASSWORD '${DB_PASS}'; END IF; END \$\$;"

echo "Creating planum database (if missing)..."
kubectl exec -n databases deploy/postgres -- psql -U postgres -v ON_ERROR_STOP=1 -c \
  "SELECT 'CREATE DATABASE planum OWNER planum' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='planum')\gexec"

echo "Granting schema privileges..."
kubectl exec -n databases deploy/postgres -- psql -U postgres -d planum -v ON_ERROR_STOP=1 -c \
  "GRANT ALL ON SCHEMA public TO planum;"

echo ""
echo "=== Planum database ready ==="
echo "Connection: postgresql://planum:<password>@postgres.databases.svc.cluster.local:5432/planum"
echo ""
echo "Next: deploy the Planum API (kubernetes/apps/default/planum/) and run migrations on first start."
