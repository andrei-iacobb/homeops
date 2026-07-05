#!/bin/bash
set -e

# ============================================================================
# Provision script for Claude SRE Triager LXC
# Runs INSIDE the container as root
# Installs all dependencies and sets up the service
# ============================================================================

echo "[*] Starting provisioning of Claude SRE Triager LXC..."
echo ""

# Update package manager
echo "[*] Updating package lists..."
apt-get update -qq

# Install base packages
echo "[*] Installing base system packages..."
apt-get install -y -qq \
    curl \
    ca-certificates \
    git \
    python3 \
    python3-venv \
    python3-pip \
    jq \
    apt-transport-https \
    gnupg \
    openssh-client \
    wget

# Add NodeSource repository and install Node.js 20.x LTS
echo "[*] Installing Node.js 20.x LTS..."
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null 2>&1 || {
    echo "[!] WARNING: NodeSource setup failed. Trying apt-get install nodejs..."
    apt-get install -y -qq nodejs || {
        echo "[!] ERROR: Failed to install Node.js. This is required for Claude CLI."
        exit 1
    }
}
apt-get install -y -qq nodejs || {
    echo "[!] ERROR: Failed to install Node.js from NodeSource."
    exit 1
}

# Install Claude Code CLI globally
echo "[*] Installing Claude Code CLI (@anthropic-ai/claude-code)..."
npm install -g @anthropic-ai/claude-code > /dev/null 2>&1 || {
    echo "[!] ERROR: Failed to install Claude Code CLI. This is required."
    exit 1
}

# Verify claude CLI installed
if ! command -v claude &> /dev/null; then
    echo "[!] ERROR: claude CLI not found in PATH after installation."
    exit 1
fi
echo "[+] claude CLI installed: $(claude --version 2>/dev/null || echo 'version check failed')"

# Install kubectl (Kubernetes CLI) via direct binary download
# (the old apt.kubernetes.io/kubernetes-xenial repo was retired in 2023)
echo "[*] Installing kubectl..."
KVER=$(curl -L -s https://dl.k8s.io/release/stable.txt)
curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/${KVER}/bin/linux/amd64/kubectl"
chmod +x /usr/local/bin/kubectl
export PATH="$PATH:/usr/local/bin"
if /usr/local/bin/kubectl version --client &>/dev/null; then
    echo "[+] kubectl installed: ${KVER}"
else
    echo "[!] ERROR: kubectl install failed"
    exit 1
fi

# Install Flux CLI (optional but useful for manual Flux operations)
echo "[*] Installing Flux CLI..."
curl -s https://fluxcd.io/install.sh | bash > /dev/null 2>&1 || {
    echo "[!] WARNING: Flux CLI installation failed (optional, not critical)"
}

# Create triager system user
# Home directory: /var/lib/claude-triager
# No login shell (system user)
echo "[*] Creating triager system user..."
useradd -r -s /usr/sbin/nologin -d /var/lib/claude-triager -m triager 2>/dev/null || {
    echo "[+] triager user already exists"
}

# Create application directory structure
echo "[*] Setting up application directories..."
mkdir -p /opt/claude-triager/{bot,policy,state}
chown -R triager:triager /opt/claude-triager
chmod 750 /opt/claude-triager
chmod 750 /opt/claude-triager/bot
chmod 750 /opt/claude-triager/policy
chmod 750 /opt/claude-triager/state

# Create Python virtual environment
echo "[*] Setting up Python 3 virtual environment..."
cd /opt/claude-triager
python3 -m venv venv > /dev/null 2>&1 || {
    echo "[!] ERROR: Failed to create Python virtual environment."
    exit 1
}

# Activate venv and upgrade pip
source venv/bin/activate
pip install --quiet --upgrade pip setuptools wheel

# Copy requirements.txt from stage if present
if [ -f /root/stage/bot/requirements.txt ]; then
    echo "[*] Copying requirements.txt from stage..."
    cp /root/stage/bot/requirements.txt /opt/claude-triager/requirements.txt
    chown triager:triager /opt/claude-triager/requirements.txt
fi

# Install Python dependencies from requirements.txt if present
if [ -f /opt/claude-triager/requirements.txt ]; then
    echo "[*] Installing Python dependencies from requirements.txt..."
    pip install --quiet -r /opt/claude-triager/requirements.txt || {
        echo "[!] ERROR: Failed to install Python dependencies. Check requirements.txt."
        deactivate
        exit 1
    }
else
    echo "[!] WARNING: requirements.txt not found. Installing defaults (discord.py, pyyaml)..."
    pip install --quiet discord.py>=2.3.2 pyyaml>=6.0.1 || {
        echo "[!] ERROR: Failed to install default Python dependencies."
        deactivate
        exit 1
    }
fi

# Copy bot code from stage if present
if [ -d /root/stage/bot ]; then
    echo "[*] Copying bot code from stage..."
    # Only copy main.py if it exists
    if [ -f /root/stage/bot/triager.py ]; then
        cp /root/stage/bot/triager.py /opt/claude-triager/bot/main.py
    fi
fi

