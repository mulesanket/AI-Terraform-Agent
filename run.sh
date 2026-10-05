#!/usr/bin/env bash
set -euo pipefail

# Load environment variables if .env exists
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Default workdir if not set
export AGENT_WORKDIR="${AGENT_WORKDIR:-$(pwd)/workspace}"
mkdir -p "$AGENT_WORKDIR"

echo "Starting Self-Healing Terraform Agent..."
echo "  Workdir: $AGENT_WORKDIR"
echo "  Port:    ${MCP_PORT:-8080}"

python3 server.py
