#!/bin/bash
# WLC High Bay Monitoring - Setup Script
# Automated setup for new installations

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║     WLC High Bay Particle Monitoring - Setup Script              ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── Step 1: Check Python version ─────────────────────────────────────────────

echo -e "${BLUE}[1/7] Checking Python version...${NC}"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}✗ Python 3 not found!${NC}"
    echo "Please install Python 3.8 or newer and try again."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')

if [ "$PYTHON_MAJOR" -lt 3 ] || [ "$PYTHON_MAJOR" -eq 3 -a "$PYTHON_MINOR" -lt 8 ]; then
    echo -e "${RED}✗ Python 3.8+ required (found: $PYTHON_VERSION)${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Python $PYTHON_VERSION found${NC}"
echo ""

# ─── Step 2: Install Python dependencies ──────────────────────────────────────

echo -e "${BLUE}[2/7] Installing Python dependencies...${NC}"

if [ -f "requirements.txt" ]; then
    echo "Installing from requirements.txt..."

    # Try regular pip install first
    if python3 -m pip install -r requirements.txt --quiet 2>/dev/null; then
        echo -e "${GREEN}✓ Dependencies installed successfully${NC}"
    else
        # Try with --user flag if system install fails
        echo -e "${YELLOW}⚠ System install failed, trying --user install...${NC}"
        if python3 -m pip install --user -r requirements.txt --quiet 2>/dev/null; then
            echo -e "${GREEN}✓ Dependencies installed (user mode)${NC}"
        else
            echo -e "${YELLOW}⚠ Automated install failed${NC}"
            echo "Please install manually:"
            echo "  pip3 install pymodbus>=3.5 pyyaml>=6.0"
            echo "Or:"
            echo "  pip3 install --user pymodbus>=3.5 pyyaml>=6.0"
            echo ""
            read -p "Press Enter to continue anyway, or Ctrl+C to exit..."
        fi
    fi
else
    echo -e "${RED}✗ requirements.txt not found${NC}"
    exit 1
fi

echo ""

# ─── Step 3: Get counter IP address ───────────────────────────────────────────

echo -e "${BLUE}[3/7] Configuring particle counter connection...${NC}"
echo ""

read -p "Enter particle counter IP address [default: 10.66.66.68]: " COUNTER_IP
COUNTER_IP=${COUNTER_IP:-10.66.66.68}

read -p "Enter Modbus port [default: 502]: " COUNTER_PORT
COUNTER_PORT=${COUNTER_PORT:-502}

read -p "Enter counter admin password (leave empty if none): " COUNTER_PASSWORD

echo ""
echo -e "${GREEN}✓ Counter configuration:${NC}"
echo "  IP: $COUNTER_IP"
echo "  Port: $COUNTER_PORT"
echo ""

# ─── Step 4: Test counter connectivity ────────────────────────────────────────

echo -e "${BLUE}[4/7] Testing counter connectivity...${NC}"

# Create a temporary test script
cat > /tmp/test_counter_$$.py << 'EOF'
import sys
from pymodbus.client import ModbusTcpClient

counter_ip = sys.argv[1]
counter_port = int(sys.argv[2])

try:
    client = ModbusTcpClient(counter_ip, port=counter_port, timeout=5)
    if client.connect():
        print("SUCCESS")
        client.close()
    else:
        print("FAILED")
except Exception as e:
    print(f"ERROR: {e}")
EOF

TEST_RESULT=$(python3 /tmp/test_counter_$$.py "$COUNTER_IP" "$COUNTER_PORT" 2>&1)
rm -f /tmp/test_counter_$$.py

if [[ "$TEST_RESULT" == "SUCCESS" ]]; then
    echo -e "${GREEN}✓ Successfully connected to counter at $COUNTER_IP:$COUNTER_PORT${NC}"