# Copy policy files from stage if present
if [ -d /root/stage/policy ]; then
    echo "[*] Copying policy files from stage..."
    cp /root/stage/policy/* /opt/claude-triager/policy/ 2>/dev/null || true
fi

# Deactivate venv and fix ownership
deactivate
chown -R triager:triager /opt/claude-triager
chmod 755 /opt/claude-triager/venv/bin/python3 2>/dev/null || true

# Create config template from example if not present
if [ ! -f /etc/claude-triager.env ]; then
    echo "[*] Creating /etc/claude-triager.env template..."
    cat > /etc/claude-triager.env << 'EOF'
# Discord Bot Configuration
DISCORD_TOKEN=your-bot-token-here
ALERTS_CHANNEL_ID=123456789
OWNER_USER_ID=987654321

# Claude API Configuration
CLAUDE_CODE_OAUTH_TOKEN=sk-...

# Kubernetes Configuration
CLAUDE_RO_KUBECONFIG=/opt/claude-triager/kubeconfig-ro
BOT_RW_KUBECONFIG=/opt/claude-triager/kubeconfig-rw

# Policy and Logging
POLICY_SYSTEM_PROMPT=/opt/claude-triager/policy/system_prompt.txt
TRIAGER_STATE_FILE=/opt/claude-triager/state/triager_state.json
LOG_FILE=/var/log/triager.log

# Optional
# CLAUDE_MODEL=gpt-5.5
HEARTBEAT_INTERVAL=300
CLAUDE_BIN=claude
EOF
    chmod 600 /etc/claude-triager.env
    chown root:root /etc/claude-triager.env
    echo "[+] Created template at /etc/claude-triager.env (mode 600)"
else
    echo "[+] /etc/claude-triager.env already exists"
fi

# Verify perms
if [ -f /etc/claude-triager.env ]; then
    PERMS=$(stat -c %a /etc/claude-triager.env)
    if [ "$PERMS" != "600" ]; then
        echo "[!] ERROR: /etc/claude-triager.env has incorrect permissions: $PERMS (should be 600)"
        chmod 600 /etc/claude-triager.env
    fi
    OWNER=$(stat -c %U:%G /etc/claude-triager.env)
    if [ "$OWNER" != "root:root" ]; then
        echo "[!] ERROR: /etc/claude-triager.env has incorrect owner: $OWNER (should be root:root)"
        chown root:root /etc/claude-triager.env
    fi
fi

# Pre-create log file with correct ownership
echo "[*] Pre-creating log file..."
mkdir -p /var/log
touch /var/log/triager.log
chmod 600 /var/log/triager.log
chown triager:triager /var/log/triager.log
echo "[+] Log file: /var/log/triager.log (mode 600, owner triager:triager)"

# Install systemd service unit
echo "[*] Installing systemd service unit..."
if [ -f /tmp/triager.service ]; then
    cp /tmp/triager.service /etc/systemd/system/triager.service
    chmod 644 /etc/systemd/system/triager.service
    systemctl daemon-reload
    systemctl enable triager
    echo "[+] Service unit installed and enabled (but not started)"
else
    echo "[!] ERROR: /tmp/triager.service not found. Service will not be configured."
    exit 1
fi

# Clean up temporary files and stage directory
echo "[*] Cleaning up temporary files..."
rm -f /tmp/20-provision.sh /tmp/triager.service
rm -rf /root/stage

echo ""
echo "============================================================================"
echo "PROVISIONING COMPLETE - Application ready for configuration"
echo "============================================================================"
echo ""
echo "Next steps (run from Proxmox host with 'pct exec <ctid> ...'):"
echo ""
echo "1. Copy kubeconfigs into the container:"
echo "   pct push <host> /path/to/kubeconfig-ro /opt/claude-triager/kubeconfig-ro"
echo "   pct exec $CTID chmod 600 /opt/claude-triager/kubeconfig-ro"
echo "   pct exec $CTID chown triager:triager /opt/claude-triager/kubeconfig-ro"
echo ""
echo "   pct push <host> /path/to/kubeconfig-rw /opt/claude-triager/kubeconfig-rw"
echo "   pct exec $CTID chmod 600 /opt/claude-triager/kubeconfig-rw"
echo "   pct exec $CTID chown triager:triager /opt/claude-triager/kubeconfig-rw"
echo ""
echo "2. Edit /etc/claude-triager.env with your API keys:"
echo "   pct exec <ctid> nano /etc/claude-triager.env"
echo ""
echo "3. Verify application files are in place:"
echo "   pct exec <ctid> ls -la /opt/claude-triager/bot/"
echo "   pct exec <ctid> ls -la /opt/claude-triager/policy/"
echo ""
echo "4. Start the service:"
echo "   pct exec <ctid> systemctl start triager"
echo "   pct exec <ctid> systemctl status triager"
echo "   pct exec <ctid> journalctl -u triager -f"
echo ""
echo "Application Structure:"
echo "  /opt/claude-triager/"
echo "  ├── venv/                    # Python virtual environment"
echo "  ├── bot/"
echo "  │   └── main.py              # Bot entry point"
echo "  ├── policy/"
echo "  │   └── system_prompt.txt    # Claude system instructions"
echo "  ├── state/                   # Writable state directory"
echo "  ├── kubeconfig-ro            # Read-only kubeconfig (you copy here)"
echo "  ├── kubeconfig-rw            # Read-write kubeconfig (you copy here)"
echo "  └── requirements.txt         # Python dependencies"
echo ""
echo "Config File:"
echo "  /etc/claude-triager.env      # Environment variables (mode 600, root:root)"
echo ""
echo "Log File:"
echo "  /var/log/triager.log        # Service logs (mode 600, triager:triager)"
echo ""
echo "============================================================================"