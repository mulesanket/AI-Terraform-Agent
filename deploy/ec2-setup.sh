#!/usr/bin/env bash
# =============================================================================
# EC2 Deployment Script for AI Terraform Agent
# OS: Ubuntu 24.04 LTS or Amazon Linux 2023
#
# Usage on EC2:
#   REPO_URL=https://github.com/<your-user>/<your-repo>.git bash deploy/ec2-setup.sh
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/terraform-agent}"
REPO_URL="${REPO_URL:-}"
TERRAFORM_VERSION="${TERRAFORM_VERSION:-1.8.5}"

echo "========================================"
echo " AI Terraform Agent - EC2 Setup"
echo "========================================"

if [ -z "$REPO_URL" ]; then
    echo "ERROR: Set REPO_URL to your GitHub repository URL."
    echo "Example:"
    echo "  REPO_URL=https://github.com/sanketmu/ai-terraform-agent.git bash deploy/ec2-setup.sh"
    exit 1
fi

if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="$ID"
else
    echo "ERROR: Cannot detect OS"
    exit 1
fi

echo "[1/6] Installing system dependencies..."
if [[ "$OS_ID" == "amzn" ]]; then
    sudo dnf update -y
    sudo dnf install -y docker git python3 python3-pip unzip curl jq
    sudo systemctl start docker
    sudo systemctl enable docker
    sudo usermod -aG docker ec2-user || true
elif [[ "$OS_ID" == "ubuntu" ]]; then
    sudo apt-get update -y
    sudo apt-get install -y docker.io docker-compose-plugin git python3 python3-venv python3-pip unzip curl jq
    sudo systemctl start docker
    sudo systemctl enable docker
    sudo usermod -aG docker ubuntu || true
else
    echo "WARNING: Unsupported OS '$OS_ID'. Attempting generic apt/dnf install..."
    sudo apt-get update -y || sudo dnf update -y
    sudo apt-get install -y docker.io git python3 python3-venv python3-pip unzip curl jq || \
        sudo dnf install -y docker git python3 python3-pip unzip curl jq
fi

echo "[2/6] Installing Terraform..."
if ! command -v terraform >/dev/null 2>&1; then
    curl -fsSLO "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip"
    sudo unzip -o "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -d /usr/local/bin/
    rm "terraform_${TERRAFORM_VERSION}_linux_amd64.zip"
fi
terraform version

echo "[3/6] Cloning or updating repository..."
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR"
    git pull
else
    sudo mkdir -p "$APP_DIR"
    sudo chown "$(whoami):$(whoami)" "$APP_DIR"
    if [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
        echo "ERROR: $APP_DIR exists and is not an empty git repository."
        echo "Move it away or set APP_DIR to a new path."
        exit 1
    fi
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

echo "[4/6] Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install checkov gunicorn
mkdir -p workspace

echo "[5/6] Creating .env if missing..."
if [ ! -f .env ]; then
    cp .env.example .env
    python3 - <<'PY'
from pathlib import Path
import secrets

path = Path(".env")
text = path.read_text()
replacements = {
    "API_KEY=": f"API_KEY={secrets.token_urlsafe(32)}",
    "SECRET_KEY=": f"SECRET_KEY={secrets.token_urlsafe(48)}",
}
for old, new in replacements.items():
    text = text.replace(old, new, 1)
path.write_text(text)
PY
    echo "Created .env with generated API_KEY and SECRET_KEY."
    echo "Edit the remaining values before starting the service:"
    echo "  nano $APP_DIR/.env"
fi

echo "[6/6] Creating systemd service..."
sudo tee /etc/systemd/system/terraform-agent.service > /dev/null << EOF
[Unit]
Description=AI Terraform Agent
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 300 server:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable terraform-agent

echo ""
echo "========================================"
echo " SETUP COMPLETE"
echo "========================================"
echo "1. Edit config:   nano $APP_DIR/.env"
echo "2. Start app:     sudo systemctl start terraform-agent"
echo "3. Check status:  sudo systemctl status terraform-agent"
echo "4. Follow logs:   journalctl -u terraform-agent -f"
echo ""
echo "Access: http://<EC2-PUBLIC-IP>:8080"
echo "========================================"