else
    echo -e "${YELLOW}⚠ Could not connect to counter: $TEST_RESULT${NC}"
    echo "The counter may be offline or unreachable."
    echo "You can still continue setup and test connectivity later."
    echo ""
    read -p "Continue anyway? [Y/n]: " CONTINUE
    CONTINUE=${CONTINUE:-Y}
    if [[ ! "$CONTINUE" =~ ^[Yy]$ ]]; then
        echo "Setup cancelled."
        exit 1
    fi
fi

echo ""

# ─── Step 5: Create configuration file ────────────────────────────────────────

echo -e "${BLUE}[5/7] Creating configuration file...${NC}"
echo ""

read -p "Do you want to enable GitHub Pages auto-push? [y/N]: " ENABLE_GITHUB
ENABLE_GITHUB=${ENABLE_GITHUB:-N}

if [[ "$ENABLE_GITHUB" =~ ^[Yy]$ ]]; then
    GITHUB_ENABLED="true"
    echo -e "${YELLOW}⚠ GitHub push enabled${NC}"
    echo "Make sure you have:"
    echo "  1. Forked this repo to your GitHub account"
    echo "  2. Set up SSH key or token for git push"
    echo "  3. Enabled GitHub Pages in repo settings"
else
    GITHUB_ENABLED="false"
    echo -e "${GREEN}✓ GitHub push disabled (monitoring only)${NC}"
fi

# ── Determine archive directory (automatic, no prompts) ───────────────────
echo ""
echo "Determining archive directory..."

# Try to create /project/dune/slow_control/particle_plus
# This is the standard location for Yale/similar institutions
SYSTEM_DIR="/project/dune/slow_control/particle_plus"

if [ -d "/project" ]; then
    # /project exists (likely a cluster with shared filesystem)
    echo "Detected /project filesystem (cluster environment)"
    echo "Attempting to create: $SYSTEM_DIR"

    if mkdir -p "$SYSTEM_DIR" 2>/dev/null; then
        PROJECT_DIR="$SYSTEM_DIR"
        echo -e "${GREEN}✓ Created $PROJECT_DIR${NC}"
    else
        # Need sudo
        echo -e "${YELLOW}⚠ Permission denied, trying with sudo...${NC}"
        if sudo mkdir -p "$SYSTEM_DIR" 2>/dev/null && sudo chown $USER "$SYSTEM_DIR" 2>/dev/null; then
            PROJECT_DIR="$SYSTEM_DIR"
            echo -e "${GREEN}✓ Created $PROJECT_DIR (with sudo)${NC}"
        else
            # Sudo failed, use home directory
            PROJECT_DIR="$HOME/particle_data"
            echo -e "${YELLOW}⚠ Could not create system directory${NC}"
            echo "Using home directory: $PROJECT_DIR"
            mkdir -p "$PROJECT_DIR"
        fi
    fi
else
    # No /project filesystem (desktop/laptop/different cluster)
    echo "No /project filesystem detected (desktop/laptop environment)"
    PROJECT_DIR="$HOME/particle_data"
    echo "Using home directory: $PROJECT_DIR"
    mkdir -p "$PROJECT_DIR"
fi

echo -e "${GREEN}✓ Archive directory: $PROJECT_DIR${NC}"
echo "  (This is where ALL data will be stored permanently)"

# Create config.local.yaml
cat > config.local.yaml << EOF
# Local configuration - created by setup.sh
# This file is gitignored (your personal settings)

counter:
  ip: '$COUNTER_IP'
  port: $COUNTER_PORT
  password: '$COUNTER_PASSWORD'

paths:
  project_data_dir: '$PROJECT_DIR'

github:
  enabled: $GITHUB_ENABLED

# Other settings inherited from config.yaml
EOF

echo -e "${GREEN}✓ Created config.local.yaml${NC}"
echo ""

# ─── Step 6: Create necessary directories ─────────────────────────────────────

echo -e "${BLUE}[6/7] Creating data directories...${NC}"

