#!/bin/sh
# Inject the iacob.co.uk Entra app credentials (secret wazuh-m365) into the
# office365 and ms-graph blocks of ossec.conf. Runs on every start, after the
# image copies /wazuh-config-mount over /var/ossec, so git only holds placeholders.
set -eu
src=/etc/wazuh-m365
conf=/var/ossec/etc/ossec.conf
[ -r "$src/client_secret" ] || { echo "20-m365: no credentials mounted, skipping"; exit 0; }
install -m 640 -o root -g wazuh "$src/client_secret" /var/ossec/etc/m365.secret
esc() { sed -e 's/[&|\\]/\\&/g' "$1"; }
sed -i \
  -e "s|@@M365_TENANT_ID@@|$(esc "$src/tenant_id")|g" \
  -e "s|@@M365_CLIENT_ID@@|$(esc "$src/client_id")|g" \
  -e "s|@@M365_CLIENT_SECRET@@|$(esc "$src/client_secret")|g" \
  "$conf"
