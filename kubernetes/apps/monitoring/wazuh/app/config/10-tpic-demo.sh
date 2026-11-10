#!/bin/bash
# Runs inside the manager before the Wazuh daemons start (image hook:
# /entrypoint-scripts/*.sh). The image copies /wazuh-config-mount into
# /var/ossec as root, so give the copied files the ownership Wazuh expects;
# analysisd runs as the wazuh user and skips rule files it cannot read.
set -eu
chown wazuh:wazuh /var/ossec/etc/decoders/tpic_demo_decoders.xml /var/ossec/etc/rules/tpic_demo_rules.xml
chmod 660 /var/ossec/etc/decoders/tpic_demo_decoders.xml /var/ossec/etc/rules/tpic_demo_rules.xml
chown root:wazuh /var/ossec/etc/authd.pass
chmod 640 /var/ossec/etc/authd.pass
# Synthetic demo input; create it so logcollector follows it from the start.
touch /var/log/tpic-demo/transfers.log