# Create local data directory (for 30-day live.csv - pushed to GitHub if enabled)
mkdir -p data
echo -e "${GREEN}✓ Created data/ directory (for 30-day rolling window)${NC}"

# Archive directory was already created in Step 5
if [ -d "$PROJECT_DIR" ] && [ -w "$PROJECT_DIR" ]; then
    echo -e "${GREEN}✓ Archive directory ready: $PROJECT_DIR${NC}"
else
    echo -e "${RED}✗ Archive directory not accessible: $PROJECT_DIR${NC}"
    echo "Please check permissions and try again."
    exit 1
fi

echo ""

# ─── Step 7: Test the installation ────────────────────────────────────────────

echo -e "${BLUE}[7/7] Testing installation...${NC}"

# Test if particle_plus.py can be imported
if python3 -c "import particle_plus" 2>/dev/null; then
    echo -e "${GREEN}✓ particle_plus.py loads successfully${NC}"
else
    echo -e "${RED}✗ Failed to load particle_plus.py${NC}"
    echo "Please check the error messages above."
    exit 1
fi

echo ""

# ─── Summary ───────────────────────────────────────────────────────────────────

echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                    ✓ SETUP COMPLETE!                              ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

echo -e "${BLUE}Configuration Summary:${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Counter IP:       $COUNTER_IP:$COUNTER_PORT"
echo "GitHub push:      $GITHUB_ENABLED"
echo "Data directory:   data/"
echo "Archive directory: $PROJECT_DIR"
echo "Config file:      config.local.yaml"
echo ""

echo -e "${BLUE}Next Steps:${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [[ "$TEST_RESULT" == "SUCCESS" ]]; then
    echo -e "${GREEN}1. Your counter is online and reachable!${NC}"
    echo ""
    echo "2. Start monitoring with:"
    echo -e "   ${YELLOW}python3 particle_plus.py --all${NC}"
    echo ""
    echo "   Or run in background (tmux/screen):"
    echo -e "   ${YELLOW}tmux new -s particle${NC}"
    echo -e "   ${YELLOW}python3 particle_plus.py --all${NC}"
    echo -e "   ${YELLOW}# Press Ctrl+B, D to detach${NC}"
else
    echo -e "${YELLOW}1. Counter is not reachable yet${NC}"
    echo "   Make sure the counter is:"
    echo "     • Powered on"
    echo "     • Connected to network"
    echo "     • Accessible from this machine"
    echo ""
    echo "2. Test connectivity:"
    echo -e "   ${YELLOW}python3 test.py${NC}"
    echo ""
    echo "3. Once connected, start monitoring:"
    echo -e "   ${YELLOW}python3 particle_plus.py --all${NC}"
fi

echo ""
echo "3. View local dashboard:"
echo -e "   ${YELLOW}python3 local_serve.py --port 8800${NC}"
echo "   Then open http://localhost:8800"
echo ""

if [[ "$GITHUB_ENABLED" == "true" ]]; then
    echo -e "${YELLOW}4. GitHub push is ENABLED${NC}"
    echo "   • Make sure git is configured:"
    echo -e "     ${YELLOW}git config user.name \"Your Name\"${NC}"
    echo -e "     ${YELLOW}git config user.email \"your@email.com\"${NC}"
    echo "   • Set up SSH key or personal access token"
    echo "   • Test push access:"
    echo -e "     ${YELLOW}git push origin main${NC}"
else
    echo -e "${GREEN}4. GitHub push is DISABLED (monitoring only)${NC}"
    echo "   To enable later, edit config.local.yaml:"
    echo "     github:"
    echo "       enabled: true"
fi

echo ""
echo "Documentation:"
echo "  • README.md - Full documentation"
echo "  • GITHUB_PUSH_SETUP.md - GitHub configuration guide"
echo "  • config.yaml - Configuration reference"
echo ""
echo -e "${GREEN}Happy monitoring! 🎉${NC}"
echo ""
