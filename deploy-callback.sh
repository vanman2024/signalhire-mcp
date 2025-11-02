#!/bin/bash
# Deploy SignalHire Callback Server to DigitalOcean Droplet
# Usage: ./deploy-callback.sh 198.211.103.138

set -e

DROPLET_IP="${1:-198.211.103.138}"
DEPLOY_USER="root"

echo "🚀 Deploying SignalHire Callback Server to $DROPLET_IP"

# SSH into droplet and setup
ssh -o StrictHostKeyChecking=no ${DEPLOY_USER}@${DROPLET_IP} << 'ENDSSH'
set -e

echo "📦 Installing dependencies..."
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git

echo "📂 Creating application directory..."
mkdir -p /opt/signalhire-callback
cd /opt/signalhire-callback

echo "📥 Cloning repository..."
if [ -d ".git" ]; then
    git pull origin main
else
    git clone https://github.com/vanman2024/Mcp-Servers.git .
fi

cd signalhire

echo "🐍 Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

echo "📦 Installing Python packages..."
pip install --quiet fastapi uvicorn pydantic pydantic-settings email-validator

echo "🔧 Creating callback server startup script..."
cat > /opt/signalhire-callback/signalhire/start-callback.py << 'ENDPY'
#!/usr/bin/env python3
import sys
import uvicorn
from lib.callback_server import CallbackServer

if __name__ == "__main__":
    server = CallbackServer(host="0.0.0.0", port=8000)
    app = server.create_app()

    print("✅ Starting SignalHire Callback Server...")
    print(f"📡 Listening on http://0.0.0.0:8000")
    print(f"🔗 Callback endpoint: /signalhire/callback")
    print(f"❤️  Health check: /health")

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
ENDPY

chmod +x /opt/signalhire-callback/signalhire/start-callback.py

echo "⚙️  Creating systemd service..."
cat > /etc/systemd/system/signalhire-callback.service << 'ENDSERVICE'
[Unit]
Description=SignalHire Callback Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/signalhire-callback/signalhire
ExecStart=/opt/signalhire-callback/signalhire/venv/bin/python start-callback.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
ENDSERVICE

echo "🔄 Enabling and starting service..."
systemctl daemon-reload
systemctl enable signalhire-callback
systemctl restart signalhire-callback

echo "⏳ Waiting for service to start..."
sleep 5

echo "✅ Checking service status..."
systemctl status signalhire-callback --no-pager || true

echo ""
echo "🎉 Deployment complete!"
echo "📡 Callback URL: http://$(curl -s ifconfig.me):8000/signalhire/callback"
echo ""
echo "📋 Useful commands:"
echo "  systemctl status signalhire-callback   # Check status"
echo "  systemctl restart signalhire-callback  # Restart server"
echo "  journalctl -u signalhire-callback -f   # View logs"
echo "  curl http://localhost:8000/health      # Health check"
ENDSSH

echo ""
echo "✅ Deployment finished!"
echo "🔗 Your callback URL is: http://${DROPLET_IP}:8000/signalhire/callback"
