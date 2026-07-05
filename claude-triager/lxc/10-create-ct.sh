#!/bin/bash
set -e

# ============================================================================
# Proxmox LXC Container Creation for Claude SRE Triager
# Runs ON the Proxmox host (pve-8 or later)
# ============================================================================

# Configuration variables (preset for this deploy)
CTID=107
HOSTNAME="claude-triager"
STORAGE="VM-Pool380"
TEMPLATE="local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst"
BRIDGE="vmbr0"
CORES=2
MEMORY=2048
DISK=8

# Prompt for root password (no hardcoding)
echo "[*] Enter root password for container (will be hidden):"
read -s ROOT_PASSWORD

if [ -z "$ROOT_PASSWORD" ]; then
    echo "ERROR: Root password cannot be empty"
    exit 1
fi

# Get the directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Sanity checks
if [ ! -f "$SCRIPT_DIR/20-provision.sh" ]; then
    echo "ERROR: 20-provision.sh not found in $SCRIPT_DIR"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/triager.service" ]; then
    echo "ERROR: triager.service not found in $SCRIPT_DIR"
    exit 1
fi

if [ ! -d "$SCRIPT_DIR/../bot" ]; then
    echo "ERROR: bot/ directory not found in $(dirname $SCRIPT_DIR)/"
    exit 1
fi

if [ ! -d "$SCRIPT_DIR/../policy" ]; then
    echo "ERROR: policy/ directory not found in $(dirname $SCRIPT_DIR)/"
    exit 1
fi

# Check if container already exists
if pct status $CTID &>/dev/null; then
    echo "ERROR: Container $CTID already exists!"
    echo "To remove it: pct destroy $CTID"
    exit 1
fi

echo "[*] Creating Debian 12 LXC container for Claude SRE Triager..."
echo "    CTID=$CTID, Storage=$STORAGE, Cores=$CORES, Memory=${MEMORY}MB, Disk=${DISK}GB"

# Create the unprivileged LXC container
# - unprivileged=1: runs without root privileges (more secure)
# - onboot=1: starts automatically when Proxmox boots
# - nesting=1: allows nested containers/VMs inside (helpful for testing)
pct create $CTID $TEMPLATE \
    --hostname "$HOSTNAME" \
    --cores $CORES \
    --memory $MEMORY \
    --rootfs "${STORAGE}:${DISK}" \
    --net0 "name=eth0,bridge=${BRIDGE},type=veth,ip=dhcp" \
    --unprivileged 1 \
    --onboot 1 \
    --features "nesting=1" \
    --password "$ROOT_PASSWORD"

echo "[+] Container created. Starting container $CTID..."
pct start $CTID

# Wait for container to fully boot and obtain DHCP lease
echo "[*] Waiting for container to boot and obtain IP address (may take 10-20s)..."
sleep 15

# Try to get the container IP
CONTAINER_IP=$(pct exec $CTID hostname -I 2>/dev/null | awk '{print $1}' || echo "")
if [ -z "$CONTAINER_IP" ]; then
    echo "[!] Warning: Could not determine container IP. DHCP may be slow."
    echo "    Run manually: pct exec $CTID hostname -I"
else
    echo "[+] Container IP: $CONTAINER_IP"
fi

# Create /root/stage in container for bot and policy files
echo "[*] Creating staging directory in container..."
pct exec $CTID mkdir -p /root/stage

# Push bot/ and policy/ directories
echo "[*] Copying bot/ and policy/ into container..."
pct push $CTID "$SCRIPT_DIR/../bot" /root/stage/bot
pct push $CTID "$SCRIPT_DIR/../policy" /root/stage/policy

# Push provision and service files
echo "[*] Copying provision script and systemd unit into container..."
pct push $CTID "$SCRIPT_DIR/20-provision.sh" /tmp/20-provision.sh
pct push $CTID "$SCRIPT_DIR/triager.service" /tmp/triager.service

# Run the provision script inside the container
echo "[*] Running provision script inside container (this may take 5-10 minutes)..."
if pct exec $CTID bash /tmp/20-provision.sh; then
    echo "[+] Provisioning completed successfully"
else
    echo "[!] Provisioning completed with warnings. Check logs inside container."
fi

echo ""
echo "============================================================================"
echo "CONTAINER PROVISIONING COMPLETE"
echo "============================================================================"
echo ""
echo "Container Details:"
echo "  ID: $CTID"
echo "  Hostname: $HOSTNAME"
echo "  IP Address: ${CONTAINER_IP:-<unknown - run 'pct exec $CTID hostname -I'>}"
echo "  Storage: $STORAGE"
echo ""
echo "BEFORE starting the triager service, complete these manual steps:"
echo ""
echo "1. Copy kubeconfigs into the container:"
echo "   pct push <this-host> /path/to/kubeconfig-ro /opt/claude-triager/kubeconfig-ro"
echo "   pct exec $CTID chmod 600 /opt/claude-triager/kubeconfig-ro"
echo "   pct exec $CTID chown triager:triager /opt/claude-triager/kubeconfig-ro"
echo ""
echo "   pct push <this-host> /path/to/kubeconfig-rw /opt/claude-triager/kubeconfig-rw"
echo "   pct exec $CTID chmod 600 /opt/claude-triager/kubeconfig-rw"
echo "   pct exec $CTID chown triager:triager /opt/claude-triager/kubeconfig-rw"
echo ""
echo "2. Fill in /etc/claude-triager.env with your API keys and settings:"
echo "   pct exec $CTID nano /etc/claude-triager.env"
echo "   (or: pct exec $CTID cat > /etc/claude-triager.env << 'EOT' ... EOT)"
echo ""
echo "3. Then start and monitor the service:"
echo "   pct exec $CTID systemctl start triager"
echo "   pct exec $CTID systemctl status triager"
echo "   pct exec $CTID journalctl -u triager -f"
echo ""
echo "SSH access:"
if [ -n "$CONTAINER_IP" ]; then
    echo "   ssh root@${CONTAINER_IP}"
fi
echo "   Or: pct shell $CTID"
echo ""
echo "============================================================================"